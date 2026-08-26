"""
SCAAS Revenue Comparison and MoM YTD Profitability — standalone report.

Two independent tables plus a trend chart, scoped to programs flagged
is_scaas on dim_customer:

  Table 1  Revenue comparison. Current year vs prior year by month, with
           variance in dollars and percent. Aggregate or per-program.
  Table 2  Profitability month over month YTD. The SCAAS P&L lines as rows,
           months as columns, YTD in the final column.

YTD caps the prior year at the same months the current year actually has
data for, so the comparison stays like for like when the current year is
partial.

Run:
    streamlit run scaas_revenue_comparison.py
"""

from __future__ import annotations

import os

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

CONN_STRING = os.getenv("SUPABASE_CONN")

# Programs held out of this report, keyed on dim_customer.canonical_key
# because the key is stable and the display name is not.
#
# advantage_scaas  customer_id 37, level 2. This is the SCAAS PARENT of
#     Altria, Cambio Coffee, Coke, Dorco, Life Time and Unilever, all of
#     which carry is_scaas independently and are reported on their own
#     rows. The parent itself holds only three invoices billed direct to
#     the header — N-14783 (Sep 2025, 70,000.00), N-14672 (Oct 2025,
#     40,000.00) and N-14962 (Nov 2025, 45,000.00), 155,000.00 total.
#     These are additional billing, not recurring program revenue.
#     Gross profit equals revenue on all three, so no margin is lost by
#     dropping them. They did absorb 9,692.86 of pool-allocated SG&A,
#     which is NOT redistributed to the remaining programs — the SCAAS
#     group SG&A total for Sep-Nov 2025 therefore reads lower here than
#     the GL charged the group.
#
# Excluding the parent does not cascade to the children. dim_customer is
# keyed by customer_id with parent_id as a pointer; each child is its own
# row with its own MV entry.
EXCLUDED_CANONICAL_KEYS = ["advantage_scaas"]

MONTH_ABBR = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}

# Dollar columns pulled from mv_program_profitability. Margins are ratios and
# are recomputed at the aggregate level, never summed.
DOLLAR_COLS = [
    "revenue", "billed_amount", "temp_labor", "direct_hire",
    "freight_storage", "raw_materials", "equipment", "commission",
    "applied_wh", "applied_sga", "gross_profit", "net_profit",
]

# COGS components, in presentation order. Applied warehouse sits in COGS to
# match the consolidated P&L on the profitability dashboard.
COGS_COMPONENTS = [
    ("temp_labor", "Temp Labor"),
    ("direct_hire", "Direct Hire"),
    ("raw_materials", "Raw Materials"),
    ("equipment", "Equipment"),
    ("commission", "Commission"),
    ("freight_storage", "Freight & Storage"),
    ("applied_wh", "Applied Warehouse"),
]

BOLD_ROWS = {
    "Revenue", "Total COGS", "Gross Profit", "Net Profit",
}


@st.cache_resource
def get_engine():
    return create_engine(CONN_STRING)


@st.cache_data(ttl=300, show_spinner=False)
def load_available_years(_engine) -> list[int]:
    """Years that have SCAAS profitability rows, newest first."""
    df = pd.read_sql(
        text("""
            SELECT DISTINCT mv.recognized_year
            FROM mv_program_profitability mv
            JOIN dim_customer dc
              ON dc.customer_name = mv.customer_program
            WHERE dc.is_scaas = TRUE
              AND dc.canonical_key <> ALL(:excluded)
            ORDER BY mv.recognized_year DESC
        """),
        _engine,
        params={"excluded": EXCLUDED_CANONICAL_KEYS},
    )
    return [int(y) for y in df["recognized_year"]]


