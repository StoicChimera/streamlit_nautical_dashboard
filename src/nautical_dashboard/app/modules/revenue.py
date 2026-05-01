import streamlit as st
import pandas as pd
import numpy as np
from sqlalchemy import create_engine, text
import altair as alt
import os
from dotenv import load_dotenv

# === Load env + DB ===
load_dotenv()  # loads from .env / .env.prod depending on your alias/env
SUPABASE_CONN = os.getenv("SUPABASE_CONN")

if not SUPABASE_CONN:
    st.error("Missing SUPABASE_CONN environment variable.")
    st.stop()

engine = create_engine(SUPABASE_CONN)

# === Shared SQL: customer rollup rule ===
CUSTOMER_ROLLUP_SQL = """
CASE
  WHEN customer_full_name ILIKE '%recess%'
    OR customer_full_name ILIKE '%arrived%' THEN
      split_part(customer_full_name, ':', 1)

  WHEN array_length(string_to_array(customer_full_name, ':'), 1) > 3 THEN
      concat_ws(':',
        split_part(customer_full_name, ':', 1),
        split_part(customer_full_name, ':', 2),
        split_part(customer_full_name, ':', 3)
      )

  ELSE customer_full_name
END
"""

def get_data(sql: str) -> pd.DataFrame:
    # helpful during dev; remove if noisy
    print("DEBUG SQL:", sql)
    df = pd.read_sql(text(sql), engine)
    if "month" in df.columns:
        df["Month"] = pd.to_datetime(df["month"], errors="coerce").dt.strftime("%Y-%m")
        df = df.dropna(subset=["Month"])
    return df


def highlight_mom(df: pd.DataFrame) -> pd.DataFrame:
    """
    Styles month-over-month cells (excluding TOTAL row) based on delta from prior month.
    Expects numeric df (not formatted strings).
    """
    styles = pd.DataFrame("", index=df.index, columns=df.columns)
    cols = [c for c in df.columns if c != "TOTAL"]
    for i in range(1, len(cols)):
        prev_col, curr_col = cols[i - 1], cols[i]
        delta = df[curr_col] - df[prev_col]
        styles[curr_col] = np.where(
            df.index == "TOTAL", "",
            np.where(
                delta > 0.01,
                "background-color:#d4edda; color:#155724;",
                np.where(
                    delta < -0.01,
                    "background-color:#f8d7da; color:#721c24;",
                    "background-color:#f0f0f0; color:#333333;",
                ),
            ),
        )
    return styles


