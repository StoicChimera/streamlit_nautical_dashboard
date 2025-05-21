def render():
    import streamlit as st
    import pandas as pd
    from sqlalchemy import create_engine
    from sqlalchemy import text
    
    # === Load environment and connect ===
    engine = create_engine(st.secrets["SUPABASE_CONN"])

    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:12px;border-radius:6px;margin-bottom:20px;">
            <h2 style="color:white;margin:0;">Nautical COGS Dashboard</h2>
        </div>
        """,
        unsafe_allow_html=True
    )

    # === Tabs ===
    tab0, tab1, tab2, tab3, tab4 = st.tabs(["Summary", "Demo Kits", "OGP", "Fulfillment Labor", "Freight"])

    with tab0:
        st.title("Monthly COGS Summary")
        st.markdown("---")

        @st.cache_data(ttl=300)
        def get_cogs_summary():
            revenue_query = """
                SELECT DATE_TRUNC('month', contract_completion_date) AS month,
                    SUM(amount) AS revenue
                FROM stg_product_service_detail
                WHERE contract_completion_date BETWEEN '2025-01-01' AND '2025-12-31'
                GROUP BY 1 ORDER BY 1;
            """
            labor_query = """
                SELECT DATE_TRUNC('month', iso_week) AS month,
                    SUM(total_cost) AS labor_cost
                FROM mv_smartsheet_labor_allocation_costed
                WHERE iso_week BETWEEN '2025-01-01' AND '2025-12-31'
                GROUP BY 1 ORDER BY 1;
            """
            fulfill_labor_query = """
                SELECT DATE_TRUNC('month', entry_date) AS month,
                    SUM(ledger_amount) AS fulfillment_labor
                FROM stg_wip_fulfillment_expenses
                WHERE entry_date BETWEEN '2025-01-01' AND '2025-12-31'
                GROUP BY 1 ORDER BY 1;
            """
            freight_query = """
                SELECT DATE_TRUNC('month', entry_date) AS month,
                    SUM(ledger_amount) AS freight
                FROM mv_wip_fulfillment_freight
                WHERE entry_date BETWEEN '2025-01-01' AND '2025-12-31'
                GROUP BY 1 ORDER BY 1;
            """

            rev = pd.read_sql(revenue_query, engine)
            lab = pd.read_sql(labor_query, engine)
            flab = pd.read_sql(fulfill_labor_query, engine)
            frt = pd.read_sql(freight_query, engine)

            # Ensure all are datetime for merging
            rev["month"] = pd.to_datetime(rev["month"], utc=True).dt.tz_localize(None)
            lab["month"] = pd.to_datetime(lab["month"], utc=True).dt.tz_localize(None)
            flab["month"] = pd.to_datetime(flab["month"], utc=True).dt.tz_localize(None)
            frt["month"] = pd.to_datetime(frt["month"], utc=True).dt.tz_localize(None)


            df = rev.merge(lab, on="month", how="outer") \
                    .merge(flab, on="month", how="outer") \
                    .merge(frt, on="month", how="outer")

            df.fillna(0, inplace=True)
            df["total_cogs"] = df["labor_cost"] + df["fulfillment_labor"] + df["freight"]
            df["cogs_pct"] = (df["total_cogs"] / df["revenue"]).round(4)
            df["Month"] = df["month"].dt.strftime("%Y-%m")
            df = df.sort_values("Month")

            # Format
            df["revenue_fmt"] = df["revenue"].map("${:,.2f}".format)
            df["labor_cost_fmt"] = df["labor_cost"].map("${:,.2f}".format)
            df["fulfillment_labor_fmt"] = df["fulfillment_labor"].map("${:,.2f}".format)
            df["freight_fmt"] = df["freight"].map("${:,.2f}".format)
            df["total_cogs_fmt"] = df["total_cogs"].map("${:,.2f}".format)
            df["cogs_pct_fmt"] = df["cogs_pct"].map("{:.1%}".format)

            return df

        cogs_df = get_cogs_summary()

        # === Month Selector ===
        unique_months = cogs_df["Month"].unique().tolist()
        selected_months = st.multiselect("Select Month(s) to View", options=unique_months, default=unique_months)

        filtered_df = cogs_df[cogs_df["Month"].isin(selected_months)]

        st.subheader("Table: COGS Breakdown by Month")
        st.dataframe(
            filtered_df[[
                "Month", "revenue_fmt", "labor_cost_fmt", "fulfillment_labor_fmt",
                "freight_fmt", "total_cogs_fmt", "cogs_pct_fmt"
            ]].rename(columns={
                "revenue_fmt": "Revenue",
                "labor_cost_fmt": "Labor",
                "fulfillment_labor_fmt": "Fulfillment Labor",
                "freight_fmt": "Freight",
                "total_cogs_fmt": "Total COGS",
                "cogs_pct_fmt": "% of Revenue"
            }),
            use_container_width=True
        )

        st.subheader("Chart: COGS vs. Revenue")
        st.line_chart(filtered_df.set_index("Month")[["revenue", "total_cogs"]])

        st.subheader("Chart: % of Revenue (COGS)")
        st.line_chart(filtered_df.set_index("Month")["cogs_pct"])

    # === Tab 1: Demo Kits ===
    with tab1:
        st.title("Demo Kit Production and Labor Breakdown")
        st.markdown("---")

        @st.cache_data(ttl=300)
        def get_demo_kit_counts():
            query = """
                SELECT *
                FROM mv_demo_kits_by_iso_week
                ORDER BY week_start_date DESC;
            """
            return pd.read_sql(query, engine)

        demo_kit_df = get_demo_kit_counts()

        @st.cache_data(ttl=300)
        def get_demo_raw_data():
            query = """
                SELECT today_s_date, number_of_cases_completed
                FROM stg_smartsheet_demo
                WHERE number_of_cases_completed IS NOT NULL
            """
            return pd.read_sql(query, engine)

        stg_df = get_demo_raw_data()

        # === Monthly Totals (calendar month via actual production date)
        monthly_summary = (
            stg_df
            .assign(month=pd.to_datetime(stg_df["today_s_date"]).dt.to_period("M"))
            .groupby("month")["number_of_cases_completed"]
            .sum()
            .reset_index()
        )
        monthly_summary["month"] = monthly_summary["month"].astype(str)
        monthly_summary["total_kits_fmt"] = monthly_summary["number_of_cases_completed"].map("{:,.0f}".format)

        st.subheader("Monthly Total Kits Produced (by actual production date)")
        st.dataframe(
            monthly_summary[["month", "total_kits_fmt"]].rename(columns={
                "month": "Month",
                "total_kits_fmt": "Total Kits"
            }),
            use_container_width=True
        )

        # === Weekly Totals from MV
        demo_kit_df["total_kits_fmt"] = demo_kit_df["total_kits"].map("{:,.0f}".format)

        st.subheader("Weekly Kits Produced")
        st.dataframe(
            demo_kit_df.rename(columns={
                "week_start_date": "Week of",
                "total_kits_fmt": "Total Kits"
            })[["iso_week", "Week of", "Total Kits"]],
            use_container_width=True
        )

        st.subheader("Trend: Total Kits Completed Per Week")
        st.line_chart(demo_kit_df.set_index("week_start_date")["total_kits"])


        if st.button("🔄 Refresh Demo Kit MV"):
            with engine.begin() as conn:
                conn.execute(text("REFRESH MATERIALIZED VIEW mv_demo_kits_by_iso_week;"))
            st.success("Materialized View refreshed!")
            st.cache_data.clear()
            st.rerun()

        @st.cache_data(ttl=300)
        def get_demo_labor_costs():
            query = """
                SELECT
                    iso_week,
                    demo_labor,
                    ogp_labor,
                    total_labor,
                    ROUND(demo_pct * 100, 1) AS demo_pct,
                    ROUND(ogp_pct * 100, 1) AS ogp_pct,
                    total_cost,
                    demo_labor_cost,
                    ogp_labor_cost
                FROM mv_smartsheet_labor_allocation_costed
                ORDER BY iso_week DESC;
            """
            df = pd.read_sql(query, engine)
            df["Week of"] = df["iso_week"]
            df["demo_pct"] = df["demo_pct"].map("{:.1f}%".format)
            df["ogp_pct"] = df["ogp_pct"].map("{:.1f}%".format)
            df["total_cost"] = df["total_cost"].map("${:,.2f}".format)
            df["demo_labor_cost"] = df["demo_labor_cost"].map("${:,.2f}".format)
            df["ogp_labor_cost"] = df["ogp_labor_cost"].map("${:,.2f}".format)
            df["demo_labor"] = df["demo_labor"].map("{:,.2f}".format)
            df["ogp_labor"] = df["ogp_labor"].map("{:,.2f}".format)
            df["total_labor"] = df["total_labor"].map("{:,.2f}".format)
            return df.drop(columns=["iso_week"])

        st.markdown("---")
        st.subheader("Weekly Labor Allocation + Cost Breakdown")
        st.dataframe(get_demo_labor_costs(), use_container_width=True)

        @st.cache_data(ttl=300)
        def get_demo_unit_labor_cost():
            query = """
                SELECT *
                FROM mv_demo_unit_labor_cost_by_week
                ORDER BY iso_week DESC;
            """
            return pd.read_sql(query, engine)

        demo_unit_df = get_demo_unit_labor_cost()
        demo_unit_df["Week of"] = demo_unit_df["iso_week"]
        demo_unit_df["demo_units"] = demo_unit_df["demo_units"].map("{:,.0f}".format)
        demo_unit_df["demo_labor_cost"] = demo_unit_df["demo_labor_cost"].map("${:,.2f}".format)
        demo_unit_df["unit_labor_cost"] = demo_unit_df["unit_labor_cost"].map("${:,.4f}".format)

        st.markdown("---")
        st.subheader("Demo Unit Labor Cost Per Week")
        st.dataframe(demo_unit_df.drop(columns=["iso_week"]), use_container_width=True)

        st.subheader("Trend: Kit Unit Labor Cost Per Week")
        st.line_chart(
            demo_unit_df.set_index("Week of")["unit_labor_cost"]
            .str.replace("[$,]", "", regex=True)
            .astype(float)
        )

        if st.button("🔄 Refresh Demo Unit Labor MV"):
            with engine.begin() as conn:
                conn.execute(text("REFRESH MATERIALIZED VIEW mv_demo_unit_labor_cost_by_week;"))
            st.success("Demo Unit Labor MV refreshed!")
            st.cache_data.clear()
            st.rerun()

    # === Tab 2: OGP Kits ===
    with tab2:
        st.title("OGP Production (OW)")
        st.markdown("---")

        @st.cache_data(ttl=300)
        def get_ogp_unit_counts():
            query = """
                SELECT *
                FROM mv_ow_ogp_units_by_week
                ORDER BY iso_week DESC;
            """
            return pd.read_sql(query, engine)

        ogp_unit_df = get_ogp_unit_counts()
        ogp_unit_df["Week of"] = ogp_unit_df["iso_week"]
        ogp_unit_df["total_OW_units"] = ogp_unit_df["total_ogp_units"].map("{:,.0f}".format)

        st.subheader("OGP OW Units Produced (by ISO Week)")
        st.dataframe(ogp_unit_df.drop(columns=["iso_week", "total_ogp_units"]), use_container_width=True)

        st.subheader("Trend: OW Units Per Week")
        st.line_chart(ogp_unit_df.set_index("Week of")["total_ogp_units"])

        if st.button("🔄 Refresh OGP OW Unit MV"):
            with engine.begin() as conn:
                conn.execute(text("REFRESH MATERIALIZED VIEW mv_ow_ogp_units_by_week;"))
            st.success("OGP Unit MV refreshed!")
            st.cache_data.clear()
            st.rerun()

        @st.cache_data(ttl=300)
        def get_ogp_unit_labor_cost():
            query = """
                SELECT *
                FROM mv_ow_ogp_unit_labor_cost
                ORDER BY iso_week DESC;
            """
            return pd.read_sql(query, engine)

        ogp_unit_cost_df = get_ogp_unit_labor_cost()
        ogp_unit_cost_df["Week of"] = ogp_unit_cost_df["iso_week"]
        ogp_unit_cost_df["total_ogp_units"] = ogp_unit_cost_df["total_ogp_units"].map("{:,.0f}".format)
        ogp_unit_cost_df["ogp_labor_cost"] = ogp_unit_cost_df["ogp_labor_cost"].map("${:,.2f}".format)
        ogp_unit_cost_df["avg_unit_labor_cost"] = ogp_unit_cost_df["avg_unit_labor_cost"].map("${:,.4f}".format)

        st.markdown("---")
        st.subheader("OGP Unit Labor Cost Per Week")
        st.dataframe(ogp_unit_cost_df.drop(columns=["iso_week"]), use_container_width=True)

        st.subheader("Trend: Avg Labor Cost Per OGP Unit")
        st.line_chart(ogp_unit_cost_df.set_index("Week of")["avg_unit_labor_cost"].str.replace("[$,]", "", regex=True).astype(float))

        if st.button("🔄 Refresh OGP Unit Labor Cost MV"):
            with engine.begin() as conn:
                conn.execute(text("REFRESH MATERIALIZED VIEW mv_ow_ogp_unit_labor_cost;"))
            st.success("OGP Unit Labor Cost MV refreshed!")
            st.cache_data.clear()
            st.rerun()

    # === Tab 3: Fulfillment Labor ===
    with tab3:
        st.title("Fulfillment Labor by ISO Week")
        st.markdown("---")

        @st.cache_data(ttl=300)
        def get_fulfillment_labor():
            query = """
                SELECT 
                    customer,
                    DATE_TRUNC('month', accrual_date) AS month_start,
                    SUM(ledger_amount) AS total_labor
                FROM stg_wip_fulfillment_expenses
                GROUP BY customer, DATE_TRUNC('month', accrual_date)
                ORDER BY month_start DESC;
            """
            return pd.read_sql(query, engine)

        labor_df = get_fulfillment_labor()
        labor_df["Month"] = labor_df["month_start"].dt.strftime("%Y-%m")
        labor_df["numeric_labor"] = labor_df["total_labor"]
        labor_df["total_labor"] = labor_df["total_labor"].map("${:,.2f}".format)

        # Total summary
        total_by_month = (
            labor_df.groupby("Month")["numeric_labor"]
            .sum()
            .reset_index()
            .rename(columns={"numeric_labor": "total_labor"})
        )
        total_by_month["Formatted Total"] = total_by_month["total_labor"].map("${:,.2f}".format)

        st.subheader("Total Fulfillment Labor by Month")
        st.dataframe(total_by_month[["Month", "Formatted Total"]], use_container_width=True)
        st.subheader("Trend: Total Fulfillment Labor Over Time")
        st.line_chart(total_by_month.set_index("Month")["total_labor"])

        # By customer
        st.markdown("---")
        st.subheader("Monthly Fulfillment Labor by Customer")
        st.dataframe(labor_df.drop(columns=["month_start", "numeric_labor"]), use_container_width=True)

        st.subheader("Trend: Monthly Labor Cost by Customer")
        chart_df = labor_df.pivot_table(
            index="Month", columns="customer", values="numeric_labor", aggfunc="sum"
        ).fillna(0)
        st.line_chart(chart_df)

        if st.button("🔄 Refresh Fulfillment Labor MV"):
            with engine.begin() as conn:
                conn.execute(text("REFRESH MATERIALIZED VIEW mv_fulfillment_labor"))
            st.success("Fulfillment Labor MV refreshed!")
            st.cache_data.clear()
            st.rerun()

    # === Tab 4: Freight ===
    with tab4:
        st.title("Freight Breakdown: Fulfillment vs. Project")
        st.markdown("### Fulfillment Freight by Month")

        @st.cache_data(ttl=300)
        def get_fulfillment_freight():
            query = """
                SELECT 
                    customer,
                    DATE_TRUNC('month', entry_date) AS month_start,
                    SUM(ledger_amount) AS total_freight
                FROM mv_wip_fulfillment_freight
                GROUP BY customer, DATE_TRUNC('month', entry_date)
                ORDER BY month_start DESC;
            """
            return pd.read_sql(query, engine)


        freight_df = get_fulfillment_freight()
        freight_df["Month"] = freight_df["month_start"].dt.strftime("%Y-%m")
        freight_df["numeric_freight"] = freight_df["total_freight"]
        freight_df["total_freight"] = freight_df["total_freight"].map("${:,.2f}".format)

        # === 1. Total Freight by Month
        freight_total_by_month = (
            freight_df.groupby("Month")["numeric_freight"]
            .sum()
            .reset_index()
            .rename(columns={"numeric_freight": "total_freight"})
        )
        freight_total_by_month["Formatted Total"] = freight_total_by_month["total_freight"].map("${:,.2f}".format)

        st.subheader("Total Fulfillment Freight by Month")
        st.dataframe(freight_total_by_month[["Month", "Formatted Total"]], use_container_width=True)

        st.subheader("Trend: Total Fulfillment Freight Over Time")
        st.line_chart(freight_total_by_month.set_index("Month")["total_freight"])

        # === 2. By Customer Breakdown
        st.markdown("---")
        st.subheader("Monthly Fulfillment Freight by Customer")
        st.dataframe(
            freight_df.drop(columns=["month_start", "numeric_freight"]),
            use_container_width=True
        )

        st.subheader("Trend: Monthly Freight Cost by Customer")
        freight_chart_df = freight_df.pivot_table(
            index="Month", columns="customer", values="numeric_freight", aggfunc="sum"
        ).fillna(0)
        st.line_chart(freight_chart_df)

        if st.button("🔄 Refresh Fulfillment Freight MV"):
            with engine.begin() as conn:
                conn.execute(text("REFRESH MATERIALIZED VIEW mv_wip_fulfillment_freight"))
            st.success("Fulfillment Freight MV refreshed!")
            st.cache_data.clear()
            st.rerun()
