"""
program_snapshot.py
===================
Builds a single-program PDF snapshot.

Pages:
  1 — Cover + P&L
  2 — Labor detail
  3 — Warehouse detail
  4 — Freight lines
  5 — WIP balance
"""

from __future__ import annotations

import os
from datetime import datetime as _dt
from typing import Optional

import pandas as pd
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
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import tempfile


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
    labor_weekly_df: Optional[pd.DataFrame] = None,   # NEW
) -> str:
    
    if labor_employee_df is None:
        labor_employee_df = pd.DataFrame()
    if labor_weekly_df is None:
        labor_weekly_df = pd.DataFrame()

    styles = getSampleStyleSheet()
    ts = _dt.now().strftime("%Y-%m-%d %H:%M")
    story = []

    title_style = ParagraphStyle(
        "SnapTitle", parent=styles["Title"],
        fontSize=22, leading=26,
        textColor=colors.HexColor("#003366"),
        alignment=TA_LEFT,
    )
    sub_style = ParagraphStyle(
        "SnapSub", parent=styles["Normal"],
        fontSize=11, textColor=colors.HexColor("#555555"),
    )
    ts_style = ParagraphStyle(
        "SnapTS", parent=styles["Normal"],
        fontSize=9, textColor=colors.HexColor("#999999"),
    )
    h2_style = styles["Heading2"]
    cell_style = styles["Normal"].clone("SnapCell")
    cell_style.fontSize = 8
    cell_style.leading = 10

    def _section(title):
        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph(f"<b>{title}</b>", h2_style))
        story.append(HRFlowable(width="100%", thickness=0.5,
                                color=colors.HexColor("#003366"),
                                spaceBefore=0, spaceAfter=0.08 * inch))

    def _simple_table(headers, rows_data, col_widths):
        rows = [headers]
        for r in rows_data:
            rows.append([Paragraph(str(v) if v is not None else "", cell_style) for v in r])
        t = Table(rows, colWidths=col_widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONT",       (0, 0), (-1, 0),  "Helvetica-Bold", 8),
            ("BACKGROUND", (0, 0), (-1, 0),  colors.HexColor("#003366")),
            ("TEXTCOLOR",  (0, 0), (-1, 0),  colors.white),
            ("GRID",       (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.whitesmoke, colors.white]),
            ("FONT",       (0, 1), (-1, -1), "Helvetica", 8),
            ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return t

    def _render_weekly_chart_to_image(weekly_df: pd.DataFrame, out_dir: str) -> Optional[str]:
        """
        Renders the weekly trend chart to a PNG and returns the path.
        Returns None if weekly_df is empty.
        """
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

        fig, ax = plt.subplots(figsize=(9, 3.5))

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

        ax.set_xlabel("Week")
        ax.set_ylabel("Labor Cost")
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.15),
                ncol=4, fontsize=8, frameon=False)
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate(rotation=0)
        plt.tight_layout()

        chart_path = os.path.join(out_dir, f"weekly_chart_{_dt.now().timestamp()}.png")
        fig.savefig(chart_path, dpi=120, bbox_inches="tight")
        plt.close(fig)
        return chart_path

    # ---- Cover ----
    if logo_path and os.path.exists(logo_path):
        logo = Image(logo_path)
        logo._restrictSize(1.8 * inch, 1.0 * inch)
        logo.hAlign = "LEFT"
        story.append(logo)
        story.append(Spacer(1, 0.2 * inch))

    story.append(HRFlowable(width="100%", thickness=1.5,
                            color=colors.HexColor("#003366"),
                            spaceBefore=0, spaceAfter=0))
    story.append(Spacer(1, 0.3 * inch))
    story.append(Paragraph("Program Snapshot", title_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(program, ParagraphStyle(
        "ProgramName", parent=styles["Title"],
        fontSize=18, textColor=colors.HexColor("#1f77b4"),
    )))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(period_label, sub_style))
    story.append(Paragraph(f"Generated: {ts}", ts_style))
    story.append(Spacer(1, 0.3 * inch))

    # ---- P&L ----
    _section("Program P&L")

    if pnl_row is not None:
        rev = float(pnl_row.get("revenue", 0))
        pnl_items = [
            ("Revenue",                  _dollar(pnl_row.get("revenue", 0)),      False),
            ("Temp Labor",               _dollar(pnl_row.get("temp_labor", 0)),   False),
            ("Direct Hire",              _dollar(pnl_row.get("direct_hire", 0)),  False),
            ("Raw Materials",            _dollar(pnl_row.get("raw_materials", 0)),False),
            ("Equipment",                _dollar(pnl_row.get("equipment", 0)),    False),
            ("Commission",               _dollar(pnl_row.get("commission", 0)),   False),
            ("Freight & Storage",        _dollar(pnl_row.get("freight_storage", 0)), False),
            ("Warehouse",                _dollar(pnl_row.get("applied_wh", 0)),   False),
            ("Gross Profit",             _dollar(pnl_row.get("gross_profit", 0)), True),
            ("GP Margin",                _pct(pnl_row.get("gp_margin", 0)),       True),
            ("Applied SGA",              _dollar(pnl_row.get("applied_sga", 0)),  False),
            ("Net Profit",               _dollar(pnl_row.get("net_profit", 0)),   True),
            ("Net Margin",               _pct(pnl_row.get("net_margin", 0)),      True),
        ]

        pnl_rows = []
        for label, val, is_key in pnl_items:
            try:
                num = float(val.replace("$","").replace(",","").replace("%",""))
                color_str = "#B00020" if num < 0 else ("#000000" if not is_key else "#003366")
            except Exception:
                color_str = "#003366" if is_key else "#000000"

            font = "Helvetica-Bold" if is_key else "Helvetica"
            label_p = Paragraph(f'<font name="{font}">{label}</font>', cell_style)
            val_p   = Paragraph(f'<font name="{font}" color="{color_str}">{val}</font>', cell_style)
            pnl_rows.append([label_p, val_p])

        pt = Table(pnl_rows, colWidths=[3.5*inch, 2.0*inch], hAlign="LEFT")
        pt.setStyle(TableStyle([
            ("ALIGN",  (1, 0), (1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 7), (-1, 7), 0.5, colors.HexColor("#003366")),
            ("LINEBELOW", (0, 10), (-1, 10), 0.5, colors.HexColor("#003366")),
        ]))
        story.append(pt)

    story.append(PageBreak())

    # ---- Labor ----
    _section("Labor Allocation")

    if labor_df.empty:
        story.append(Paragraph("No labor allocated to this program for the period.", cell_style))
    else:
        sub_style = styles["Normal"].clone("EmpCell")
        sub_style.fontSize = 7
        sub_style.leading = 8
        sub_style.textColor = colors.HexColor("#444444")

        label_style = styles["Normal"].clone("EmpLabel")
        label_style.fontSize = 8
        label_style.leading = 9
        label_style.textColor = colors.HexColor("#003366")

        for ltype, label in [("direct_cogs", "Direct Hire"), ("temp", "Temp"), ("direct_sga", "SGA")]:
            sub = labor_df[labor_df["labor_type"] == ltype].copy()
            if sub.empty:
                continue

            ltype_total = float(sub["allocated_cost"].sum())

            # Section label
            story.append(Paragraph(f"<b>{label} — {_dollar(ltype_total)}</b>", label_style))
            story.append(Spacer(1, 0.05 * inch))

            # Cost center summary
            if ltype == "direct_sga":
                prog_rev     = float(pnl_row.get("revenue", 0)) if pnl_row is not None else 0.0
                rev_weight   = float(pnl_row.get("rev_weight", 0)) if pnl_row is not None else 0.0
                total_rev    = prog_rev / rev_weight if rev_weight > 0 else 0.0
                total_alloc  = float(sub["allocated_cost"].sum())
                story.append(_simple_table(
                    ["Driver", "Program Revenue", "Total Period Revenue", "Rev Weight", "Allocated"],
                    [["Revenue Weighted",
                      f"${prog_rev:,.2f}",
                      f"${total_rev:,.2f}",
                      f"{rev_weight*100:.2f}%",
                      _dollar(total_alloc)]],
                    [1.8*inch, 1.6*inch, 1.8*inch, 0.9*inch, 1.2*inch],
                ))
            else:
                rows_data = sub[["cost_center","activity_driver","activity_value","weight","allocated_cost"]].values.tolist()
                for r in rows_data:
                    r[2] = f"{float(r[2]):,.2f}" if r[2] is not None else ""
                    r[3] = f"{float(r[3]):.2%}" if r[3] is not None else ""
                    r[4] = _dollar(r[4])
                story.append(_simple_table(
                    ["Cost Center", "Driver", "Driver Value", "Weight", "Allocated"],
                    rows_data,
                    [2.0*inch, 2.0*inch, 1.2*inch, 0.9*inch, 1.2*inch],
                ))
            story.append(Spacer(1, 0.08 * inch))

            # Employee detail
            if not labor_employee_df.empty:
                emp_sub = labor_employee_df[labor_employee_df["labor_type"] == ltype].copy()
                if not emp_sub.empty:
                    story.append(Paragraph("Employee Detail", sub_style))
                    story.append(Spacer(1, 0.03 * inch))

                    if ltype == "direct_sga":
                        emp_rows = [["Employee", "Role", "Allocated"]]
                        for _, er in emp_sub.sort_values("allocated_cost", ascending=False).iterrows():
                            emp_rows.append([
                                Paragraph(str(er.get("employee", "")), sub_style),
                                Paragraph(str(er.get("role", "")),     sub_style),
                                Paragraph(_dollar(er.get("allocated_cost", 0)), sub_style),
                            ])
                        et = Table(
                            emp_rows,
                            colWidths=[4.0*inch, 2.5*inch, 1.5*inch],
                            repeatRows=1,
                        )
                    else:
                        emp_rows = [["Employee", "Role", "Cost Center", "Driver", "Weight", "Allocated"]]
                        for _, er in emp_sub.sort_values("allocated_cost", ascending=False).iterrows():
                            emp_rows.append([
                                Paragraph(str(er.get("employee", "")),        sub_style),
                                Paragraph(str(er.get("role", "")),            sub_style),
                                Paragraph(str(er.get("cost_center", "")),     sub_style),
                                Paragraph(str(er.get("activity_driver", "")), sub_style),
                                Paragraph(f"{float(er.get('weight', 0)):.2%}" if er.get("weight") is not None else "", sub_style),
                                Paragraph(_dollar(er.get("allocated_cost", 0)), sub_style),
                            ])
                        et = Table(
                            emp_rows,
                            colWidths=[1.8*inch, 1.2*inch, 1.3*inch, 1.5*inch, 0.8*inch, 0.9*inch],
                            repeatRows=1,
                        )

                    et.setStyle(TableStyle([
                        ("FONT",           (0, 0), (-1, 0),  "Helvetica-Bold", 7),
                        ("BACKGROUND",     (0, 0), (-1, 0),  colors.HexColor("#e6f2ff")),
                        ("TEXTCOLOR",      (0, 0), (-1, 0),  colors.HexColor("#003366")),
                        ("GRID",           (0, 0), (-1, -1), 0.25, colors.lightgrey),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafafa")]),
                        ("FONT",           (0, 1), (-1, -1), "Helvetica", 7),
                        ("VALIGN",         (0, 0), (-1, -1), "MIDDLE"),
                        ("ALIGN",          (5, 0), (5, -1),  "RIGHT"),
                        ("TOPPADDING",     (0, 0), (-1, -1), 1),
                        ("BOTTOMPADDING",  (0, 0), (-1, -1), 1),
                        ("LEFTPADDING",    (0, 0), (-1, -1), 3),
                    ]))
                    story.append(et)

            story.append(Spacer(1, 0.15 * inch))

    # ── Week-over-Week trend ──────────────────────────────────────
    story.append(Spacer(1, 0.2 * inch))
    story.append(Paragraph("<b>Week-over-Week Labor Trend</b>", label_style))
    story.append(Spacer(1, 0.04 * inch))

    caption_style = styles["Normal"].clone("WeeklyCaption")
    caption_style.fontSize = 7
    caption_style.textColor = colors.HexColor("#666666")
    story.append(Paragraph(
        "Shows only employees directly allocated to this program via the labor review process. "
        "Bucket-allocated labor (Demo, OGP, Overwrap, Operations, etc.) is excluded. "
        "Week labels follow ISO weeks (Monday start).",
        caption_style,
    ))
    story.append(Spacer(1, 0.08 * inch))

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
                chart_img._restrictSize(9.5 * inch, 3.5 * inch)
                chart_img.hAlign = "LEFT"
                story.append(chart_img)
                story.append(Spacer(1, 0.1 * inch))

        # Data table
        trend = labor_weekly_df.pivot_table(
            index="week_start",
            columns="labor_type",
            values="weekly_cost",
            aggfunc="sum",
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

        wt = _simple_table(
            headers,
            rows_data,
            [1.0 * inch] + [1.1 * inch] * len(wk_cols) + [1.2 * inch, 0.7 * inch],
        )
        story.append(wt)

    story.append(PageBreak())

    # ---- Warehouse ----
    _section("Warehouse Allocation")

    if not warehouse_df.empty:
        total_wh = float(warehouse_df["allocation_amount"].sum())
        story.append(Paragraph(f"Total: {_dollar(total_wh)}", cell_style))
        story.append(Spacer(1, 0.06 * inch))
        rows_data = warehouse_df[
            ["program_bucket","category","cost_type","driver_type","allocation_pct","allocation_amount"]
        ].copy()
        rows_data["allocation_pct"]    = rows_data["allocation_pct"].map(lambda x: f"{float(x):.2%}")
        rows_data["allocation_amount"] = rows_data["allocation_amount"].map(_dollar)
        story.append(_simple_table(
            ["Bucket","Category","Cost Type","Driver","Alloc %","Allocated"],
            rows_data.values.tolist(),
            [2.0*inch, 1.0*inch, 0.9*inch, 1.5*inch, 0.8*inch, 1.0*inch],
        ))
    else:
        story.append(Paragraph("No warehouse cost allocated to this program for the period.", cell_style))

    story.append(PageBreak())

    # ---- Freight ----
    _section("Freight Lines")

    if not freight_df.empty:
        total_fr = float(freight_df["amount"].sum())
        story.append(Paragraph(f"Total: {_dollar(total_fr)}", cell_style))
        story.append(Spacer(1, 0.06 * inch))
        rows_data = freight_df[["invoice_num","bill_date","line_description","amount","match_type"]].copy()
        rows_data["bill_date"] = pd.to_datetime(rows_data["bill_date"], errors="coerce").dt.strftime("%Y-%m-%d")
        rows_data["amount"]    = rows_data["amount"].map(_dollar)
        story.append(_simple_table(
            ["Invoice","Bill Date","Description","Amount","Match Type"],
            rows_data.values.tolist(),
            [1.2*inch, 1.0*inch, 3.2*inch, 1.0*inch, 1.0*inch],
        ))
    else:
        story.append(Paragraph("No matched freight lines for this program in the period.", cell_style))

    story.append(PageBreak())

    # ---- WIP ----
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
            rows_data = lp[["accrual_period","cost_center","units_produced","units_consumed","wip_balance"]].copy()
            rows_data["wip_balance"] = rows_data["wip_balance"].map(_dollar)
            story.append(_simple_table(
                ["Period","Cost Center","Units Produced","Units Consumed","WIP Balance"],
                rows_data.values.tolist(),
                [1.2*inch, 1.5*inch, 1.5*inch, 1.5*inch, 1.5*inch],
            ))
            story.append(Spacer(1, 0.1 * inch))

        if not wh.empty:
            story.append(Paragraph(f"<b>Warehouse WIP — {_dollar(total_wh_wip)}</b>", cell_style))
            story.append(Spacer(1, 0.04 * inch))
            rows_data = wh[["period","program_bucket","wip_balance"]].copy()
            rows_data["wip_balance"] = rows_data["wip_balance"].map(_dollar)
            story.append(_simple_table(
                ["Period","Bucket","WIP Balance"],
                rows_data.values.tolist(),
                [1.5*inch, 3.0*inch, 1.5*inch],
            ))

    # ---- Footer ----
    footer_style = ParagraphStyle(
        "SnapFooter", parent=styles["Normal"],
        fontSize=8, textColor=colors.HexColor("#999999"),
        alignment=TA_CENTER,
    )
    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey,
                            spaceBefore=0, spaceAfter=0.1 * inch))
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