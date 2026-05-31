"""
program_snapshot_external.py
============================
Builds a single-program PDF snapshot for external stakeholders
(e.g. customer-facing reports where the supervisor controls labor).

Pages:
  1 — Cover + P&L (GP only, no net)
  2 — Labor detail (Direct Hire + Temp only, no driver/weight noise)
       + Week-over-Week trend
  3 — Warehouse detail
  4 — Freight lines
  5 — WIP balance

Layout follows the same CONTENT_WIDTH grid system as program_snapshot.py.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime as _dt
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# =====================================================================
# CONFIG (used when running this file as a one-off script)
# =====================================================================
PROGRAM      = os.environ.get("PROGRAM", "Life Time")
PERIOD       = os.environ.get("PERIOD", "2026-02")
PERIOD_LABEL = os.environ.get(
    "PERIOD_LABEL",
    pd.to_datetime(PERIOD + "-01").strftime("%B %Y"),
)

BASE_DIR  = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "logo_nautical.png"

# Output filename derives from program name automatically
_safe_name = PROGRAM.replace(" ", "_").replace("/", "_").replace(".", "")
OUT_PATH = BASE_DIR / f"{_safe_name}_External_{PERIOD}.pdf"


# =====================================================================
# Grid system — matches internal snapshot
# =====================================================================
PAGE_SIZE     = landscape(LETTER)
PAGE_MARGIN   = 0.5 * inch
CONTENT_WIDTH = PAGE_SIZE[0] - (2 * PAGE_MARGIN)  # 10.0 inches

SPACE_XS = 0.05 * inch
SPACE_S  = 0.10 * inch
SPACE_M  = 0.20 * inch
SPACE_L  = 0.30 * inch

BRAND_NAVY  = colors.HexColor("#003366")
BRAND_BLUE  = colors.HexColor("#1f77b4")
BRAND_LIGHT = colors.HexColor("#e6f2ff")
BRAND_GRAY  = colors.HexColor("#555555")
BRAND_FAINT = colors.HexColor("#999999")
BRAND_NEG   = colors.HexColor("#B00020")
ROW_ALT     = colors.HexColor("#fafafa")


def _cols(*ratios) -> list:
    """Compute column widths from ratios; normalizes to fill CONTENT_WIDTH."""
    total = sum(ratios)
    return [(r / total) * CONTENT_WIDTH for r in ratios]


# =====================================================================
# Format helpers
# =====================================================================
def _dollar(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v) if v is not None else ""


def _pct(v) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except Exception:
        return str(v) if v is not None else ""


# =====================================================================
# Optional: data loaders (used when this file is run as a script)
# =====================================================================
def _get_engine():
    load_dotenv()
    conn = os.getenv("SUPABASE_CONN")
    if not conn:
        raise RuntimeError("Missing SUPABASE_CONN environment variable.")
    return create_engine(conn, pool_pre_ping=True)


def load_pnl_row(engine, program: str, period: str) -> pd.Series:
    sql = text("""
        SELECT *
        FROM mv_program_profitability
        WHERE month_start = TO_DATE(:period, 'YYYY-MM')
          AND customer_program = :program
        LIMIT 1
    """)
    df = pd.read_sql(sql, engine, params={"period": period, "program": program})
    if df.empty:
        raise ValueError(f"No profitability row found for {program} / {period}")
    return df.iloc[0]


def load_labor_summary(engine, program: str, period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            labor_type,
            bucket AS cost_center,
            activity_driver,
            SUM(applied_cost) AS allocated_cost
        FROM stg_labor_applied
        WHERE accrual_period = :period
          AND program = :program
          AND labor_type IN ('direct_cogs', 'temp')
        GROUP BY 1, 2, 3
        ORDER BY labor_type, cost_center, activity_driver
    """)
    return pd.read_sql(sql, engine, params={"period": period, "program": program})