@st.cache_data(ttl=60, show_spinner=False)
def load_scaas_detail(_engine, cy: int, py: int) -> pd.DataFrame:
    """Program-month grain for both years in one round trip."""
    df = pd.read_sql(
        text("""
            SELECT
                mv.recognized_year,
                mv.recognized_month,
                mv.customer_program,
                dc.canonical_key,
                mv.revenue,
                mv.billed_amount,
                mv.temp_labor,
                mv.direct_hire,
                mv.freight_storage,
                mv.raw_materials,
                mv.equipment,
                mv.commission,
                mv.applied_wh,
                mv.applied_sga,
                mv.gross_profit,
                mv.net_profit
            FROM mv_program_profitability mv
            JOIN dim_customer dc
              ON dc.customer_name = mv.customer_program
            WHERE dc.is_scaas = TRUE
              AND dc.canonical_key <> ALL(:excluded)
              AND mv.recognized_year IN (:cy, :py)
            ORDER BY mv.recognized_year, mv.recognized_month, mv.customer_program
        """),
        _engine,
        params={"cy": cy, "py": py, "excluded": EXCLUDED_CANONICAL_KEYS},
    )
    for col in DOLLAR_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df["recognized_year"] = df["recognized_year"].astype(int)
    df["recognized_month"] = df["recognized_month"].astype(int)
    return df