def render_topline_overview():
    # Topline revenue by month
    topline_sql = """
        SELECT
          TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
          SUM(amount) AS total_revenue
        FROM stg_product_service_detail
        WHERE contract_completion_date >= '2025-01-01'
        GROUP BY 1
        ORDER BY 1;
    """
    revenue_df = get_data(topline_sql)
    if revenue_df.empty:
        st.info("No topline revenue found.")
        return

    month_order = sorted(revenue_df["Month"].unique())

    st.subheader("Topline Revenue by Month")
    chart = alt.Chart(revenue_df).mark_bar(size=40).encode(
        x=alt.X("Month:N", sort=month_order),
        y=alt.Y("total_revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
        tooltip=["Month", "total_revenue"],
    ).properties(height=300)

    chart_labels = chart.mark_text(align="center", baseline="bottom", dy=-5, color="black").encode(
        text=alt.Text("total_revenue:Q", format="$,.0f")
    )

    st.altair_chart(chart + chart_labels, use_container_width=True)
    st.markdown("---")

    # Customer breakdown (rolled up + detail expander)
    customer_sql = f"""
        SELECT
          TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
          customer_full_name,
          {CUSTOMER_ROLLUP_SQL} AS customer_rollup,
          SUM(amount) AS total_billed
        FROM stg_product_service_detail
        WHERE contract_completion_date >= '2025-01-01'
        GROUP BY 1, 2, 3
        ORDER BY 1, 4 DESC;
    """
    rev_df = get_data(customer_sql)
    if rev_df.empty:
        st.info("No customer revenue found.")
        return

    available_months = sorted(rev_df["month"].unique())
    selected_months = st.multiselect("Select Month(s)", available_months, default=available_months, key="overview_months")

    filtered = rev_df[rev_df["month"].isin(selected_months)]

    pivot = filtered.pivot_table(index="customer_rollup", columns="month", values="total_billed", fill_value=0)
    pivot["TOTAL"] = pivot.sum(axis=1)
    total_row = pd.DataFrame(pivot.sum(axis=0)).T
    total_row.index = ["TOTAL"]
    pivot = pd.concat([pivot, total_row])

    st.subheader("Customer Revenue by Month (Rolled Up, with Period-over-Period Change)")
    styled = pivot.applymap(lambda x: f"${x:,.2f}")
    st.dataframe(
        styled.style.apply(lambda _: highlight_mom(pivot), axis=None).set_properties(**{"text-align": "right"}),
        use_container_width=True,
    )

    with st.expander("Show detail (full customer hierarchy)"):
        detail_pivot = filtered.pivot_table(index="customer_full_name", columns="month", values="total_billed", fill_value=0)
        detail_pivot["TOTAL"] = detail_pivot.sum(axis=1)
        detail_pivot = detail_pivot.sort_values("TOTAL", ascending=False)

        st.dataframe(detail_pivot.applymap(lambda x: f"${x:,.2f}"), use_container_width=True)

    # ================================
    # Expander: "Billed last month, not current month"
    # ================================
    month_options = sorted(rev_df["month"].unique())
    if not month_options:
        return

    st.subheader("Recurring Revenue Exceptions")

    current_month = st.selectbox(
        "Current month",
        month_options,
        index=len(month_options) - 1,
        key="missed_billing_current_month",
    )
    prior_month = prev_month_str(current_month)

    with st.expander(f"Customers billed in {prior_month} but not in {current_month}"):
        rollup_monthly = (
            rev_df.groupby(["month", "customer_rollup"], as_index=False)["total_billed"]
            .sum()
        )

        prior = rollup_monthly[rollup_monthly["month"] == prior_month].copy()
        curr = rollup_monthly[rollup_monthly["month"] == current_month].copy()

        if prior.empty:
            st.info(f"No billing found in {prior_month}.")
        else:
            prior_customers = set(prior["customer_rollup"])
            curr_customers = set(curr["customer_rollup"])
            missed = sorted(prior_customers - curr_customers)

            if not missed:
                st.success("No obvious missed billings based on prior month presence.")
            else:
                billed_col = f"billed_{prior_month}"

                missed_df = (
                    prior[prior["customer_rollup"].isin(missed)]
                    .rename(columns={"total_billed": billed_col})
                    .sort_values(billed_col, ascending=False)
                    .reset_index(drop=True)
                )

                display_df = missed_df.drop(columns=["month"], errors="ignore").copy()

                # Pretty headers
                display_df = display_df.rename(columns={
                    "customer_rollup": "Customer",
                    billed_col: f"Billed in {prior_month}",
                })

                display_df[f"Billed in {prior_month}"] = display_df[
                    f"Billed in {prior_month}"
                ].map("${:,.2f}".format)

                st.dataframe(display_df, use_container_width=True)


def render_category_tab(tab, label: str, sql: str):
    with tab:
        st.subheader(f"{label} Revenue by Month")

        df = get_data(sql)
        if df.empty:
            st.info("No data found.")
            return

        # Monthly totals for chart
        chart_df = df.groupby("Month", as_index=False)["total_revenue"].sum()
        sorted_months = sorted(chart_df["Month"].unique())

        chart = alt.Chart(chart_df).mark_bar(size=40).encode(
            x=alt.X("Month:N", sort=sorted_months),
            y=alt.Y("total_revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
            tooltip=["Month", "total_revenue"],
        ).properties(height=300)

        chart_labels = chart.mark_text(align="center", baseline="bottom", dy=-5, color="black").encode(
            text=alt.Text("total_revenue:Q", format="$,.0f")
        )

        st.altair_chart(chart + chart_labels, use_container_width=True)

        st.subheader("Monthly Revenue Table")
        chart_df["Formatted"] = chart_df["total_revenue"].map("${:,.2f}".format)
        st.dataframe(chart_df[["Month", "Formatted"]], use_container_width=True)

        # Customer breakdown (rolled up + detail expander)
        st.markdown("---")
        st.subheader(f"{label} Revenue by Customer (Rolled Up)")

        available_months = sorted(df["month"].unique())
        selected_months = st.multiselect(
            "Select Month(s)",
            available_months,
            default=available_months[-1:] if available_months else available_months,
            key=f"{label}_months",
        )

        filtered = df[df["month"].isin(selected_months)]

        cust_pivot = filtered.pivot_table(index="customer_rollup", columns="month", values="total_revenue", fill_value=0)
        cust_pivot["TOTAL"] = cust_pivot.sum(axis=1)
        cust_pivot = cust_pivot.sort_values("TOTAL", ascending=False)

        st.dataframe(cust_pivot.applymap(lambda x: f"${x:,.2f}"), use_container_width=True)

        with st.expander("Show detail (full customer hierarchy)"):
            detail_pivot = filtered.pivot_table(index="customer_full_name", columns="month", values="total_revenue", fill_value=0)
            detail_pivot["TOTAL"] = detail_pivot.sum(axis=1)
            detail_pivot = detail_pivot.sort_values("TOTAL", ascending=False)

            st.dataframe(detail_pivot.applymap(lambda x: f"${x:,.2f}"), use_container_width=True)

def prev_month_str(yyyy_mm: str) -> str:
    # yyyy_mm like "2025-12"
    p = pd.Period(yyyy_mm, freq="M")
    return (p - 1).strftime("%Y-%m")


def render():
    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:12px;border-radius:6px;margin-bottom:20px;">
            <h2 style="color:white;margin:0;">Nautical Revenue Dashboard</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tabs = st.tabs(["Overview", "Kits", "OW Units", "Bags", "Pallets"])

    # === Tab 0: Overview ===
    with tabs[0]:
        render_topline_overview()

    # === Tabs 1–4: Revenue by Category (NOW includes customer rollups + details) ===
    category_queries = {
        "Kits": f"""
            SELECT
              TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
              customer_full_name,
              {CUSTOMER_ROLLUP_SQL} AS customer_rollup,
              SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE (
                product_service ILIKE '%full kit%' OR product_service ILIKE '%base kit%' OR
                product_service ILIKE '%full target%' OR product_service ILIKE '%target easel kit%' OR
                product_service ILIKE '%full sfg%' OR product_service ILIKE '%full abs kit%' OR
                product_service ILIKE '%full go kit%' OR product_service ILIKE '%generic kit%'
            )
            AND contract_completion_date >= '2025-01-01'
            GROUP BY 1, 2, 3
            ORDER BY 1, 4 DESC;
        """,
        "OW Units": f"""
            SELECT
              TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
              customer_full_name,
              {CUSTOMER_ROLLUP_SQL} AS customer_rollup,
              SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE (product_service ILIKE '%ow%' OR product_service ILIKE '%overwrap%'OR product_service ILIKE '%OverWrap%')
              AND contract_completion_date >= '2025-01-01'
            GROUP BY 1, 2, 3
            ORDER BY 1, 4 DESC;
        """,
        "Bags": f"""
            SELECT
              TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
              customer_full_name,
              {CUSTOMER_ROLLUP_SQL} AS customer_rollup,
              SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE (product_service ILIKE '%tote%' OR product_service ILIKE '%bag%')
              AND contract_completion_date >= '2025-01-01'
            GROUP BY 1, 2, 3
            ORDER BY 1, 4 DESC;
        """,
        "Pallets": f"""
            SELECT
              TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
              customer_full_name,
              {CUSTOMER_ROLLUP_SQL} AS customer_rollup,
              SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE product_service ILIKE '%pallet storage%'
              AND contract_completion_date >= '2025-01-01'
            GROUP BY 1, 2, 3
            ORDER BY 1, 4 DESC;
        """,
    }

    # Render tabs 1..4
    for i, (label, sql) in enumerate(category_queries.items(), start=1):
        render_category_tab(tabs[i], label, sql)
