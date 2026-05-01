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

def get_demo_data() -> pd.DataFrame:
    df = pd.read_sql(text("""
        SELECT
            normalized_date                 AS date,  -- Changed from today_s_date
            customer,
            event_name,
            kit_type_walmart,
            kit_type_kroger_and_albertsons,
            kit_type_target,
            kit_type_other,
            number_of_people,
            total_sbs_hours,
            total_nautical_direct_hours,
            run_time_minutes,
            person_hours,
            number_of_cases_completed,
            run_rate,
            labor_cost_per_kit
        FROM stg_smartsheet_demo
        WHERE normalized_date IS NOT NULL          -- Changed from today_s_date
          AND number_of_cases_completed IS NOT NULL
          AND number_of_cases_completed > 0
        ORDER BY normalized_date                   -- Changed from today_s_date
    """), engine)
    df["date"] = pd.to_datetime(df["date"]) 
    df["month"]      = df["date"].dt.to_period("M").astype(str)
    df["iso_week"]   = df["date"].dt.isocalendar().week.astype(str).str.zfill(2)
    df["iso_year"]   = df["date"].dt.isocalendar().year.astype(str)
    df["week_label"] = df["iso_year"] + "-W" + df["iso_week"]
    return df


@st.cache_data(ttl=300, show_spinner=False)
def get_ogp_data() -> pd.DataFrame:
    df = pd.read_sql(text("""
        SELECT
            date,
            bag_version,
            job_name,
            daily_production_goal,
            daily_production_complete,
            number_of_people_planned,
            number_of_people_working,
            cumulative_bag_version_total_produced,
            total_hours_staffed,
            sbs_hours,
            nautical_direct_hours,
            is_this_rework,
            project_ship_date
        FROM stg_smartsheet_ogp
        WHERE date IS NOT NULL
          AND daily_production_complete IS NOT NULL
          AND daily_production_complete > 0
        ORDER BY date
    """), engine)
    df["date"] = pd.to_datetime(df["date"])
    df["month"]      = df["date"].dt.to_period("M").astype(str)
    df["iso_week"]   = df["date"].dt.isocalendar().week.astype(str).str.zfill(2)
    df["iso_year"]   = df["date"].dt.isocalendar().year.astype(str)
    df["week_label"] = df["iso_year"] + "-W" + df["iso_week"]
    return df


@st.cache_data(ttl=300, show_spinner=False)
def get_ow_data() -> pd.DataFrame:
    df = pd.read_sql(text("""
        SELECT
            date_started,
            date_finished,
            customer,
            project_name,
            work_order_number,
            hours_worked,
            units_produced,
            sale_price_per_unit_for_overwrap_portion    AS unit_rate,
            adv_stock_reference_numbers,
            pack_out_job
        FROM stg_smartsheet_overwrap
        WHERE date_started IS NOT NULL
          AND units_produced IS NOT NULL
          AND units_produced > 0
        ORDER BY date_started
    """), engine)
    df["date_started"] = pd.to_datetime(df["date_started"])
    df["month"]        = df["date_started"].dt.to_period("M").astype(str)
    df["week_label"]   = df["date_started"].dt.strftime("%Y-W%W")
    return df


# ---------------------------------------------------------------------------
# Shared chart helpers
# ---------------------------------------------------------------------------

def bar_chart(df: pd.DataFrame, x: str, y: str, title: str, color: str = "#1f77b4"):
    sorted_x = sorted(df[x].unique())
    chart = (
        alt.Chart(df)
        .mark_bar(color=color)
        .encode(
            x=alt.X(f"{x}:N", sort=sorted_x, title=None),
            y=alt.Y(f"{y}:Q", title="Units"),
            tooltip=[
                alt.Tooltip(f"{x}:N", title="Period"),
                alt.Tooltip(f"{y}:Q", title="Units", format=","),
            ]
        )
        .properties(title=title, height=280)
    )
    labels = chart.mark_text(align="center", baseline="bottom", dy=-4, fontSize=11).encode(
        text=alt.Text(f"{y}:Q", format=",")
    )
    return chart + labels


