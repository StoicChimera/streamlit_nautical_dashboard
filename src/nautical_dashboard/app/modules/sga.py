import streamlit as st
import pandas as pd
import altair as alt
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# === Load env + DB ===
load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")

if not SUPABASE_CONN:
    st.error("Missing SUPABASE_CONN environment variable.")
    st.stop()

engine = create_engine(SUPABASE_CONN)


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_sga_summary() -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT
            accrual_period,
            category,
            SUM(amount) AS total
        FROM vw_sga_transactions
        GROUP BY accrual_period, category
        ORDER BY accrual_period, category
    """), engine)


@st.cache_data(ttl=300, show_spinner=False)
def get_sga_detail(period_start: str, period_end: str) -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT
            accrual_period,
            txn_date,
            source,
            category,
            account_name,
            counterparty,
            description,
            department,
            amount
        FROM vw_sga_transactions
        WHERE accrual_period BETWEEN :start AND :end
        ORDER BY accrual_period, category, txn_date
    """), engine, params={"start": period_start, "end": period_end})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_dollar(x) -> str:
    try:
        return f"${float(x):,.2f}"
    except Exception:
        return str(x)


def build_pivot(df: pd.DataFrame, periods: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (body_df, total_row) separately so TOTAL always stays at bottom."""
    pivot = df.pivot_table(
        index="category", columns="accrual_period",
        values="total", aggfunc="sum", fill_value=0
    )
    ordered_cols = [p for p in sorted(periods) if p in pivot.columns]
    pivot = pivot[ordered_cols]
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("Total", ascending=False)

    total_row = pivot.sum(axis=0).to_frame().T
    total_row.index = ["TOTAL"]

    return pivot, total_row


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render():
    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:12px;border-radius:6px;margin-bottom:20px;">
            <h2 style="color:white;margin:0;">SG&A Analysis</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    summary_df = get_sga_summary()
    if summary_df.empty:
        st.warning("No SG&A data found.")
        return

    all_periods = sorted(summary_df["accrual_period"].unique())

    # --- Period selector ---
    selected_periods = st.multiselect(
        "Filter by Period",
        all_periods,
        default=all_periods[-6:] if len(all_periods) >= 6 else all_periods,
        key="sga_periods"
    )

    if not selected_periods:
        st.info("Select at least one period.")
        return

    filtered = summary_df[summary_df["accrual_period"].isin(selected_periods)]

    # --- KPIs ---
    monthly_totals = filtered.groupby("accrual_period")["total"].sum()
    avg_monthly    = monthly_totals.mean()

    # MoM change on latest two periods
    if len(monthly_totals) >= 2:
        sorted_totals = monthly_totals.sort_index()
        mom_delta = sorted_totals.iloc[-1] - sorted_totals.iloc[-2]
        mom_pct   = (mom_delta / sorted_totals.iloc[-2] * 100) if sorted_totals.iloc[-2] else 0
        mom_label = f"{'+' if mom_delta >= 0 else ''}{mom_pct:.1f}% vs prior period"
    else:
        mom_label = "—"

    k1, k2 = st.columns(2)
    k1.metric("Avg Monthly", fmt_dollar(avg_monthly))
    k2.metric("MoM Change",  mom_label)

    st.divider()

    # --- Trend line ---
    st.subheader("Total SG&A Trend")
    trend_df = (
        filtered.groupby("accrual_period")["total"]
        .sum().reset_index().sort_values("accrual_period")
    )
    trend_chart = (
        alt.Chart(trend_df)
        .mark_line(point=True, color="#1f77b4")
        .encode(
            x=alt.X("accrual_period:N", sort=sorted(selected_periods), title=None),
            y=alt.Y("total:Q", title="Total ($)", axis=alt.Axis(format="$,.0f")),
            tooltip=[
                alt.Tooltip("accrual_period:N", title="Period"),
                alt.Tooltip("total:Q",          title="Total", format="$,.2f"),
            ]
        )
        .properties(height=220)
    )
    st.altair_chart(trend_chart, use_container_width=True)

    # --- Stacked bar chart ---
    st.subheader("Monthly SG&A")
    chart = (
        alt.Chart(filtered)
        .mark_bar()
        .encode(
            x=alt.X("accrual_period:N", sort=sorted(selected_periods), title=None),
            y=alt.Y("total:Q", title="Amount ($)", axis=alt.Axis(format="$,.0f")),
            color=alt.Color("category:N", legend=alt.Legend(title="Category")),
            tooltip=[
                alt.Tooltip("accrual_period:N", title="Period"),
                alt.Tooltip("category:N",       title="Category"),
                alt.Tooltip("total:Q",          title="Amount", format="$,.2f"),
            ]
        )
        .properties(height=260)
    )
    st.altair_chart(chart, use_container_width=True)

    st.divider()

    # --- Category × Month pivot table with totals ---
    st.subheader("Expense Breakdown")
    pivot, total_row = build_pivot(filtered, selected_periods)

    # Format body
    body_fmt = pivot.applymap(fmt_dollar)

    # Format total row separately
    total_fmt = total_row.applymap(fmt_dollar)
    total_fmt = total_fmt.style.apply(
        lambda _: ["font-weight: bold; background-color: #f0f0f0"] * len(total_fmt.columns),
        axis=1
    )

    # Display body then total row pinned at bottom
    st.dataframe(body_fmt, use_container_width=True)
    st.dataframe(total_fmt, use_container_width=True, hide_index=False)

    st.divider()

    # --- Transaction detail drill down ---
    st.subheader("Transaction Detail")

    period_min = min(selected_periods)
    period_max = max(selected_periods)
    detail_df  = get_sga_detail(period_min, period_max)
    detail_df  = detail_df[detail_df["accrual_period"].isin(selected_periods)]

    col1, col2 = st.columns(2)
    with col1:
        drill_category = st.selectbox(
            "Category",
            ["All"] + sorted(detail_df["category"].unique()),
            key="sga_drill_category"
        )
    with col2:
        if drill_category != "All":
            acct_options = sorted(detail_df[detail_df["category"] == drill_category]["account_name"].unique())
        else:
            acct_options = sorted(detail_df["account_name"].unique())
        drill_account = st.selectbox(
            "Account",
            ["All"] + acct_options,
            key="sga_drill_account"
        )

    if drill_category != "All":
        detail_df = detail_df[detail_df["category"] == drill_category]
    if drill_account != "All":
        detail_df = detail_df[detail_df["account_name"] == drill_account]

    # Account summary
    acct_summary = (
        detail_df.groupby(["account_name", "category"])["amount"]
        .sum().reset_index()
        .sort_values("amount", ascending=False)
        .rename(columns={"amount": "Total", "account_name": "Account", "category": "Category"})
    )
    acct_summary["Total"] = acct_summary["Total"].map(fmt_dollar)
    st.dataframe(acct_summary, use_container_width=True, hide_index=True)

    # Raw transactions
    with st.expander("Raw transactions"):
        display_df = detail_df[[
            "accrual_period", "txn_date", "source", "category",
            "account_name", "counterparty", "description", "department", "amount"
        ]].copy()
        display_df["amount"] = display_df["amount"].map(fmt_dollar)
        display_df = display_df.sort_values(["accrual_period", "category", "txn_date"])
        st.dataframe(display_df, use_container_width=True, hide_index=True)