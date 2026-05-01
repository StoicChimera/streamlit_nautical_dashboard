from __future__ import annotations

import os
from datetime import datetime as _dt
from typing import Optional

import pandas as pd
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

"""
profitability_report.py
=======================

Builds the full period profitability report PDF.

Pages:
  1  — Cover
  2  — Full profitability summary
  3  — Experiential breakdown
  4  — SCAAS breakdown
  5  — Production breakdown
  6  — Other Fulfillment breakdown
  7  — SG&A category breakdown
  8  — Production activity (3-month comparison)
  9  — WIP Summary
  10 — Warehouse Allocation
  11 — Labor — Direct Hire by program
  12 — Labor — Temp by program
"""


# =====================================================
# Helpers
# =====================================================

def _dollar(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v) if v is not None else ""


def _whole(v) -> str:
    try:
        return f"{float(v):,.0f}"
    except Exception:
        return str(v) if v is not None else ""


def _pct(v) -> str:
    try:
        return f"{float(v) * 100:.1f}%"
    except Exception:
        return str(v) if v is not None else ""


def _safe_float(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _neg_red(val: str, styles) -> Paragraph:
    text_style = styles["Normal"].clone("CellText")
    text_style.fontSize = 8
    text_style.leading = 9
    text_style.textColor = colors.black
    try:
        num = float(str(val).replace("$", "").replace(",", "").replace("%", ""))
        if num < 0:
            return Paragraph(f'<font color="#B00020">{val}</font>', text_style)
    except Exception:
        pass
    return Paragraph(str(val), text_style)


def _base_cell_style(styles, name: str = "BaseCell", font_size: int = 8, leading: int = 9):
    s = styles["Normal"].clone(name)
    s.fontSize = font_size
    s.leading = leading
    return s


def _section_heading(styles, text: str) -> list:
    heading_style = styles["Heading2"].clone(f"H2_{text[:20]}")
    heading_style.fontSize = 11
    return [Paragraph(text, heading_style), Spacer(1, 0.1 * inch)]


def _standard_table(rows, col_widths, header_bg="#003366", header_fg=colors.white,
                    zebra=True, numeric_cols=None, font_size=8):
    if numeric_cols is None:
        numeric_cols = []

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    cmds = [
        ("FONT",           (0, 0), (-1, 0), "Helvetica-Bold", font_size),
        ("BACKGROUND",     (0, 0), (-1, 0), colors.HexColor(header_bg)),
        ("TEXTCOLOR",      (0, 0), (-1, 0), header_fg),
        ("GRID",           (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("FONT",           (0, 1), (-1, -1), "Helvetica", font_size),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",     (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 2),
    ]
    if zebra:
        cmds.append(("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]))
    for idx in numeric_cols:
        cmds.append(("ALIGN", (idx, 0), (idx, -1), "RIGHT"))
    tbl.setStyle(TableStyle(cmds))
    return tbl


# =====================================================
# Main profitability tables
# =====================================================

def _profitability_table(df: pd.DataFrame, styles, title: str) -> list:
    story = []
    story.extend(_section_heading(styles, title))

    if df.empty:
        story.append(Paragraph("No data for this period.", styles["Normal"]))
        return story

    col_map = {
        "customer_program": "Program",
        "revenue":          "Revenue",
        "temp_labor":       "Temp Labor",
        "direct_hire":      "Direct Hire",
        "raw_materials":    "Raw Mat",
        "equipment":        "Equip",
        "commission":       "Comm",
        "freight_storage":  "Freight",
        "applied_wh":       "Whse",
        "gross_profit":     "Gross Profit",
        "gp_margin":        "GP%",
        "applied_sga":      "SGA",
        "net_profit":       "Net Profit",
        "net_margin":       "Net%",
    }

    display_cols = [c for c in col_map if c in df.columns]
    header_row = [col_map[c] for c in display_cols]

    dollar_cols = {
        "revenue", "temp_labor", "direct_hire", "raw_materials",
        "equipment", "commission", "freight_storage", "applied_wh",
        "gross_profit", "applied_sga", "net_profit",
    }
    pct_cols = {"gp_margin", "net_margin"}
    neg_cols = {"gross_profit", "gp_margin", "net_profit", "net_margin"}

    cell_style = _base_cell_style(styles, "ProfitCell", 7, 8)

    def _fmt(col, val):
        if col in dollar_cols:
            return _dollar(val)
        if col in pct_cols:
            return _pct(val)
        return str(val) if val is not None else ""

    rows = [header_row]
    for _, row in df.iterrows():
        out = []
        for col in display_cols:
            fmt = _fmt(col, row[col])
            if col in neg_cols:
                out.append(_neg_red(fmt, styles))
            else:
                out.append(Paragraph(fmt, cell_style))
        rows.append(out)

    totals = []
    for col in display_cols:
        if col == "customer_program":
            totals.append(Paragraph("<b>TOTAL</b>", cell_style))
        elif col in dollar_cols:
            totals.append(Paragraph(_dollar(df[col].sum()), cell_style))
        elif col in pct_cols:
            rev = _safe_float(df["revenue"].sum()) if "revenue" in df.columns else 0
            if col == "gp_margin":
                val = _safe_float(df["gross_profit"].sum()) / rev if rev else 0
            else:
                val = _safe_float(df["net_profit"].sum()) / rev if rev else 0
            totals.append(Paragraph(_pct(val), cell_style))
        else:
            totals.append(Paragraph("", cell_style))
    rows.append(totals)

    first_w = 2.0 * inch
    remaining = (10.8 * inch - first_w) / max(len(display_cols) - 1, 1)
    col_widths = [first_w] + [remaining] * (len(display_cols) - 1)

    numeric_indices = [i for i, c in enumerate(display_cols) if c in dollar_cols | pct_cols]
    t = _standard_table(rows, col_widths=col_widths, numeric_cols=numeric_indices, font_size=7)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eef3ff")),
        ("FONT",       (0, -1), (-1, -1), "Helvetica-Bold", 7),
        ("ALIGN",      (0, 0), (0, -1), "LEFT"),
    ]))

    story.append(t)
    story.append(Spacer(1, 0.2 * inch))
    return story


# =====================================================
# SG&A
# =====================================================

def _sga_breakdown_table(sga_df: pd.DataFrame, styles, title: str) -> list:
    story = []
    story.extend(_section_heading(styles, title))

    if sga_df.empty:
        story.append(Paragraph("No SG&A breakdown available for this period.", styles["Normal"]))
        return story

    cell_style = _base_cell_style(styles, "SgaCell", 8, 9)
    cell_style.textColor = colors.black

    header_style = _base_cell_style(styles, "SgaHeaderCell", 8, 9)
    header_style.textColor = colors.white

    money_style = _base_cell_style(styles, "SgaMoneyCell", 8, 9)
    money_style.textColor = colors.black

    header_map = {
        "category": "Category",
        "Total": "Total",
    }

    def _fmt_month(col):
        try:
            return pd.to_datetime(col).strftime("%b %Y")
        except:
            return col

    display_cols = list(sga_df.columns)
    pretty_headers = [
        header_map.get(col, _fmt_month(col))
        for col in display_cols
    ]

    rows = [[Paragraph(f"<b>{c}</b>", header_style) for c in pretty_headers]]

    for _, row in sga_df.iterrows():
        out = []
        for col in display_cols:
            if col == "category":
                out.append(Paragraph(str(row[col]), cell_style))
            else:
                val = _dollar(row[col])
                out.append(_neg_red(val, styles))
        rows.append(out)

    total_row = []
    for col in display_cols:
        if col == "category":
            total_row.append(Paragraph("<b>TOTAL</b>", cell_style))
        else:
            total_row.append(Paragraph(f"<b>{_dollar(sga_df[col].sum())}</b>", money_style))
    rows.append(total_row)

    first_w = 3.0 * inch
    remaining = (10.8 * inch - first_w) / max(len(display_cols) - 1, 1)
    widths = [first_w] + [remaining] * (len(display_cols) - 1)

    tbl = Table(rows, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONT",           (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("TEXTCOLOR",      (0, 0), (-1, 0), colors.white),
        ("BACKGROUND",     (0, 0), (-1, 0), colors.HexColor("#003366")),
        ("FONT",           (0, 1), (-1, -2), "Helvetica", 8),
        ("FONT",           (0, -1), (-1, -1), "Helvetica-Bold", 8),
        ("BACKGROUND",     (0, -1), (-1, -1), colors.HexColor("#eef3ff")),
        ("TEXTCOLOR",      (0, 1), (-1, -1), colors.black),
        ("GRID",           (0, 0), (-1, -1), 0.35, colors.HexColor("#c9d2dc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.whitesmoke, colors.HexColor("#f7f9fc")]),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",     (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
        ("ALIGN",          (0, 0), (0, -1), "LEFT"),
        ("ALIGN",          (1, 0), (-1, -1), "RIGHT"),
    ]))

    story.append(tbl)
    story.append(Spacer(1, 0.15 * inch))

    if "Total" in sga_df.columns:
        top3 = sga_df.sort_values("Total", ascending=False).head(3)
        summary = ", ".join(
            f"{r['category']} ({_dollar(r['Total'])})"
            for _, r in top3.iterrows()
        )
        note_style = _base_cell_style(styles, "SgaNote", 8, 9)
        note_style.textColor = colors.HexColor("#444444")
        story.append(Paragraph(f"<b>Largest SG&A categories:</b> {summary}", note_style))

    return story


# =====================================================
# Production activity 3 month comparison
# =====================================================

def _production_activity_table(prod_df: pd.DataFrame, styles, title: str) -> list:
    story = []
    story.extend(_section_heading(styles, title))

    if prod_df.empty:
        story.append(Paragraph("No production activity comparison available.", styles["Normal"]))
        return story

    cell_style = _base_cell_style(styles, "Prod3MoCell", 8, 9)
    cell_style.textColor = colors.black

    header_style = _base_cell_style(styles, "Prod3MoHeaderCell", 8, 9)
    header_style.textColor = colors.white

    header_map = {
        "activity_type": "Activity",
        "MoM Δ": "MoM Change",
        "MoM %": "MoM %",
    }

    # dynamic month headers (2026-01 → Jan 2026)
    def _fmt_month(col):
        try:
            return pd.to_datetime(col).strftime("%b %Y")
        except:
            return col

    display_cols = list(prod_df.columns)
    pretty_headers = [
        header_map.get(col, _fmt_month(col))
        for col in display_cols
    ]

    rows = [[Paragraph(f"<b>{c}</b>", header_style) for c in pretty_headers]]

    for _, row in prod_df.iterrows():
        out = []
        for col in display_cols:
            if col == "activity_type":
                out.append(Paragraph(str(row[col]), cell_style))
            elif col == "MoM Δ":
                out.append(_neg_red(f"{_safe_float(row[col]):,.0f}", styles))
            elif col == "MoM %":
                out.append(_neg_red(_pct(row[col]), styles))
            else:
                out.append(Paragraph(_whole(row[col]), cell_style))
        rows.append(out)

    first_w = 2.8 * inch
    remaining = (10.8 * inch - first_w) / max(len(display_cols) - 1, 1)
    widths = [first_w] + [remaining] * (len(display_cols) - 1)

    tbl = Table(rows, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("FONT",           (0, 0), (-1, 0), "Helvetica-Bold", 8),
        ("TEXTCOLOR",      (0, 0), (-1, 0), colors.white),
        ("BACKGROUND",     (0, 0), (-1, 0), colors.HexColor("#003366")),
        ("FONT",           (0, 1), (-1, -1), "Helvetica", 8),
        ("TEXTCOLOR",      (0, 1), (-1, -1), colors.black),
        ("GRID",           (0, 0), (-1, -1), 0.35, colors.HexColor("#c9d2dc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.HexColor("#f7f9fc")]),
        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",     (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",  (0, 0), (-1, -1), 4),
        ("LEFTPADDING",    (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",   (0, 0), (-1, -1), 6),
        ("ALIGN",          (0, 0), (0, -1), "LEFT"),
        ("ALIGN",          (1, 0), (-1, -1), "RIGHT"),
    ]))

    story.append(tbl)
    story.append(Spacer(1, 0.15 * inch))

    note_style = _base_cell_style(styles, "Prod3MoNote", 8, 9)
    note_style.textColor = colors.HexColor("#444444")
    story.append(Paragraph(
        "Production activity compares Demo Kits, OGP Units, and Overwrap Units across the latest three months.",
        note_style
    ))
    return story


# =====================================================
# Existing sections
# =====================================================

def _wip_table(wip: dict, styles) -> list:
    story = []
    story.extend(_section_heading(styles, "WIP Balance Summary"))

    cell_style = _base_cell_style(styles, "WipCell", 8, 9)

    sections = [
        ("Labor — Production WIP",  wip.get("labor_production", pd.DataFrame()), "customer_program", "wip_balance"),
        ("Labor — Fulfillment WIP", wip.get("labor_fulfillment", pd.DataFrame()), "customer_program", "wip_balance"),
        ("Warehouse WIP",           wip.get("warehouse", pd.DataFrame()), "customer_program", "wip_balance"),
        ("Freight WIP",             wip.get("freight", pd.DataFrame()), "customer_program", "wip_balance"),
    ]

    for section_title, df, program_col, balance_col in sections:
        if df.empty:
            story.append(Paragraph(f"<b>{section_title} — $0.00</b>", cell_style))
            story.append(Paragraph("No outstanding WIP.", cell_style))
            story.append(Spacer(1, 0.1 * inch))
            continue

        if program_col not in df.columns or balance_col not in df.columns:
            continue

        grouped = df.groupby(program_col, as_index=False)[balance_col].sum()
        grouped = grouped.sort_values(balance_col, ascending=False)
        total = float(grouped[balance_col].sum())

        story.append(Paragraph(f"<b>{section_title} — {_dollar(total)}</b>", cell_style))
        story.append(Spacer(1, 0.04 * inch))

        rows = [["Program", "WIP Balance"]]
        for _, row in grouped.iterrows():
            rows.append([
                Paragraph(str(row[program_col]), cell_style),
                Paragraph(_dollar(row[balance_col]), cell_style),
            ])

        t = _standard_table(rows, [5.5 * inch, 1.5 * inch], numeric_cols=[1], font_size=8)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6f2ff")),
                               ("TEXTCOLOR",  (0, 0), (-1, 0), colors.HexColor("#003366"))]))
        story.append(t)
        story.append(Spacer(1, 0.15 * inch))

    return story