def _amt(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return ""


def _pct(v) -> str:
    if v is None or pd.isna(v):
        return "n/a"
    return f"{float(v) * 100:+.1f}%"


def _ratio(num, den):
    return (num / den) if den else None


def _variance_pct(cy_val, py_val):
    """Percent change. Undefined against a zero or negative prior base."""
    if py_val is None or py_val <= 0:
        return None
    return (cy_val / py_val) - 1.0


def _month_columns(months: list[int]) -> list[str]:
    return [MONTH_ABBR[m] for m in months]


def _style_rows(row):
    label = str(row.iloc[0])
    is_bold = label.strip() in BOLD_ROWS or label.startswith("TOTAL")
    weight = "font-weight: bold" if is_bold else ""
    styles = [weight] * len(row)
    for i, val in enumerate(row):
        text_val = str(val)
        if text_val.startswith("-$") or (text_val.startswith("-") and "%" in text_val):
            styles[i] = (weight + "; color: #B00020").lstrip("; ")
    return styles


# ----------------------------------------------------------------------
# Table 1 — revenue comparison
# ----------------------------------------------------------------------

def _revenue_comparison_aggregate(
    df: pd.DataFrame, cy: int, py: int, months: list[int], label_prefix: str = ""
) -> pd.DataFrame:
    """Aggregate SCAAS revenue, current vs prior year, months as columns."""
    cy_rev = df[df["recognized_year"] == cy].groupby("recognized_month")["revenue"].sum()
    py_rev = df[df["recognized_year"] == py].groupby("recognized_month")["revenue"].sum()
    cy_progs = df[df["recognized_year"] == cy].groupby("recognized_month")["customer_program"].nunique()
    py_progs = df[df["recognized_year"] == py].groupby("recognized_month")["customer_program"].nunique()

    cy_ytd = float(sum(cy_rev.get(m, 0.0) for m in months))
    py_ytd = float(sum(py_rev.get(m, 0.0) for m in months))

    rows = []

    row = {"Line": f"{label_prefix}Current Year ({cy})"}
    for m in months:
        row[MONTH_ABBR[m]] = _amt(cy_rev.get(m, 0.0))
    row["YTD"] = _amt(cy_ytd)
    rows.append(row)

    row = {"Line": f"{label_prefix}Prior Year ({py})"}
    for m in months:
        row[MONTH_ABBR[m]] = _amt(py_rev.get(m, 0.0))
    row["YTD"] = _amt(py_ytd)
    rows.append(row)

    row = {"Line": f"{label_prefix}Variance $"}
    for m in months:
        row[MONTH_ABBR[m]] = _amt(float(cy_rev.get(m, 0.0)) - float(py_rev.get(m, 0.0)))
    row["YTD"] = _amt(cy_ytd - py_ytd)
    rows.append(row)

    row = {"Line": f"{label_prefix}Variance %"}
    for m in months:
        row[MONTH_ABBR[m]] = _pct(
            _variance_pct(float(cy_rev.get(m, 0.0)), float(py_rev.get(m, 0.0)))
        )
    row["YTD"] = _pct(_variance_pct(cy_ytd, py_ytd))
    rows.append(row)

    # Mix indicator — a YoY swing driven by program count is not a rate story.
    row = {"Line": f"{label_prefix}Programs (CY / PY)"}
    for m in months:
        row[MONTH_ABBR[m]] = f"{int(cy_progs.get(m, 0))} / {int(py_progs.get(m, 0))}"
    row["YTD"] = ""
    rows.append(row)

    return pd.DataFrame(rows)[["Line"] + _month_columns(months) + ["YTD"]]


def _revenue_comparison_by_program(
    df: pd.DataFrame, cy: int, py: int, months: list[int]
) -> pd.DataFrame:
    """Per-program revenue, CY / PY / variance stacked under each program.

    Programs are ordered by current-year YTD revenue descending, so the
    material programs lead and any dormant one sinks to the bottom rather
    than heading the table with a row of zeros.
    """
    cy_in_window = df[(df["recognized_year"] == cy) & (df["recognized_month"].isin(months))]
    order = (
        cy_in_window.groupby("customer_program")["revenue"].sum()
        .sort_values(ascending=False)
    )
    programs = list(order.index)
    # Programs with no current-year revenue in the window still get a row.
    for prog in sorted(df["customer_program"].dropna().unique()):
        if prog not in programs:
            programs.append(prog)

    rows = []
    for prog in programs:
        sub = df[df["customer_program"] == prog]
        cy_rev = sub[sub["recognized_year"] == cy].groupby("recognized_month")["revenue"].sum()
        py_rev = sub[sub["recognized_year"] == py].groupby("recognized_month")["revenue"].sum()

        cy_ytd = float(sum(cy_rev.get(m, 0.0) for m in months))
        py_ytd = float(sum(py_rev.get(m, 0.0) for m in months))

        header = {"Line": prog}
        for m in months:
            header[MONTH_ABBR[m]] = ""
        header["YTD"] = f"{_amt(cy_ytd)} vs {_amt(py_ytd)}"
        rows.append(header)

        row = {"Line": f"  {cy}"}
        for m in months:
            row[MONTH_ABBR[m]] = _amt(cy_rev.get(m, 0.0))
        row["YTD"] = _amt(cy_ytd)
        rows.append(row)

        row = {"Line": f"  {py}"}
        for m in months:
            row[MONTH_ABBR[m]] = _amt(py_rev.get(m, 0.0))
        row["YTD"] = _amt(py_ytd)
        rows.append(row)

        row = {"Line": "  Variance %"}
        for m in months:
            row[MONTH_ABBR[m]] = _pct(
                _variance_pct(float(cy_rev.get(m, 0.0)), float(py_rev.get(m, 0.0)))
            )
        row["YTD"] = _pct(_variance_pct(cy_ytd, py_ytd))
        rows.append(row)

    blank = {"Line": ""}
    for m in months:
        blank[MONTH_ABBR[m]] = ""
    blank["YTD"] = ""

    total = _revenue_comparison_aggregate(df, cy, py, months, label_prefix="TOTAL — ")

    out = pd.concat([pd.DataFrame(rows + [blank]), total], ignore_index=True)
    return out[["Line"] + _month_columns(months) + ["YTD"]]


# ----------------------------------------------------------------------
# Table 2 — profitability month over month YTD
# ----------------------------------------------------------------------

def _profitability_mom(
    df: pd.DataFrame, cy: int, months: list[int]
) -> tuple[pd.DataFrame, list[str]]:
    """SCAAS P&L lines as rows, current-year months as columns, YTD last.

    Returns the display frame and any tie-out warnings.
    """
    cy_df = df[df["recognized_year"] == cy]
    by_month = cy_df.groupby("recognized_month")[DOLLAR_COLS].sum()

    ytd = by_month.reindex(months).fillna(0.0).sum()

    def series(col: str) -> dict:
        return {m: float(by_month[col].get(m, 0.0)) for m in months}

    revenue = series("revenue")
    revenue_ytd = float(ytd["revenue"])

    cogs = {m: sum(series(c)[m] for c, _ in COGS_COMPONENTS) for m in months}
    cogs_ytd = float(sum(ytd[c] for c, _ in COGS_COMPONENTS))

    gp = {m: revenue[m] - cogs[m] for m in months}
    gp_ytd = revenue_ytd - cogs_ytd

    sga = series("applied_sga")
    sga_ytd = float(ytd["applied_sga"])

    net = {m: gp[m] - sga[m] for m in months}
    net_ytd = gp_ytd - sga_ytd

    # Tie-out: derived gross profit should match the view's own column.
    warnings: list[str] = []
    mv_gp = series("gross_profit")
    for m in months:
        if abs(mv_gp[m] - gp[m]) > 0.01:
            warnings.append(
                f"{MONTH_ABBR[m]}: derived gross profit {_amt(gp[m])} does not tie to "
                f"mv_program_profitability.gross_profit {_amt(mv_gp[m])} "
                f"(difference {_amt(mv_gp[m] - gp[m])})"
            )

    rows: list[dict] = []

    def add(label: str, values: dict | None, ytd_val, formatter=_amt):
        row = {"Line Item": label}
        for m in months:
            row[MONTH_ABBR[m]] = formatter(values[m]) if values is not None else ""
        row["YTD"] = formatter(ytd_val) if values is not None else ""
        rows.append(row)

    def margin_fmt(v) -> str:
        if v is None or pd.isna(v):
            return "n/a"
        return f"{float(v) * 100:.1f}%"

    add("Revenue", revenue, revenue_ytd)
    add("Billed Amount", series("billed_amount"), float(ytd["billed_amount"]))
    add("", None, None)
    add("Cost of Goods Sold", None, None)
    for col, label in COGS_COMPONENTS:
        add(f"  {label}", series(col), float(ytd[col]))
    add("Total COGS", cogs, cogs_ytd)
    add("", None, None)
    add("Gross Profit", gp, gp_ytd)
    add(
        "GP Margin",
        {m: _ratio(gp[m], revenue[m]) for m in months},
        _ratio(gp_ytd, revenue_ytd),
        margin_fmt,
    )
    add("", None, None)
    add("Applied SG&A", sga, sga_ytd)
    add("Net Profit", net, net_ytd)
    add(
        "Net Margin",
        {m: _ratio(net[m], revenue[m]) for m in months},
        _ratio(net_ytd, revenue_ytd),
        margin_fmt,
    )

    out = pd.DataFrame(rows)[["Line Item"] + _month_columns(months) + ["YTD"]]
    return out, warnings


# ----------------------------------------------------------------------
# Chart
# ----------------------------------------------------------------------

def _render_trend(df: pd.DataFrame, cy: int, py: int, months: list[int]) -> None:
    cy_rev = df[df["recognized_year"] == cy].groupby("recognized_month")["revenue"].sum()
    py_rev = df[df["recognized_year"] == py].groupby("recognized_month")["revenue"].sum()

    labels = _month_columns(months)
    cy_vals = [float(cy_rev.get(m, 0.0)) for m in months]
    py_vals = [float(py_rev.get(m, 0.0)) for m in months]

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=labels, y=cy_vals, mode="lines+markers", name=f"{cy} Revenue",
    ))
    fig.add_trace(go.Scatter(
        x=labels, y=py_vals, mode="lines+markers", name=f"{py} Revenue",
        line=dict(dash="dash"),
    ))
    fig.update_layout(
        height=400,
        title=f"SCAAS Revenue Trend — {cy} vs {py}",
        yaxis_tickformat="$,.0f",
        xaxis_title="Month",
        yaxis_title="Revenue",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    st.plotly_chart(fig, use_container_width=True)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def render() -> None:
    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:12px;border-radius:6px;margin-bottom:20px;">
            <h2 style="color:white;margin:0;">SCAAS Revenue Comparison and YTD Profitability</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    engine = get_engine()

    years = load_available_years(engine)
    if not years:
        st.warning("No SCAAS profitability data available.")
        return

    col_year, col_grain = st.columns([2, 3])
    with col_year:
        cy = st.selectbox("Current year", years, index=0)
    py = cy - 1

    df = load_scaas_detail(engine, cy, py)
    if df.empty:
        st.warning(f"No SCAAS data for {cy} or {py}.")
        return

    months = sorted(df[df["recognized_year"] == cy]["recognized_month"].unique().tolist())
    if not months:
        st.warning(f"No {cy} SCAAS data.")
        return

    py_months = sorted(df[df["recognized_year"] == py]["recognized_month"].unique().tolist())
    if not py_months:
        st.warning(
            f"No {py} data for these programs — the comparison columns will read as zero."
        )

    with col_grain:
        grain = st.radio(
            "Revenue comparison grain",
            ["Aggregate", "By program"],
            horizontal=True,
        )

    st.caption(
        f"Prior year is capped at {MONTH_ABBR[months[0]]}–{MONTH_ABBR[months[-1]]} so the "
        f"YTD column compares the same months in both years. Programs are those flagged "
        f"is_scaas on dim_customer as of today, applied to both years. The SCAAS parent "
        f"header (Advantage Solutions - SCAAS) is excluded — it carries only direct-to-header "
        f"additional billing, not program revenue."
    )

    # ---- Table 1 ----
    st.divider()
    st.markdown(f"### Revenue Comparison — {cy} vs {py}")

    if grain == "Aggregate":
        rev_table = _revenue_comparison_aggregate(df, cy, py, months)
    else:
        rev_table = _revenue_comparison_by_program(df, cy, py, months)

    st.dataframe(
        rev_table.style.apply(_style_rows, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    cy_ytd = float(df[(df["recognized_year"] == cy)
                      & (df["recognized_month"].isin(months))]["revenue"].sum())
    py_ytd = float(df[(df["recognized_year"] == py)
                      & (df["recognized_month"].isin(months))]["revenue"].sum())
    var_pct = _variance_pct(cy_ytd, py_ytd)

    m1, m2, m3 = st.columns(3)
    m1.metric(f"{cy} YTD Revenue", _amt(cy_ytd))
    m2.metric(f"{py} YTD Revenue", _amt(py_ytd))
    m3.metric("YoY Variance", _amt(cy_ytd - py_ytd),
              delta=_pct(var_pct) if var_pct is not None else None)

    # ---- Trend ----
    st.divider()
    _render_trend(df, cy, py, months)

    # ---- Table 2 ----
    st.divider()
    st.markdown(f"### Profitability Month over Month — {cy} YTD")
    st.caption(
        "Current year only. Margins are computed on aggregated dollars, not "
        "averaged across months. SG&A is frozen for committed periods and live "
        "for open ones, so the most recent month can still move."
    )

    pnl_table, tie_warnings = _profitability_mom(df, cy, months)

    for w in tie_warnings:
        st.warning(w)

    st.dataframe(
        pnl_table.style.apply(_style_rows, axis=1),
        use_container_width=True,
        hide_index=True,
    )

    # ---- Export ----
    st.divider()
    csv_rev = rev_table.to_csv(index=False).encode("utf-8")
    csv_pnl = pnl_table.to_csv(index=False).encode("utf-8")
    c1, c2 = st.columns(2)
    c1.download_button(
        "Download Revenue Comparison (CSV)",
        data=csv_rev,
        file_name=f"scaas_revenue_comparison_{cy}_vs_{py}.csv",
        mime="text/csv",
    )
    c2.download_button(
        "Download Profitability MoM (CSV)",
        data=csv_pnl,
        file_name=f"scaas_profitability_mom_{cy}.csv",
        mime="text/csv",
    )


if __name__ == "__main__":
    st.set_page_config(page_title="SCAAS Revenue Comparison", layout="wide")
    render()