def mom_table(df: pd.DataFrame, index_col: str, value_col: str,
              label: str = "Units") -> pd.DataFrame:
    monthly = (
        df.groupby(index_col)[value_col]
        .sum()
        .reset_index()
        .rename(columns={index_col: "Month", value_col: label})
        .sort_values("Month")
    )
    monthly["MoM Δ"] = monthly[label].diff()
    monthly["MoM %"] = (monthly[label].pct_change() * 100).round(1)
    monthly[label]   = monthly[label].map("{:,.0f}".format)
    monthly["MoM Δ"] = monthly["MoM Δ"].apply(
        lambda x: f"+{x:,.0f}" if pd.notna(x) and x > 0
        else f"{x:,.0f}" if pd.notna(x) else "—"
    )
    monthly["MoM %"] = monthly["MoM %"].apply(
        lambda x: f"+{x:.1f}%" if pd.notna(x) and x > 0
        else f"{x:.1f}%" if pd.notna(x) else "—"
    )
    return monthly


# ---------------------------------------------------------------------------
# Tab renders
# ---------------------------------------------------------------------------

def render_demo_tab():
    st.subheader("Demo Kit Production Activity")

    demo_df = get_demo_data()
    if demo_df.empty:
        st.info("No demo data found.")
        return

    months   = sorted(demo_df["month"].unique())
    selected = st.multiselect(
        "Filter by Month", months,
        default=months[-3:] if len(months) >= 3 else months,
        key="demo_months"
    )
    if selected:
        demo_df = demo_df[demo_df["month"].isin(selected)]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Kits",         f"{demo_df['number_of_cases_completed'].sum():,.0f}")
    k2.metric("Total Person Hours", f"{demo_df['person_hours'].sum():,.1f}")
    k3.metric("Avg Run Rate",
              f"{demo_df['run_rate'].mean():,.1f}" if demo_df["run_rate"].notna().any() else "—")
    k4.metric("Production Days",    f"{demo_df['date'].nunique():,}")

    st.divider()

    monthly_agg = demo_df.groupby("month")["number_of_cases_completed"].sum().reset_index()
    st.altair_chart(
        bar_chart(monthly_agg, "month", "number_of_cases_completed",
                  "Monthly Kits Completed", "#1f77b4"),
        use_container_width=True
    )

    st.subheader("Month-over-Month Summary")
    st.dataframe(
        mom_table(demo_df, "month", "number_of_cases_completed", "Kits"),
        use_container_width=True, hide_index=True
    )

    st.divider()

    weekly_agg = demo_df.groupby("week_label")["number_of_cases_completed"].sum().reset_index()
    st.altair_chart(
        bar_chart(weekly_agg, "week_label", "number_of_cases_completed",
                  "Weekly Kits Completed", "#5470c6"),
        use_container_width=True
    )

    st.subheader("By Customer")
    cust_agg = (
        demo_df.groupby(["month", "customer"])["number_of_cases_completed"]
        .sum().reset_index()
    )
    cust_pivot = cust_agg.pivot_table(
        index="customer", columns="month",
        values="number_of_cases_completed", fill_value=0
    )
    cust_pivot["Total"] = cust_pivot.sum(axis=1)
    cust_pivot = cust_pivot.sort_values("Total", ascending=False)
    st.dataframe(cust_pivot.applymap(lambda x: f"{x:,.0f}"), use_container_width=True)

    with st.expander("Raw detail"):
        st.dataframe(
            demo_df[[
                "date", "customer", "event_name", "number_of_cases_completed",
                "number_of_people", "person_hours", "run_rate", "labor_cost_per_kit"
            ]].sort_values("date", ascending=False),
            use_container_width=True, hide_index=True
        )


