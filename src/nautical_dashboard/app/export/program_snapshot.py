"""
program_snapshot.py
===================
Builds a single-program PDF snapshot.

Pages:
  1 — Cover + P&L
  2 — Labor detail + Week-over-Week trend
  3 — Warehouse detail
  4 — Freight lines
  5 — WIP balance

Layout: all tables are anchored to a single CONTENT_WIDTH grid (10 inches
for landscape LETTER with 0.5" margins). Column widths are expressed as
ratios of CONTENT_WIDTH so every table aligns to the same left/right
boundaries regardless of column count.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime as _dt
from typing import Optional

import pandas as pd

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
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# =====================================================================
# Grid system
# =====================================================================
# Landscape LETTER is 11.0" wide. With 0.5" left + 0.5" right margins,
# content width is 10.0 inches. Every table column-width array is
# expressed as ratios that sum to 1.0, then multiplied by this constant.
# This guarantees every table spans the same left/right boundaries.
PAGE_SIZE     = landscape(LETTER)
PAGE_MARGIN   = 0.5 * inch
CONTENT_WIDTH = PAGE_SIZE[0] - (2 * PAGE_MARGIN)  # 10.0 inches

# Vertical rhythm — use only these spacer values throughout the document
SPACE_XS = 0.05 * inch
SPACE_S  = 0.10 * inch
SPACE_M  = 0.20 * inch
SPACE_L  = 0.30 * inch

# Brand colors
BRAND_NAVY    = colors.HexColor("#003366")
BRAND_BLUE    = colors.HexColor("#1f77b4")
BRAND_LIGHT   = colors.HexColor("#e6f2ff")
BRAND_GRAY    = colors.HexColor("#555555")
BRAND_FAINT   = colors.HexColor("#999999")
BRAND_NEG     = colors.HexColor("#B00020")
ROW_ALT       = colors.HexColor("#fafafa")


def _cols(*ratios) -> list:
    """
    Compute column widths from ratios that should sum to ~1.0.
    Any drift gets normalized so the table fills CONTENT_WIDTH exactly.
    """
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
        return f"{float(v)*100:.1f}%"
    except Exception:
        return str(v) if v is not None else ""


# =====================================================================
# Weekly chart renderer
# =====================================================================
def _render_weekly_chart_to_image(weekly_df: pd.DataFrame, out_dir: str) -> Optional[str]:
    """Render the weekly trend chart to PNG at CONTENT_WIDTH proportions."""
    if weekly_df.empty:
        return None

    trend = weekly_df.pivot_table(
        index="week_start",
        columns="labor_type",
        values="weekly_cost",
        aggfunc="sum",
    ).fillna(0).sort_index()
    trend["Total"] = trend.sum(axis=1)
    trend["Rolling_4wk_Avg"] = trend["Total"].rolling(window=4, min_periods=1).mean()
    trend["Spike"] = trend["Total"] > (trend["Rolling_4wk_Avg"] * 1.25)

    # Chart sized to match CONTENT_WIDTH at 100 dpi → 10" wide × 3" tall
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
def build_program_snapshot(
    out_path: str,
    program: str,
    period_label: str,
    pnl_row: pd.Series,
    labor_df: pd.DataFrame,
    warehouse_df: pd.DataFrame,
    freight_df: pd.DataFrame,
    wip: dict,
    labor_employee_df: pd.DataFrame = None,
    logo_path: Optional[str] = None,
    labor_weekly_df: Optional[pd.DataFrame] = None,
) -> str:

    if labor_employee_df is None:
        labor_employee_df = pd.DataFrame()
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

    # ---- Section header helper ----
    def _section(title: str):
        story.append(Spacer(1, SPACE_M))
        story.append(Paragraph(title, h2_style))
        story.append(HRFlowable(
            width=CONTENT_WIDTH, thickness=0.5, color=BRAND_NAVY,
            spaceBefore=0, spaceAfter=SPACE_S, hAlign="LEFT",
        ))

    # ---- Data table helper (left-aligned, fills CONTENT_WIDTH) ----
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

    # ---- Employee detail table (lighter header) ----
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
    # Program P&L
    # =================================================================
    _section("Program P&L")

    if pnl_row is not None:
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
            ("Applied SGA",       _dollar(pnl_row.get("applied_sga", 0)),     False),
            ("Net Profit",        _dollar(pnl_row.get("net_profit", 0)),      True),
            ("Net Margin",        _pct(pnl_row.get("net_margin", 0)),         True),
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

        # P&L is narrow — leave it at 60% of content width
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
            ("LINEBELOW",     (0, 10), (-1, 10), 0.5, BRAND_NAVY),
        ]))
        story.append(pt)

    story.append(PageBreak())

    # =================================================================
    # Labor Allocation
    # =================================================================
    _section("Labor Allocation")

    if labor_df.empty:
        story.append(Paragraph("No labor allocated to this program for the period.", cell_style))
    else:
        for ltype, lbl in [("direct_cogs", "Direct Hire"), ("temp", "Temp"), ("direct_sga", "SGA")]:
            sub = labor_df[labor_df["labor_type"] == ltype].copy()
            if sub.empty:
                continue

            ltype_total = float(sub["allocated_cost"].sum())
            story.append(Paragraph(
                f'<font color="#003366"><b>{lbl} — {_dollar(ltype_total)}</b></font>',
                label_style,
            ))
            story.append(Spacer(1, SPACE_XS))

            # Summary table per labor type
            if ltype == "direct_sga":
                prog_rev    = float(pnl_row.get("revenue", 0)) if pnl_row is not None else 0.0
                rev_weight  = float(pnl_row.get("rev_weight", 0)) if pnl_row is not None else 0.0
                total_rev   = prog_rev / rev_weight if rev_weight > 0 else 0.0
                story.append(_data_table(
                    ["Driver", "Program Revenue", "Total Period Revenue", "Rev Weight", "Allocated"],
                    [["Revenue Weighted", _dollar(prog_rev), _dollar(total_rev),
                      f"{rev_weight*100:.2f}%", _dollar(ltype_total)]],
                    col_ratios=[0.25, 0.20, 0.25, 0.15, 0.15],
                ))
            else:
                rows_data = sub[
                    ["cost_center", "activity_driver", "activity_value", "weight", "allocated_cost"]
                ].values.tolist()
                for r in rows_data:
                    r[2] = f"{float(r[2]):,.2f}" if r[2] is not None else ""
                    r[3] = f"{float(r[3]):.2%}" if r[3] is not None else ""
                    r[4] = _dollar(r[4])
                story.append(_data_table(
                    ["Cost Center", "Driver", "Driver Value", "Weight", "Allocated"],
                    rows_data,
                    col_ratios=[0.28, 0.27, 0.18, 0.12, 0.15],
                ))
            story.append(Spacer(1, SPACE_S))

            # Employee detail
            if not labor_employee_df.empty:
                emp_sub = labor_employee_df[labor_employee_df["labor_type"] == ltype].copy()
                if not emp_sub.empty:
                    story.append(Paragraph("Employee Detail", emp_cell_style))
                    story.append(Spacer(1, SPACE_XS))

                    if ltype == "direct_sga":
                        emp_rows = []
                        for _, er in emp_sub.sort_values("allocated_cost", ascending=False).iterrows():
                            emp_rows.append([
                                str(er.get("employee", "")),
                                str(er.get("role", "")),
                                _dollar(er.get("allocated_cost", 0)),
                            ])
                        story.append(_employee_table(
                            ["Employee", "Role", "Allocated"],
                            emp_rows,
                            col_ratios=[0.50, 0.35, 0.15],
                        ))
                    else:
                        emp_rows = []
                        for _, er in emp_sub.sort_values("allocated_cost", ascending=False).iterrows():
                            weight_txt = (
                                f"{float(er.get('weight', 0)):.2%}"
                                if er.get("weight") is not None else ""
                            )
                            emp_rows.append([
                                str(er.get("employee", "")),
                                str(er.get("role", "")),
                                str(er.get("cost_center", "")),
                                str(er.get("activity_driver", "")),
                                weight_txt,
                                _dollar(er.get("allocated_cost", 0)),
                            ])
                        story.append(_employee_table(
                            ["Employee", "Role", "Cost Center", "Driver", "Weight", "Allocated"],
                            emp_rows,
                            col_ratios=[0.22, 0.16, 0.18, 0.20, 0.10, 0.14],
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

            # Build weekly table
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

            # Even split, but Week and Spike are narrower
            n_data_cols = len(wk_cols) + 1  # +1 for rolling avg
            data_col_ratio = (1.0 - 0.15 - 0.10) / n_data_cols  # 0.15 Week, 0.10 Spike
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