def load_labor_employee_detail(engine, program: str, period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            labor_type,
            employee_name AS employee,
            COALESCE(role_detail, labor_source, '') AS role,
            source_bucket AS cost_center,
            activity_driver,
            allocated_cost
        FROM stg_labor_incurred_employee
        WHERE accrual_period = :period
          AND target_program = :program
          AND labor_type IN ('direct_cogs', 'temp')
        ORDER BY labor_type, allocated_cost DESC, employee_name
    """)
    try:
        return pd.read_sql(sql, engine, params={"period": period, "program": program})
    except Exception:
        return pd.DataFrame(columns=[
            "labor_type", "employee", "role", "cost_center",
            "activity_driver", "allocated_cost",
        ])


def load_labor_weekly(engine, program: str, period: str) -> pd.DataFrame:
    """Same query as internal — returns weekly cost spread evenly across ISO weeks."""
    year, month = int(period.split("-")[0]), int(period.split("-")[1])
    month_start = pd.Timestamp(year=year, month=month, day=1).date()
    month_end = (pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)).date()

    sql = text("""
        WITH dh_daily AS (
            SELECT
                dh.employee_name,
                dh.total_labor_cost,
                GREATEST(dh.pay_period_start, :month_start) AS overlap_start,
                LEAST(dh.pay_period_end, :month_end)        AS overlap_end,
                (dh.pay_period_end - dh.pay_period_start + 1) AS period_days
            FROM stg_labor_direct_hire dh
            WHERE dh.accrual_period = :period
              AND dh.pay_period_start IS NOT NULL
              AND dh.pay_period_end IS NOT NULL
        ),
        dh_expanded AS (
            SELECT
                d.employee_name,
                day::date AS work_day,
                d.total_labor_cost / NULLIF(d.period_days, 0) AS daily_cost
            FROM dh_daily d
            CROSS JOIN LATERAL generate_series(d.overlap_start, d.overlap_end, INTERVAL '1 day') day
            WHERE d.overlap_start <= d.overlap_end
        ),
        dh AS (
            SELECT
                DATE_TRUNC('week', e.work_day)::date AS week_start,
                'Direct Hire'::text AS labor_type,
                SUM(e.daily_cost * COALESCE(ea.allocation_pct, 0)) AS weekly_cost
            FROM dh_expanded e
            JOIN stg_labor_employee_allocation ea
              ON ea.employee_name = e.employee_name
             AND ea.accrual_period = :period
            WHERE ea.target_program = :program
            GROUP BY 1
        ),
        tmp_daily AS (
            SELECT
                t.employee_name,
                t.total_labor_cost,
                GREATEST(t.pay_period_start, :month_start) AS overlap_start,
                LEAST(t.pay_period_end, :month_end)        AS overlap_end,
                (t.pay_period_end - t.pay_period_start + 1) AS period_days
            FROM stg_labor_temp t
            WHERE t.accrual_period = :period
              AND t.pay_period_start IS NOT NULL
              AND t.pay_period_end IS NOT NULL
        ),
        tmp_expanded AS (
            SELECT
                d.employee_name,
                day::date AS work_day,
                d.total_labor_cost / NULLIF(d.period_days, 0) AS daily_cost
            FROM tmp_daily d
            CROSS JOIN LATERAL generate_series(d.overlap_start, d.overlap_end, INTERVAL '1 day') day
            WHERE d.overlap_start <= d.overlap_end
        ),
        tmp AS (
            SELECT
                DATE_TRUNC('week', e.work_day)::date AS week_start,
                'Temp'::text AS labor_type,
                SUM(e.daily_cost * COALESCE(ea.allocation_pct, 0)) AS weekly_cost
            FROM tmp_expanded e
            JOIN stg_labor_employee_allocation ea
              ON ea.employee_name = e.employee_name
             AND ea.accrual_period = :period
            WHERE ea.target_program = :program
            GROUP BY 1
        )
        SELECT week_start, labor_type, weekly_cost FROM dh
        UNION ALL
        SELECT week_start, labor_type, weekly_cost FROM tmp
        ORDER BY week_start, labor_type
    """)
    return pd.read_sql(sql, engine, params={
        "period": period, "program": program,
        "month_start": month_start, "month_end": month_end,
    })


def load_warehouse(engine, program: str, period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            program_bucket,
            category,
            cost_type,
            driver_type,
            allocation_pct,
            allocation_amount
        FROM stg_warehouse_allocation
        WHERE month_start = TO_DATE(:period, 'YYYY-MM')
          AND customer_program = :program
          AND cost_type = 'cogs'
        ORDER BY allocation_amount DESC, program_bucket
    """)
    return pd.read_sql(sql, engine, params={"period": period, "program": program})