def render_ogp_tab():
    st.subheader("OGP Production Activity")

    ogp_df = get_ogp_data()
    if ogp_df.empty:
        st.info("No OGP data found.")
        return

    months   = sorted(ogp_df["month"].unique())
    selected = st.multiselect(
        "Filter by Month", months,
        default=months[-3:] if len(months) >= 3 else months,
        key="ogp_months"
    )
    if selected:
        ogp_df = ogp_df[ogp_df["month"].isin(selected)]

    show_rework = st.toggle("Include rework rows", value=False, key="ogp_rework")
    if not show_rework:
        ogp_df = ogp_df[ogp_df["is_this_rework"].astype(str).str.upper().fillna("") != "YES"]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Units",            f"{ogp_df['daily_production_complete'].sum():,.0f}")
    k2.metric("Total Hours Staffed",    f"{ogp_df['total_hours_staffed'].sum():,.1f}")
    k3.metric("SBS Hours",              f"{ogp_df['sbs_hours'].sum():,.1f}")
    k4.metric("Nautical Direct Hours",  f"{ogp_df['nautical_direct_hours'].sum():,.1f}")

    st.divider()

    monthly_agg = ogp_df.groupby("month")["daily_production_complete"].sum().reset_index()
    st.altair_chart(
        bar_chart(monthly_agg, "month", "daily_production_complete",
                  "Monthly Units Completed", "#2ca02c"),
        use_container_width=True
    )

    st.subheader("Month-over-Month Summary")
    st.dataframe(
        mom_table(ogp_df, "month", "daily_production_complete", "Units"),
        use_container_width=True, hide_index=True
    )

    st.divider()

    weekly_agg = ogp_df.groupby("week_label")["daily_production_complete"].sum().reset_index()
    st.altair_chart(
        bar_chart(weekly_agg, "week_label", "daily_production_complete",
                  "Weekly Units Completed", "#98df8a"),
        use_container_width=True
    )

    st.subheader("By Bag Version")
    bag_agg = (
        ogp_df.groupby(["month", "bag_version"])["daily_production_complete"]
        .sum().reset_index()
    )
    bag_pivot = bag_agg.pivot_table(
        index="bag_version", columns="month",
        values="daily_production_complete", fill_value=0
    )
    bag_pivot["Total"] = bag_pivot.sum(axis=1)
    bag_pivot = bag_pivot.sort_values("Total", ascending=False)
    st.dataframe(bag_pivot.applymap(lambda x: f"{x:,.0f}"), use_container_width=True)

    with st.expander("Raw detail"):
        st.dataframe(
            ogp_df[[
                "date", "bag_version", "job_name", "daily_production_goal",
                "daily_production_complete", "number_of_people_working",
                "total_hours_staffed", "is_this_rework"
            ]].sort_values("date", ascending=False),
            use_container_width=True, hide_index=True
        )


def render_ow_tab():
    st.subheader("Overwrap Unit Activity")

    ow_df = get_ow_data()
    if ow_df.empty:
        st.info("No overwrap data found.")
        return

    months   = sorted(ow_df["month"].unique())
    selected = st.multiselect(
        "Filter by Month", months,
        default=months[-3:] if len(months) >= 3 else months,
        key="ow_months"
    )
    if selected:
        ow_df = ow_df[ow_df["month"].isin(selected)]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Units",   f"{ow_df['units_produced'].sum():,.0f}")
    k2.metric("Total Hours",   f"{ow_df['hours_worked'].sum():,.1f}")
    k3.metric("Avg Unit Rate",
              f"${ow_df['unit_rate'].mean():,.2f}" if ow_df["unit_rate"].notna().any() else "—")
    k4.metric("Work Orders",   f"{ow_df['work_order_number'].nunique():,}")

    st.divider()

    monthly_agg = ow_df.groupby("month")["units_produced"].sum().reset_index()
    st.altair_chart(
        bar_chart(monthly_agg, "month", "units_produced",
                  "Monthly Units Produced", "#ff7f0e"),
        use_container_width=True
    )

    st.subheader("Month-over-Month Summary")
    st.dataframe(
        mom_table(ow_df, "month", "units_produced", "Units"),
        use_container_width=True, hide_index=True
    )

    st.divider()

    weekly_agg = ow_df.groupby("week_label")["units_produced"].sum().reset_index()
    st.altair_chart(
        bar_chart(weekly_agg, "week_label", "units_produced",
                  "Weekly Units Produced", "#ffbb78"),
        use_container_width=True
    )

    st.subheader("By Project")
    proj_agg = (
        ow_df.groupby(["month", "project_name"])["units_produced"]
        .sum().reset_index()
    )
    proj_pivot = proj_agg.pivot_table(
        index="project_name", columns="month",
        values="units_produced", fill_value=0
    )
    proj_pivot["Total"] = proj_pivot.sum(axis=1)
    proj_pivot = proj_pivot.sort_values("Total", ascending=False)
    st.dataframe(proj_pivot.applymap(lambda x: f"{x:,.0f}"), use_container_width=True)

    with st.expander("Raw detail"):
        st.dataframe(
            ow_df[[
                "date_started", "date_finished", "customer", "project_name",
                "work_order_number", "units_produced", "hours_worked", "unit_rate"
            ]].sort_values("date_started", ascending=False),
            use_container_width=True, hide_index=True
        )


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render():
    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:12px;border-radius:6px;margin-bottom:20px;">
            <h2 style="color:white;margin:0;">Production Activity</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    tab1, tab2, tab3 = st.tabs(["Demo Kits", "OGP", "Overwrap"])

    with tab1:
        render_demo_tab()

    with tab2:
        render_ogp_tab()

    with tab3:
        render_ow_tab()