def _warehouse_table(wh_df: pd.DataFrame, styles) -> list:
    story = []
    story.extend(_section_heading(styles, "Warehouse Allocation"))

    if wh_df.empty:
        story.append(Paragraph("No committed warehouse allocation for this period.", styles["Normal"]))
        return story

    cell_style = _base_cell_style(styles, "WhCell", 8, 9)
    total = float(wh_df["allocation_amount"].sum())
    story.append(Paragraph(f"Total Allocated: {_dollar(total)}", cell_style))
    story.append(Spacer(1, 0.08 * inch))

    story.append(Paragraph("<b>By Bucket</b>", cell_style))
    story.append(Spacer(1, 0.04 * inch))

    bucket_summary = wh_df.groupby(
        ["program_bucket", "category", "cost_type"], as_index=False
    ).agg(
        programs=("customer_program", "nunique"),
        bucket_sqft=("bucket_sqft", "first"),
        allocation_amount=("allocation_amount", "sum"),
    ).sort_values("allocation_amount", ascending=False)

    brows = [["Bucket", "Category", "Cost Type", "Sqft", "Programs", "Allocated"]]
    for _, row in bucket_summary.iterrows():
        brows.append([
            Paragraph(str(row["program_bucket"]), cell_style),
            Paragraph(str(row["category"]), cell_style),
            Paragraph(str(row["cost_type"]), cell_style),
            Paragraph(f"{_safe_float(row['bucket_sqft']):,.0f}", cell_style),
            Paragraph(str(int(row["programs"])), cell_style),
            Paragraph(_dollar(row["allocation_amount"]), cell_style),
        ])

    bt = _standard_table(
        brows,
        [2.2 * inch, 1.0 * inch, 0.8 * inch, 0.8 * inch, 0.7 * inch, 1.0 * inch],
        numeric_cols=[3, 4, 5],
        font_size=8,
    )
    story.append(bt)
    story.append(Spacer(1, 0.15 * inch))

    story.append(Paragraph("<b>By Program</b>", cell_style))
    story.append(Spacer(1, 0.04 * inch))

    prog_summary = wh_df.groupby(["customer_program"], as_index=False)["allocation_amount"].sum()
    prog_summary = prog_summary.sort_values("allocation_amount", ascending=False)

    prows = [["Program", "Allocated"]]
    for _, row in prog_summary.iterrows():
        prows.append([
            Paragraph(str(row["customer_program"]), cell_style),
            Paragraph(_dollar(row["allocation_amount"]), cell_style),
        ])

    pt = _standard_table(prows, [5.5 * inch, 1.5 * inch], numeric_cols=[1], font_size=8)
    story.append(pt)
    return story


