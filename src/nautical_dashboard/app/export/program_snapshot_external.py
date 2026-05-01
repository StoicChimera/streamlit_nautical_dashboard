from __future__ import annotations

import os
from datetime import datetime as _dt
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import landscape, LETTER
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

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
PROGRAM = "Life Time"
PERIOD = "2026-02"
PERIOD_LABEL = "February 2026"

BASE_DIR = Path(__file__).resolve().parent
OUT_PATH = BASE_DIR / "Life_Time_External_GP_Only.pdf"
LOGO_PATH = BASE_DIR / "logo_nautical.png"

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

engine = create_engine(SUPABASE_CONN, pool_pre_ping=True)


# -----------------------------------------------------------------------------
# FORMAT HELPERS
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# DATA LOADERS
# -----------------------------------------------------------------------------
def load_pnl_row(program: str, period: str) -> pd.Series:
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


def load_labor_summary(program: str, period: str) -> pd.DataFrame:
    """
    Direct + temp only.
    Remove driver value and weight from the output because they are noisy
    and unreliable for this external one-off.
    """
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


def load_labor_employee_detail(program: str, period: str) -> pd.DataFrame:
    """
    Direct + temp employee detail only.
    Also strips out weight from the rendered report.
    """
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
            "labor_type",
            "employee",
            "role",
            "cost_center",
            "activity_driver",
            "allocated_cost",
        ])


def load_warehouse(program: str, period: str) -> pd.DataFrame:
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
    df = pd.read_sql(sql, engine, params={"period": period, "program": program})
    print(f"[DEBUG] warehouse rows: {len(df)}")
    if not df.empty:
        print(df.to_string(index=False))
    return df

def load_freight(program: str, period: str) -> pd.DataFrame:
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
            "invoice_num", "bill_date", "line_description", "amount", "match_type"
        ])


def load_wip(program: str) -> dict:
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
            "accrual_period", "cost_center", "units_produced", "units_consumed", "wip_balance"
        ])

    try:
        wh_df = pd.read_sql(warehouse_sql, engine, params={"program": program})
    except Exception:
        wh_df = pd.DataFrame(columns=["period", "program_bucket", "wip_balance"])

    return {
        "labor_production": labor_df,
        "warehouse": wh_df,
    }


