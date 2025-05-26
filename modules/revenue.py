import streamlit as st
import pandas as pd
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy import text
import altair as alt

# === Load env + DB ===
engine = create_engine(st.secrets["SUPABASE_CONN"])

def get_data(sql: str):
    df = pd.read_sql(text(sql), engine)
    df["Month"] = pd.to_datetime(df["month"], errors="coerce").dt.strftime("%Y-%m")
    return df.dropna(subset=["Month"])


def render():
    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:12px;border-radius:6px;margin-bottom:20px;">
            <h2 style="color:white;margin:0;">Nautical Revenue Dashboard</h2>
        </div>
        """,
        unsafe_allow_html=True
    )

    tabs = st.tabs(["Overview", "Kits", "OW Units", "Bags", "Pallets"])

    # === Tab 0: Overview ===
    with tabs[0]:
        def get_topline_revenue():
            query = """
                SELECT TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
                       SUM(amount) AS total_revenue
                FROM stg_product_service_detail
                WHERE contract_completion_date >= '2025-01-01'
                GROUP BY 1 ORDER BY 1;
            """
            df = pd.read_sql(text(query), engine)
            df["Month"] = pd.to_datetime(df["month"], errors="coerce").dt.strftime("%Y-%m")
            return df.dropna(subset=["Month"])

        revenue_df = get_topline_revenue()
        month_order = sorted(revenue_df["Month"].unique())

        st.subheader("Topline Revenue by Month")
        chart = alt.Chart(revenue_df).mark_bar(size=40).encode(
            x=alt.X("Month:N", sort=month_order),
            y=alt.Y("total_revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
            tooltip=["Month", "total_revenue"]
        ).properties(height=300)

        chart_labels = chart.mark_text(
            align='center', baseline='bottom', dy=-5, color='black'
        ).encode(text=alt.Text("total_revenue:Q", format="$,.0f"))

        st.altair_chart(chart + chart_labels, use_container_width=True)
        st.markdown("---")

        def get_customer_breakdown():
            query = """
                SELECT TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
                       customer_full_name,
                       SUM(amount) AS total_billed
                FROM stg_product_service_detail
                WHERE contract_completion_date >= '2025-01-01'
                GROUP BY 1, 2
                ORDER BY 1, 3 DESC;
            """
            return pd.read_sql(query, engine)

        rev_df = get_customer_breakdown()
        available_months = sorted(rev_df["month"].unique())
        selected_months = st.multiselect("Select Month(s)", available_months, default=available_months)

        filtered = rev_df[rev_df["month"].isin(selected_months)]
        pivot = filtered.pivot_table(index="customer_full_name", columns="month", values="total_billed", fill_value=0)
        pivot["TOTAL"] = pivot.sum(axis=1)
        total_row = pd.DataFrame(pivot.sum(axis=0)).T
        total_row.index = ["TOTAL"]
        pivot = pd.concat([pivot, total_row])

        def highlight_mom(df):
            styles = pd.DataFrame("", index=df.index, columns=df.columns)
            cols = [c for c in df.columns if c != "TOTAL"]
            for i in range(1, len(cols)):
                prev_col, curr_col = cols[i - 1], cols[i]
                delta = df[curr_col] - df[prev_col]
                styles[curr_col] = np.where(
                    df.index == "TOTAL", "",
                    np.where(
                        delta > 0.01, "background-color:#d4edda; color:#155724;",
                        np.where(delta < -0.01, "background-color:#f8d7da; color:#721c24;", "background-color:#f0f0f0; color:#333333;")
                    )
                )
            return styles

        st.subheader("Customer Revenue by Month (with Period-over-Period Change)")
        styled = pivot.applymap(lambda x: f"${x:,.2f}")
        st.dataframe(
            styled.style.apply(lambda _: highlight_mom(pivot), axis=None).set_properties(**{'text-align': 'right'}),
            use_container_width=True
        )

    # === Tabs 1–4: Revenue by Category ===
    category_queries = {
        "Kits": """
            SELECT TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
                   SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE (
                product_service ILIKE '%full kit%' OR product_service ILIKE '%base kit%' OR
                product_service ILIKE '%full target%' OR product_service ILIKE '%target easel kit%' OR
                product_service ILIKE '%full sfg%' OR product_service ILIKE '%full abs kit%' OR
                product_service ILIKE '%full go kit%' OR product_service ILIKE '%generic kit%'
            )
              WHERE contract_completion_date >= '2025-01-01'
            GROUP BY 1 ORDER BY 1;
        """,
        "OW Units": """
            SELECT TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
                   SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE product_service ILIKE '%ow%'
              WHERE contract_completion_date >= '2025-01-01'
            GROUP BY 1 ORDER BY 1;
        """,
        "Bags": """
            SELECT TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
                   SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE product_service ILIKE '%tote%' OR product_service ILIKE '%tote bag%'
              WHERE contract_completion_date >= '2025-01-01''
            GROUP BY 1 ORDER BY 1;
        """,
        "Pallets": """
            SELECT TO_CHAR(DATE_TRUNC('month', contract_completion_date), 'YYYY-MM') AS month,
                   SUM(amount) AS total_revenue
            FROM stg_product_service_detail
            WHERE product_service ILIKE '%pallet storage full month%'
              WHERE contract_completion_date >= '2025-01-01'
            GROUP BY 1 ORDER BY 1;
        """
    }

    def render_bar_tab(tab, label, sql):
        with tab:
            st.subheader(f"{label} Revenue by Month")

            df = get_data(sql)  # ✅ ONLY one argument

            if df.empty:
                st.info("No data found.")
                return

            sorted_months = sorted(df["Month"].unique())

            chart = alt.Chart(df).mark_bar(size=40).encode(
                x=alt.X("Month:N", sort=sorted_months),
                y=alt.Y("total_revenue:Q", title="Revenue ($)", axis=alt.Axis(format="$,.0f")),
                tooltip=["Month", "total_revenue"]
            ).properties(height=300)

            chart_labels = chart.mark_text(align="center", baseline="bottom", dy=-5, color="black").encode(
                text=alt.Text("total_revenue:Q", format="$,.0f")
            )

            st.altair_chart(chart + chart_labels, use_container_width=True)
            st.subheader("Monthly Revenue Table")
            df["Formatted"] = df["total_revenue"].map("${:,.2f}".format)
            st.dataframe(df[["Month", "Formatted"]], use_container_width=True)

    for i, (label, sql) in enumerate(category_queries.items(), start=1):
        render_bar_tab(tabs[i], label, sql)