def _labor_table(labor_df: pd.DataFrame, employee_df: pd.DataFrame, styles, title: str) -> list:
    story = []
    story.extend(_section_heading(styles, title))

    if labor_df.empty:
        story.append(Paragraph("No data for this period.", styles["Normal"]))
        return story

    cell_style = _base_cell_style(styles, "LaborCell", 8, 9)
    sub_style = _base_cell_style(styles, "LaborSubCell", 7, 8)
    sub_style.textColor = colors.HexColor("#444444")

    label_style = _base_cell_style(styles, "LaborLabel", 7, 8)
    label_style.textColor = colors.HexColor("#003366")

    total = float(labor_df["allocated_cost"].sum())
    story.append(Paragraph(f"Total: {_dollar(total)}", cell_style))
    story.append(Spacer(1, 0.08 * inch))

    summary = labor_df.sort_values(["program", "source_bucket"])
    sum_rows = [["Program", "Cost Center", "Allocated"]]
    for _, row in summary.iterrows():
        sum_rows.append([
            Paragraph(str(row["program"]), cell_style),
            Paragraph(str(row["source_bucket"]), cell_style),
            Paragraph(_dollar(row["allocated_cost"]), cell_style),
        ])

    stbl = _standard_table(sum_rows, [3.5 * inch, 2.0 * inch, 1.5 * inch], numeric_cols=[2], font_size=8)
    story.append(stbl)

    if employee_df.empty:
        return story

    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("<b>Employee Detail by Program</b>", cell_style))
    story.append(Spacer(1, 0.08 * inch))

    ltype = labor_df["labor_type"].iloc[0] if "labor_type" in labor_df.columns else None
    emp_filtered = employee_df[employee_df["labor_type"] == ltype].copy() if ltype else employee_df.copy()

    if emp_filtered.empty:
        story.append(Paragraph("No employee detail available.", sub_style))
        return story

    programs = (
        emp_filtered.groupby("target_program")["allocated_cost"]
        .sum()
        .sort_values(ascending=False)
        .index
        .tolist()
    )

    for program in programs:
        prog_df = emp_filtered[emp_filtered["target_program"] == program].copy()
        prog_total = float(prog_df["allocated_cost"].sum())

        story.append(Paragraph(
            f'<font color="#003366"><b>{program}</b></font>  —  {_dollar(prog_total)}',
            label_style,
        ))
        story.append(Spacer(1, 0.03 * inch))

        emp_rows = [["Employee", "Role", "Cost Center", "Driver", "Weight", "Allocated"]]
        for _, er in prog_df.sort_values("allocated_cost", ascending=False).iterrows():
            weight_txt = f"{float(er.get('weight', 0)):.2%}" if pd.notna(er.get("weight")) else ""
            emp_rows.append([
                Paragraph(str(er.get("employee_name", "")), sub_style),
                Paragraph(str(er.get("role_detail", "")), sub_style),
                Paragraph(str(er.get("source_bucket", "")), sub_style),
                Paragraph(str(er.get("activity_driver", "")), sub_style),
                Paragraph(weight_txt, sub_style),
                Paragraph(_dollar(er.get("allocated_cost", 0)), sub_style),
            ])

        et = _standard_table(
            emp_rows,
            [1.8 * inch, 1.2 * inch, 1.3 * inch, 1.5 * inch, 0.8 * inch, 0.9 * inch],
            header_bg="#e6f2ff",
            header_fg=colors.HexColor("#003366"),
            numeric_cols=[4, 5],
            font_size=7,
        )
        et.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(et)
        story.append(Spacer(1, 0.1 * inch))

    return story