# -----------------------------------------------------------------------------
# PDF BUILDER
# -----------------------------------------------------------------------------
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
) -> str:
    styles = getSampleStyleSheet()
    ts = _dt.now().strftime("%Y-%m-%d %H:%M")
    story = []

    title_style = ParagraphStyle(
        "SnapTitle",
        parent=styles["Title"],
        fontSize=22,
        leading=26,
        textColor=colors.HexColor("#003366"),
        alignment=TA_LEFT,
    )
    sub_style = ParagraphStyle(
        "SnapSub",
        parent=styles["Normal"],
        fontSize=11,
        textColor=colors.HexColor("#555555"),
    )
    ts_style = ParagraphStyle(
        "SnapTS",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#999999"),
    )
    h2_style = styles["Heading2"]

    cell_style = styles["Normal"].clone("SnapCell")
    cell_style.fontSize = 8
    cell_style.leading = 10

    emp_style = styles["Normal"].clone("EmpCell")
    emp_style.fontSize = 7
    emp_style.leading = 8
    emp_style.textColor = colors.HexColor("#444444")

    label_style = styles["Normal"].clone("EmpLabel")
    label_style.fontSize = 8
    label_style.leading = 9
    label_style.textColor = colors.HexColor("#003366")

    def _section(title: str):
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph(f"<b>{title}</b>", h2_style))
        story.append(HRFlowable(
            width="100%",
            thickness=0.5,
            color=colors.HexColor("#003366"),
            spaceBefore=0,
            spaceAfter=0.08 * inch,
        ))

    def _simple_table(headers, rows_data, col_widths):
        header_cells = [
            Paragraph(f'<font color="white"><b>{h}</b></font>', cell_style)
            for h in headers
        ]
        rows = [header_cells]

        for r in rows_data:
            rows.append([
                Paragraph(str(v) if v is not None else "", cell_style)
                for v in r
            ])

        t = Table(rows, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#003366")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ]))
        return t

    # -----------------------------------------------------------------
    # COVER
    # -----------------------------------------------------------------
    if logo_path and os.path.exists(logo_path):
        logo = Image(logo_path)
        logo._restrictSize(1.8 * inch, 1.0 * inch)
        logo.hAlign = "LEFT"
        story.append(logo)
        story.append(Spacer(1, 0.2 * inch))

    story.append(HRFlowable(
        width="100%",
        thickness=1.5,
        color=colors.HexColor("#003366"),
        spaceBefore=0,
        spaceAfter=0,
    ))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("Program Snapshot", title_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(
        program,
        ParagraphStyle(
            "ProgramName",
            parent=styles["Title"],
            fontSize=18,
            textColor=colors.HexColor("#1f77b4"),
        ),
    ))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(period_label, sub_style))
    story.append(Paragraph(f"Generated: {ts}", ts_style))
    story.append(Spacer(1, 0.3 * inch))

    # -----------------------------------------------------------------
    # P&L - GP ONLY
    # -----------------------------------------------------------------
    _section("Program P&L")

    pnl_items = [
        ("Revenue", _dollar(pnl_row.get("revenue", 0)), False),
        ("Temp Labor", _dollar(pnl_row.get("temp_labor", 0)), False),
        ("Direct Hire", _dollar(pnl_row.get("direct_hire", 0)), False),
        ("Raw Materials", _dollar(pnl_row.get("raw_materials", 0)), False),
        ("Equipment", _dollar(pnl_row.get("equipment", 0)), False),
        ("Commission", _dollar(pnl_row.get("commission", 0)), False),
        ("Freight & Storage", _dollar(pnl_row.get("freight_storage", 0)), False),
        ("Warehouse", _dollar(pnl_row.get("applied_wh", 0)), False),
        ("Gross Profit", _dollar(pnl_row.get("gross_profit", 0)), True),
        ("GP Margin", _pct(pnl_row.get("gp_margin", 0)), True),
    ]

    pnl_rows = []
    for label, val, is_key in pnl_items:
        try:
            num = float(val.replace("$", "").replace(",", "").replace("%", ""))
            color_str = "#B00020" if num < 0 else ("#000000" if not is_key else "#003366")
        except Exception:
            color_str = "#003366" if is_key else "#000000"

        font = "Helvetica-Bold" if is_key else "Helvetica"
        label_p = Paragraph(f'<font name="{font}">{label}</font>', cell_style)
        val_p = Paragraph(f'<font name="{font}" color="{color_str}">{val}</font>', cell_style)
        pnl_rows.append([label_p, val_p])

    pt = Table(pnl_rows, colWidths=[3.5 * inch, 2.0 * inch], hAlign="LEFT")
    pt.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEBELOW", (0, 7), (-1, 7), 0.5, colors.HexColor("#003366")),
    ]))
    story.append(pt)

    story.append(PageBreak())

    # -----------------------------------------------------------------
    # LABOR - NO DRIVER VALUE / NO WEIGHT / NO SGA
    # -----------------------------------------------------------------
    _section("Labor Allocation")

    if labor_df.empty:
        story.append(Paragraph("No labor allocated to this program for the period.", cell_style))
    else:
        for ltype, label in [("direct_cogs", "Direct Hire"), ("temp", "Temp")]:
            sub = labor_df[labor_df["labor_type"] == ltype].copy()
            if sub.empty:
                continue

            total_alloc = float(sub["allocated_cost"].sum())
            story.append(Paragraph(f"<b>{label} — {_dollar(total_alloc)}</b>", label_style))
            story.append(Spacer(1, 0.05 * inch))

            summary_rows = sub[["cost_center", "activity_driver", "allocated_cost"]].copy()
            summary_rows["allocated_cost"] = summary_rows["allocated_cost"].map(_dollar)

            story.append(_simple_table(
                ["Cost Center", "Driver", "Allocated"],
                summary_rows.values.tolist(),
                [2.2 * inch, 2.4 * inch, 1.3 * inch],
            ))
            story.append(Spacer(1, 0.08 * inch))

            emp_sub = labor_employee_df[labor_employee_df["labor_type"] == ltype].copy()
            if not emp_sub.empty:
                story.append(Paragraph("Employee Detail", emp_style))
                story.append(Spacer(1, 0.03 * inch))

                emp_rows = [["Employee", "Role", "Cost Center", "Driver", "Allocated"]]
                for _, er in emp_sub.sort_values("allocated_cost", ascending=False).iterrows():
                    emp_rows.append([
                        Paragraph(str(er.get("employee", "")), emp_style),
                        Paragraph(str(er.get("role", "")), emp_style),
                        Paragraph(str(er.get("cost_center", "")), emp_style),
                        Paragraph(str(er.get("activity_driver", "")), emp_style),
                        Paragraph(_dollar(er.get("allocated_cost", 0)), emp_style),
                    ])

                et = Table(
                    emp_rows,
                    colWidths=[2.0 * inch, 1.4 * inch, 1.5 * inch, 2.0 * inch, 1.0 * inch],
                    repeatRows=1,
                )
                et.setStyle(TableStyle([
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f2ff")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#003366")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
                    ("FONT", (0, 1), (-1, -1), "Helvetica", 7),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (4, 0), (4, -1), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 1),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ]))
                story.append(et)

            story.append(Spacer(1, 0.15 * inch))

    story.append(PageBreak())

    # -----------------------------------------------------------------
    # WAREHOUSE - KEEP THIS
    # -----------------------------------------------------------------
    # ---- Warehouse ----
    story.append(Spacer(1, 0.2 * inch))
    _section("Warehouse Allocation")

    if not warehouse_df.empty:
        total_wh = float(warehouse_df["allocation_amount"].sum())
        story.append(Paragraph(f"Total: {_dollar(total_wh)}", cell_style))
        story.append(Spacer(1, 0.06 * inch))

        wh_style = styles["Normal"].clone("WhCell")
        wh_style.fontSize = 7
        wh_style.leading = 8

        rows = [[
            Paragraph('<font color="white"><b>Bucket</b></font>', wh_style),
            Paragraph('<font color="white"><b>Category</b></font>', wh_style),
            Paragraph('<font color="white"><b>Cost Type</b></font>', wh_style),
            Paragraph('<font color="white"><b>Driver</b></font>', wh_style),
            Paragraph('<font color="white"><b>Alloc %</b></font>', wh_style),
            Paragraph('<font color="white"><b>Allocated</b></font>', wh_style),
        ]]

        for _, r in warehouse_df.iterrows():
            rows.append([
                Paragraph(str(r["program_bucket"]), wh_style),
                Paragraph(str(r["category"]), wh_style),
                Paragraph(str(r["cost_type"]), wh_style),
                Paragraph(str(r["driver_type"]), wh_style),
                Paragraph(f'{float(r["allocation_pct"]):.2%}', wh_style),
                Paragraph(_dollar(r["allocation_amount"]), wh_style),
            ])

        t = Table(
            rows,
            colWidths=[2.0*inch, 1.1*inch, 0.9*inch, 2.0*inch, 0.9*inch, 1.1*inch],
            repeatRows=1,
        )

        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#003366")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ALIGN", (4, 1), (5, -1), "RIGHT"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))

        story.append(t)

    else:
        story.append(Paragraph("No warehouse cost allocated to this program for the period.", cell_style))

    story.append(PageBreak())
    

    # -----------------------------------------------------------------
    # FREIGHT
    # -----------------------------------------------------------------
    _section("Freight Lines")

    if not freight_df.empty:
        total_fr = float(freight_df["amount"].sum())
        story.append(Paragraph(f"Total: {_dollar(total_fr)}", cell_style))
        story.append(Spacer(1, 0.06 * inch))

        rows_data = freight_df[["invoice_num", "bill_date", "line_description", "amount", "match_type"]].copy()
        rows_data["bill_date"] = pd.to_datetime(rows_data["bill_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        rows_data["amount"] = rows_data["amount"].map(_dollar)

        story.append(_simple_table(
            ["Invoice", "Bill Date", "Description", "Amount", "Match Type"],
            rows_data.values.tolist(),
            [1.2 * inch, 1.0 * inch, 3.2 * inch, 1.0 * inch, 1.0 * inch],
        ))
    else:
        story.append(Paragraph("No matched freight lines for this program in the period.", cell_style))

    story.append(PageBreak())

    # -----------------------------------------------------------------
    # WIP
    # -----------------------------------------------------------------
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
        story.append(Spacer(1, 0.1 * inch))

        if not lp.empty:
            story.append(Paragraph(f"<b>Labor Production WIP — {_dollar(total_lp)}</b>", cell_style))
            story.append(Spacer(1, 0.04 * inch))
            rows_data = lp[["accrual_period", "cost_center", "units_produced", "units_consumed", "wip_balance"]].copy()
            rows_data["wip_balance"] = rows_data["wip_balance"].map(_dollar)
            story.append(_simple_table(
                ["Period", "Cost Center", "Units Produced", "Units Consumed", "WIP Balance"],
                rows_data.values.tolist(),
                [1.2 * inch, 1.5 * inch, 1.5 * inch, 1.5 * inch, 1.5 * inch],
            ))
            story.append(Spacer(1, 0.1 * inch))

        if not wh.empty:
            story.append(Paragraph(f"<b>Warehouse WIP — {_dollar(total_wh_wip)}</b>", cell_style))
            story.append(Spacer(1, 0.04 * inch))
            rows_data = wh[["period", "program_bucket", "wip_balance"]].copy()
            rows_data["wip_balance"] = rows_data["wip_balance"].map(_dollar)
            story.append(_simple_table(
                ["Period", "Bucket", "WIP Balance"],
                rows_data.values.tolist(),
                [1.5 * inch, 3.0 * inch, 1.5 * inch],
            ))

    # -----------------------------------------------------------------
    # FOOTER
    # -----------------------------------------------------------------
    footer_style = ParagraphStyle(
        "SnapFooter",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#999999"),
        alignment=TA_CENTER,
    )
    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(
        width="100%",
        thickness=0.5,
        color=colors.lightgrey,
        spaceBefore=0,
        spaceAfter=0.1 * inch,
    ))
    story.append(Paragraph(
        f"Nautical Manufacturing & Fulfillment LLC — Confidential — {ts}",
        footer_style,
    ))

    doc = SimpleDocTemplate(
        out_path,
        pagesize=landscape(LETTER),
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )
    doc.build(story)
    return os.path.abspath(out_path)


# -----------------------------------------------------------------------------
# RUN
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    pnl_row = load_pnl_row(PROGRAM, PERIOD)
    labor_df = load_labor_summary(PROGRAM, PERIOD)
    labor_employee_df = load_labor_employee_detail(PROGRAM, PERIOD)
    warehouse_df = load_warehouse(PROGRAM, PERIOD)
    freight_df = load_freight(PROGRAM, PERIOD)
    wip = load_wip(PROGRAM)
    print(f"[DEBUG] warehouse_df empty? {warehouse_df.empty}")
    print(f"[DEBUG] warehouse_df columns: {warehouse_df.columns.tolist()}")
    if not warehouse_df.empty:
        print(warehouse_df.to_string(index=False))
    pdf_path = build_program_snapshot_external(
        out_path=str(OUT_PATH),
        program=PROGRAM,
        period_label=PERIOD_LABEL,
        pnl_row=pnl_row,
        labor_df=labor_df,
        labor_employee_df=labor_employee_df,
        warehouse_df=warehouse_df,
        freight_df=freight_df,
        wip=wip,
        logo_path=str(LOGO_PATH) if LOGO_PATH.exists() else None,
    )

    print(f"Built: {pdf_path}")