def load_freight(engine, program: str, period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            invoice_num,
            bill_date,
            line_description,
            amount,
            match_type
        FROM stg_freight_applied
        WHERE accrual_period = :period
          AND customer_program = :program
        ORDER BY bill_date, invoice_num
    """)
    try:
        return pd.read_sql(sql, engine, params={"period": period, "program": program})
    except Exception:
        return pd.DataFrame(columns=[
            "invoice_num", "bill_date", "line_description", "amount", "match_type",
        ])


def load_wip(engine, program: str) -> dict:
    labor_sql = text("""
        SELECT
            accrual_period,
            cost_center,
            SUM(units_produced) AS units_produced,
            SUM(units_produced - units_remaining) AS units_consumed,
            SUM(
                labor_pool * CASE
                    WHEN units_produced > 0 THEN units_remaining / units_produced
                    ELSE 1
                END
            ) AS wip_balance
        FROM stg_wip_production_layers
        WHERE customer_program = :program
          AND units_remaining > 0
        GROUP BY 1, 2
        ORDER BY accrual_period, cost_center
    """)

    warehouse_sql = text("""
        SELECT
            accrual_period AS period,
            program_bucket,
            SUM(wip_balance) AS wip_balance
        FROM stg_warehouse_wip
        WHERE customer_name = :program
          AND wip_balance <> 0
        GROUP BY 1, 2
        ORDER BY 1, 2
    """)

    try:
        labor_df = pd.read_sql(labor_sql, engine, params={"program": program})
    except Exception:
        labor_df = pd.DataFrame(columns=[
            "accrual_period", "cost_center", "units_produced", "units_consumed", "wip_balance",
        ])

    try:
        wh_df = pd.read_sql(warehouse_sql, engine, params={"program": program})
    except Exception:
        wh_df = pd.DataFrame(columns=["period", "program_bucket", "wip_balance"])

    return {"labor_production": labor_df, "warehouse": wh_df}


# =====================================================================
# Weekly chart renderer (same as internal)
# =====================================================================
def _render_weekly_chart_to_image(weekly_df: pd.DataFrame, out_dir: str) -> Optional[str]:
    if weekly_df.empty:
        return None

    trend = weekly_df.pivot_table(
        index="week_start", columns="labor_type",
        values="weekly_cost", aggfunc="sum",
    ).fillna(0).sort_index()
    trend["Total"] = trend.sum(axis=1)
    trend["Rolling_4wk_Avg"] = trend["Total"].rolling(window=4, min_periods=1).mean()
    trend["Spike"] = trend["Total"] > (trend["Rolling_4wk_Avg"] * 1.25)

    fig, ax = plt.subplots(figsize=(10.0, 3.0))

    for col in trend.columns:
        if col in ("Rolling_4wk_Avg", "Spike", "Total"):
            continue
        ax.plot(trend.index, trend[col], marker="o", label=col, linewidth=1.5)

    ax.plot(trend.index, trend["Total"], marker="o", label="Total",
            linewidth=2, color="#1f77b4")
    ax.plot(trend.index, trend["Rolling_4wk_Avg"], linestyle="--",
            color="gray", label="4-wk Rolling Avg", linewidth=1.2)

    spikes = trend[trend["Spike"]]
    if not spikes.empty:
        ax.scatter(spikes.index, spikes["Total"], marker="x", s=120,
                   color="red", label="Spike (>25% above rolling avg)",
                   zorder=5, linewidths=2)

    ax.set_xticks(trend.index)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.set_ylabel("Labor Cost ($)")
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, 1.25),  
        ncol=5,                        
        fontsize=7,                    
        frameon=False,
    )
    ax.grid(True, alpha=0.3)
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right", fontsize=8)
    plt.tight_layout()

    chart_path = os.path.join(out_dir, f"weekly_chart_{_dt.now().timestamp()}.png")
    fig.savefig(chart_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return chart_path


# =====================================================================
# Main builder
# =====================================================================
def build_program_snapshot_external(
    out_path: str,
    program: str,
    period_label: str,
    pnl_row: pd.Series,
    labor_df: pd.DataFrame,
    labor_employee_df: pd.DataFrame,
    warehouse_df: pd.DataFrame,
    freight_df: pd.DataFrame,
    wip: dict,
    logo_path: Optional[str] = None,
    labor_weekly_df: Optional[pd.DataFrame] = None,
) -> str:

    if labor_weekly_df is None:
        labor_weekly_df = pd.DataFrame()

    styles = getSampleStyleSheet()
    ts = _dt.now().strftime("%Y-%m-%d %H:%M")
    story = []

    # ---- Paragraph styles ----
    title_style = ParagraphStyle(
        "SnapTitle", parent=styles["Title"],
        fontSize=22, leading=26, textColor=BRAND_NAVY, alignment=TA_LEFT,
    )
    program_style = ParagraphStyle(
        "ProgramName", parent=styles["Title"],
        fontSize=18, textColor=BRAND_BLUE, alignment=TA_LEFT,
    )
    sub_style = ParagraphStyle(
        "SnapSub", parent=styles["Normal"],
        fontSize=11, textColor=BRAND_GRAY,
    )
    ts_style = ParagraphStyle(
        "SnapTS", parent=styles["Normal"],
        fontSize=9, textColor=BRAND_FAINT,
    )
    h2_style = ParagraphStyle(
        "SnapH2", parent=styles["Heading2"],
        fontSize=13, leading=16, textColor=BRAND_NAVY,
        spaceBefore=0, spaceAfter=4,
    )
    cell_style = ParagraphStyle(
        "SnapCell", parent=styles["Normal"],
        fontSize=8, leading=10,
    )
    label_style = ParagraphStyle(
        "SnapLabel", parent=styles["Normal"],
        fontSize=9, leading=11, textColor=BRAND_NAVY,
    )
    emp_cell_style = ParagraphStyle(
        "EmpCell", parent=styles["Normal"],
        fontSize=7, leading=9, textColor=colors.HexColor("#444444"),
    )
    caption_style = ParagraphStyle(
        "Caption", parent=styles["Normal"],
        fontSize=7, leading=9, textColor=colors.HexColor("#666666"),
    )

    # ---- Helpers ----
    def _section(title: str):
        story.append(Spacer(1, SPACE_M))
        story.append(Paragraph(title, h2_style))
        story.append(HRFlowable(
            width=CONTENT_WIDTH, thickness=0.5, color=BRAND_NAVY,
            spaceBefore=0, spaceAfter=SPACE_S, hAlign="LEFT",
        ))

    def _data_table(headers, rows_data, col_ratios):
        col_widths = _cols(*col_ratios)
        rows = [headers]
        for r in rows_data:
            rows.append([
                Paragraph(str(v) if v is not None else "", cell_style)
                for v in r
            ])
        t = Table(rows, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("FONT",          (0, 0), (-1, 0),  "Helvetica-Bold", 8),
            ("BACKGROUND",    (0, 0), (-1, 0),  BRAND_NAVY),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
            ("GRID",          (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ("FONT",          (0, 1), (-1, -1), "Helvetica", 8),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 4),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ]))
        return t

    def _employee_table(headers, rows_data, col_ratios):
        col_widths = _cols(*col_ratios)
        rows = [headers]
        for r in rows_data:
            rows.append([
                Paragraph(str(v) if v is not None else "", emp_cell_style)
                for v in r
            ])
        t = Table(rows, colWidths=col_widths, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("FONT",          (0, 0), (-1, 0),  "Helvetica-Bold", 7),
            ("BACKGROUND",    (0, 0), (-1, 0),  BRAND_LIGHT),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  BRAND_NAVY),
            ("GRID",          (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, ROW_ALT]),
            ("FONT",          (0, 1), (-1, -1), "Helvetica", 7),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN",         (-1, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ]))
        return t

    # =================================================================
    # Cover
    # =================================================================
    if logo_path and os.path.exists(logo_path):
        logo = Image(logo_path)
        logo._restrictSize(1.8 * inch, 1.0 * inch)
        logo.hAlign = "LEFT"
        story.append(logo)
        story.append(Spacer(1, SPACE_M))

    story.append(HRFlowable(
        width=CONTENT_WIDTH, thickness=1.5, color=BRAND_NAVY,
        spaceBefore=0, spaceAfter=0, hAlign="LEFT",
    ))
    story.append(Spacer(1, SPACE_L))
    story.append(Paragraph("Program Snapshot", title_style))
    story.append(Spacer(1, SPACE_S))
    story.append(Paragraph(program, program_style))
    story.append(Spacer(1, SPACE_S))
    story.append(Paragraph(period_label, sub_style))
    story.append(Paragraph(f"Generated: {ts}", ts_style))
    story.append(Spacer(1, SPACE_L))

    # =================================================================
    # P&L — GP ONLY (no net for external)
    # =================================================================
    _section("Program P&L")

    pnl_items = [
        ("Revenue",           _dollar(pnl_row.get("revenue", 0)),         False),
        ("Temp Labor",        _dollar(pnl_row.get("temp_labor", 0)),      False),
        ("Direct Hire",       _dollar(pnl_row.get("direct_hire", 0)),     False),
        ("Raw Materials",     _dollar(pnl_row.get("raw_materials", 0)),   False),
        ("Equipment",         _dollar(pnl_row.get("equipment", 0)),       False),
        ("Commission",        _dollar(pnl_row.get("commission", 0)),      False),
        ("Freight & Storage", _dollar(pnl_row.get("freight_storage", 0)), False),
        ("Warehouse",         _dollar(pnl_row.get("applied_wh", 0)),      False),
        ("Gross Profit",      _dollar(pnl_row.get("gross_profit", 0)),    True),
        ("GP Margin",         _pct(pnl_row.get("gp_margin", 0)),          True),
    ]

    pnl_rows = []
    for lbl, val, is_key in pnl_items:
        try:
            num = float(val.replace("$", "").replace(",", "").replace("%", ""))
            color_str = "#B00020" if num < 0 else ("#000000" if not is_key else "#003366")
        except Exception:
            color_str = "#003366" if is_key else "#000000"
        font = "Helvetica-Bold" if is_key else "Helvetica"
        pnl_rows.append([
            Paragraph(f'<font name="{font}">{lbl}</font>', cell_style),
            Paragraph(f'<font name="{font}" color="{color_str}">{val}</font>', cell_style),
        ])

    pt = Table(
        pnl_rows,
        colWidths=[CONTENT_WIDTH * 0.40, CONTENT_WIDTH * 0.20],
        hAlign="LEFT",
    )
    pt.setStyle(TableStyle([
        ("ALIGN",         (1, 0), (1, -1), "RIGHT"),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW",     (0, 7), (-1, 7), 0.5, BRAND_NAVY),
    ]))
    story.append(pt)

    story.append(PageBreak())

    # =================================================================
    # Labor — no driver values / no weights / no SGA
    # =================================================================
    _section("Labor Allocation")

    if labor_df.empty:
        story.append(Paragraph("No labor allocated to this program for the period.", cell_style))
    else:
        for ltype, lbl in [("direct_cogs", "Direct Hire"), ("temp", "Temp")]:
            sub = labor_df[labor_df["labor_type"] == ltype].copy()
            if sub.empty:
                continue

            ltype_total = float(sub["allocated_cost"].sum())
            story.append(Paragraph(
                f'<font color="#003366"><b>{lbl} — {_dollar(ltype_total)}</b></font>',
                label_style,
            ))
            story.append(Spacer(1, SPACE_XS))

            summary_rows = sub[["cost_center", "activity_driver", "allocated_cost"]].copy()
            summary_rows["allocated_cost"] = summary_rows["allocated_cost"].map(_dollar)
            story.append(_data_table(
                ["Cost Center", "Driver", "Allocated"],
                summary_rows.values.tolist(),
                col_ratios=[0.35, 0.45, 0.20],
            ))
            story.append(Spacer(1, SPACE_S))

            emp_sub = labor_employee_df[labor_employee_df["labor_type"] == ltype].copy()
            if not emp_sub.empty:
                story.append(Paragraph("Employee Detail", emp_cell_style))
                story.append(Spacer(1, SPACE_XS))

                emp_rows = []
                for _, er in emp_sub.sort_values("allocated_cost", ascending=False).iterrows():
                    emp_rows.append([
                        str(er.get("employee", "")),
                        str(er.get("role", "")),
                        str(er.get("cost_center", "")),
                        str(er.get("activity_driver", "")),
                        _dollar(er.get("allocated_cost", 0)),
                    ])
                story.append(_employee_table(
                    ["Employee", "Role", "Cost Center", "Driver", "Allocated"],
                    emp_rows,
                    col_ratios=[0.25, 0.20, 0.20, 0.20, 0.15],
                ))

            story.append(Spacer(1, SPACE_M))

        # ─── Week-over-Week Labor Trend ──────────────────────────────
        story.append(PageBreak())
        _section("Week-over-Week Labor Trend")
        story.append(Paragraph(
            "Shows only employees directly allocated to this program via the labor "
            "review process. Bucket-allocated labor (Demo, OGP, Overwrap, Operations, etc.) "
            "is excluded. Week labels follow ISO weeks (Monday start).",
            caption_style,
        ))
        story.append(Spacer(1, SPACE_S))

        if labor_weekly_df.empty:
            story.append(Paragraph(
                "No direct employee allocations for this program. "
                "Weekly trends only show for programs with direct labor.",
                cell_style,
            ))
        else:
            with tempfile.TemporaryDirectory() as tmpdir:
                chart_path = _render_weekly_chart_to_image(labor_weekly_df, tmpdir)
                if chart_path and os.path.exists(chart_path):
                    chart_img = Image(chart_path)
                    chart_img._restrictSize(CONTENT_WIDTH, 3.5 * inch)
                    chart_img.hAlign = "LEFT"
                    story.append(chart_img)
                    story.append(Spacer(1, SPACE_S))

            trend = labor_weekly_df.pivot_table(
                index="week_start", columns="labor_type",
                values="weekly_cost", aggfunc="sum",
            ).fillna(0).sort_index()
            trend["Total"] = trend.sum(axis=1)
            trend["Rolling_4wk_Avg"] = trend["Total"].rolling(window=4, min_periods=1).mean()
            trend["Spike"] = trend["Total"] > (trend["Rolling_4wk_Avg"] * 1.25)

            wk_cols = [c for c in trend.columns if c not in ("Rolling_4wk_Avg", "Spike")]
            headers = ["Week"] + wk_cols + ["4-wk Rolling Avg", "Spike"]

            rows_data = []
            for week, row in trend.iterrows():
                week_str = pd.to_datetime(week).strftime("%Y-%m-%d")
                line = [week_str]
                for c in wk_cols:
                    line.append(_dollar(row[c]))
                line.append(_dollar(row["Rolling_4wk_Avg"]))
                line.append("Spike" if row["Spike"] else "")
                rows_data.append(line)

            n_data_cols = len(wk_cols) + 1
            data_col_ratio = (1.0 - 0.15 - 0.10) / n_data_cols
            ratios = [0.15] + [data_col_ratio] * n_data_cols + [0.10]
            story.append(_data_table(headers, rows_data, col_ratios=ratios))

    story.append(PageBreak())

    # =================================================================
    # Warehouse
    # =================================================================
    _section("Warehouse Allocation")

    if not warehouse_df.empty:
        total_wh = float(warehouse_df["allocation_amount"].sum())
        story.append(Paragraph(f"Total: {_dollar(total_wh)}", cell_style))
        story.append(Spacer(1, SPACE_XS))

        rows_data = warehouse_df[
            ["program_bucket", "category", "cost_type", "driver_type",
             "allocation_pct", "allocation_amount"]
        ].copy()
        rows_data["allocation_pct"]    = rows_data["allocation_pct"].map(lambda x: f"{float(x):.2%}")
        rows_data["allocation_amount"] = rows_data["allocation_amount"].map(_dollar)

        story.append(_data_table(
            ["Bucket", "Category", "Cost Type", "Driver", "Alloc %", "Allocated"],
            rows_data.values.tolist(),
            col_ratios=[0.25, 0.15, 0.12, 0.23, 0.10, 0.15],
        ))
    else:
        story.append(Paragraph(
            "No warehouse cost allocated to this program for the period.",
            cell_style,
        ))

    story.append(PageBreak())

    # =================================================================
    # Freight
    # =================================================================
    _section("Freight Lines")

    if not freight_df.empty:
        total_fr = float(freight_df["amount"].sum())
        story.append(Paragraph(f"Total: {_dollar(total_fr)}", cell_style))
        story.append(Spacer(1, SPACE_XS))

        rows_data = freight_df[
            ["invoice_num", "bill_date", "line_description", "amount", "match_type"]
        ].copy()
        rows_data["bill_date"] = pd.to_datetime(rows_data["bill_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        rows_data["amount"]    = rows_data["amount"].map(_dollar)

        story.append(_data_table(
            ["Invoice", "Bill Date", "Description", "Amount", "Match Type"],
            rows_data.values.tolist(),
            col_ratios=[0.15, 0.12, 0.45, 0.13, 0.15],
        ))
    else:
        story.append(Paragraph(
            "No matched freight lines for this program in the period.",
            cell_style,
        ))

    story.append(PageBreak())

    # =================================================================
    # WIP
    # =================================================================
    _section("WIP Balance")

    lp = wip.get("labor_production", pd.DataFrame())
    wh = wip.get("warehouse", pd.DataFrame())
    total_lp = float(lp["wip_balance"].sum()) if not lp.empty else 0.0
    total_wh_wip = float(wh["wip_balance"].sum()) if not wh.empty else 0.0
    total_wip = total_lp + total_wh_wip

    if total_wip == 0:
        story.append(Paragraph("No outstanding WIP for this program as of this period.", cell_style))
    else:
        story.append(Paragraph(f"Total WIP Balance: {_dollar(total_wip)}", cell_style))
        story.append(Spacer(1, SPACE_S))

        if not lp.empty:
            story.append(Paragraph(
                f'<font color="#003366"><b>Labor Production WIP — {_dollar(total_lp)}</b></font>',
                label_style,
            ))
            story.append(Spacer(1, SPACE_XS))
            rows_data = lp[
                ["accrual_period", "cost_center", "units_produced", "units_consumed", "wip_balance"]
            ].copy()
            rows_data["wip_balance"] = rows_data["wip_balance"].map(_dollar)
            story.append(_data_table(
                ["Period", "Cost Center", "Units Produced", "Units Consumed", "WIP Balance"],
                rows_data.values.tolist(),
                col_ratios=[0.15, 0.25, 0.20, 0.20, 0.20],
            ))
            story.append(Spacer(1, SPACE_S))

        if not wh.empty:
            story.append(Paragraph(
                f'<font color="#003366"><b>Warehouse WIP — {_dollar(total_wh_wip)}</b></font>',
                label_style,
            ))
            story.append(Spacer(1, SPACE_XS))
            rows_data = wh[["period", "program_bucket", "wip_balance"]].copy()
            rows_data["wip_balance"] = rows_data["wip_balance"].map(_dollar)
            story.append(_data_table(
                ["Period", "Bucket", "WIP Balance"],
                rows_data.values.tolist(),
                col_ratios=[0.20, 0.60, 0.20],
            ))

    # =================================================================
    # Footer
    # =================================================================
    footer_style = ParagraphStyle(
        "SnapFooter", parent=styles["Normal"],
        fontSize=8, textColor=BRAND_FAINT, alignment=TA_CENTER,
    )
    story.append(Spacer(1, SPACE_L))
    story.append(HRFlowable(
        width=CONTENT_WIDTH, thickness=0.5, color=colors.lightgrey,
        spaceBefore=0, spaceAfter=SPACE_S, hAlign="LEFT",
    ))
    story.append(Paragraph(
        f"Nautical Manufacturing & Fulfillment LLC — Confidential — {ts}",
        footer_style,
    ))

    doc = SimpleDocTemplate(
        out_path,
        pagesize=PAGE_SIZE,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
    )
    doc.build(story)
    return os.path.abspath(out_path)


# =====================================================================
# Script entry point
# =====================================================================
if __name__ == "__main__":
    eng = _get_engine()
    pnl       = load_pnl_row(eng, PROGRAM, PERIOD)
    labor_df  = load_labor_summary(eng, PROGRAM, PERIOD)
    emp_df    = load_labor_employee_detail(eng, PROGRAM, PERIOD)
    weekly_df = load_labor_weekly(eng, PROGRAM, PERIOD)
    wh_df     = load_warehouse(eng, PROGRAM, PERIOD)
    fr_df     = load_freight(eng, PROGRAM, PERIOD)
    wip_data  = load_wip(eng, PROGRAM)

    pdf_path = build_program_snapshot_external(
        out_path          = str(OUT_PATH),
        program           = PROGRAM,
        period_label      = PERIOD_LABEL,
        pnl_row           = pnl,
        labor_df          = labor_df,
        labor_employee_df = emp_df,
        warehouse_df      = wh_df,
        freight_df        = fr_df,
        wip               = wip_data,
        labor_weekly_df   = weekly_df,
        logo_path         = str(LOGO_PATH) if LOGO_PATH.exists() else None,
    )
    print(f"Built: {pdf_path}")