# =====================================================
# Main builder
# =====================================================

def build_profitability_report(
    out_path: str,
    period_label: str,
    full_df: pd.DataFrame,
    experiential_df: pd.DataFrame,
    scaas_df: pd.DataFrame,
    production_df: pd.DataFrame,
    other_df: pd.DataFrame,
    sga_breakdown_df: pd.DataFrame,
    production_activity_3mo_df: pd.DataFrame,
    wip: dict,
    warehouse_df: pd.DataFrame,
    direct_hire_df: pd.DataFrame,
    temp_df: pd.DataFrame,
    employee_df: pd.DataFrame | None = None,
    logo_path: Optional[str] = None,
) -> str:
    styles = getSampleStyleSheet()
    ts = _dt.now().strftime("%Y-%m-%d %H:%M")

    doc = SimpleDocTemplate(
        out_path,
        pagesize=landscape(LETTER),
        leftMargin=0.5 * inch,
        rightMargin=0.5 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )

    story = []

    title_style = ParagraphStyle(
        "CoverTitle",
        parent=styles["Title"],
        fontSize=28,
        leading=34,
        textColor=colors.HexColor("#003366"),
        alignment=TA_LEFT,
    )
    sub_style = ParagraphStyle(
        "CoverSub",
        parent=styles["Normal"],
        fontSize=12,
        textColor=colors.HexColor("#555555"),
        alignment=TA_LEFT,
    )
    ts_style = ParagraphStyle(
        "CoverTS",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#999999"),
        alignment=TA_LEFT,
    )

    # ---- Cover ----
    if logo_path and os.path.exists(logo_path):
        logo = Image(logo_path)
        logo._restrictSize(2.0 * inch, 1.2 * inch)
        logo.hAlign = "LEFT"
        story.append(logo)
        story.append(Spacer(1, 0.3 * inch))

    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#003366")))
    story.append(Spacer(1, 0.4 * inch))
    story.append(Paragraph("Program Profitability Report", title_style))
    story.append(Spacer(1, 0.15 * inch))
    story.append(Paragraph(period_label, sub_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(f"Generated: {ts}", ts_style))
    story.append(Spacer(1, 0.3 * inch))

    if not full_df.empty:
        total_rev = _safe_float(full_df["revenue"].sum())
        total_gp = _safe_float(full_df["gross_profit"].sum())
        total_net = _safe_float(full_df["net_profit"].sum())

        metric_style = _base_cell_style(styles, "MetricStyle", 11, 13)
        metrics = [
            ["Total Revenue", _dollar(total_rev)],
            ["Total GP", _dollar(total_gp)],
            ["GP Margin", _pct(total_gp / total_rev) if total_rev else "—"],
            ["Total Net", _dollar(total_net)],
            ["Net Margin", _pct(total_net / total_rev) if total_rev else "—"],
            ["Programs", str(full_df["customer_program"].nunique())],
        ]

        mt = Table(
            [[Paragraph(f"<b>{m[0]}</b>", metric_style), Paragraph(m[1], metric_style)] for m in metrics],
            colWidths=[3.0 * inch, 2.0 * inch],
            hAlign="LEFT",
        )
        mt.setStyle(TableStyle([
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.lightgrey),
        ]))
        story.append(mt)

    story.append(PageBreak())

    # ---- Core profitability ----
    story.extend(_profitability_table(full_df, styles, f"Full Program Summary — {period_label}"))
    story.append(PageBreak())

    story.extend(_profitability_table(experiential_df, styles, f"Experiential Programs — {period_label}"))
    story.append(PageBreak())

    story.extend(_profitability_table(scaas_df, styles, f"SCAAS Programs — {period_label}"))
    story.append(PageBreak())

    story.extend(_profitability_table(production_df, styles, f"Production Programs — {period_label}"))
    story.append(PageBreak())

    story.extend(_profitability_table(other_df, styles, f"Other Fulfillment Programs — {period_label}"))
    story.append(PageBreak())

    # ---- New sections ----
    story.extend(_sga_breakdown_table(sga_breakdown_df, styles, f"SG&A Category Breakdown — {period_label}"))
    story.append(PageBreak())

    story.extend(_production_activity_table(
        production_activity_3mo_df,
        styles,
        f"Production Activity — 3 Month Comparison ({period_label})"
    ))
    story.append(PageBreak())

    # ---- WIP / Warehouse ----
    story.extend(_wip_table(wip, styles))
    story.append(PageBreak())

    story.extend(_warehouse_table(warehouse_df, styles))
    story.append(PageBreak())

    # ---- Labor ----
    emp = employee_df if employee_df is not None else pd.DataFrame()

    dh = direct_hire_df[direct_hire_df["labor_type"] == "direct_cogs"].copy() if not direct_hire_df.empty else pd.DataFrame()
    dh_emp = emp[emp["labor_type"] == "direct_cogs"].copy() if not emp.empty else pd.DataFrame()
    story.extend(_labor_table(dh, dh_emp, styles, "Labor — Direct Hire by Program"))
    story.append(PageBreak())

    tmp = temp_df[temp_df["labor_type"] == "temp"].copy() if not temp_df.empty else pd.DataFrame()
    tmp_emp = emp[emp["labor_type"] == "temp"].copy() if not emp.empty else pd.DataFrame()
    story.extend(_labor_table(tmp, tmp_emp, styles, "Labor — Temp by Program"))

    # ---- Footer ----
    footer_style = ParagraphStyle(
        "Footer",
        parent=styles["Normal"],
        fontSize=8,
        textColor=colors.HexColor("#999999"),
        alignment=TA_CENTER,
    )
    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey,
                            spaceBefore=0, spaceAfter=0.1 * inch))
    story.append(Paragraph(f"Nautical Manufacturing & Fulfillment LLC — Confidential — {ts}", footer_style))

    doc.build(story)
    return os.path.abspath(out_path)