import streamlit as st
import pandas as pd
import altair as alt
import os
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from . import wip_labor_review as wlr
from . import wip_labor_container_unload as wcu
from . import wip_labor_compute as wlc
from . import wip_labor_allocation as wla
from . import auth

# === Load env + DB ===
load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")

if not SUPABASE_CONN:
    st.error("Missing SUPABASE_CONN environment variable.")
    st.stop()

engine = create_engine(SUPABASE_CONN)

# ---------------------------------------------------------------------------
# Static translation constants
# ---------------------------------------------------------------------------

ACTIVITY_LABEL_MAP = {
    "Demo":      "Demo Kits (Driver)",
    "OGP":       "Bags (Driver)",
    "Overwrap":  "OW Units (Driver)",
    "SGA":       "Revenue",
    "Receiving": "Receipts (Driver)",
    "Shipping":  "Shipments (Driver)",
    "Inventory": "Pallets: 3-Month Avg (Driver)",
}


# ---------------------------------------------------------------------------
# Receiving returns CRUD
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def get_receiving_returns(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT id, accrual_period, customer_name, return_count,
               minutes_per_return, hours_per_period, set_by, set_at
        FROM stg_labor_receiving_returns
        WHERE accrual_period = :period
        ORDER BY customer_name
    """)
    try:
        return pd.read_sql(sql, engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


def upsert_receiving_return(period: str, customer_name: str, return_count: int,
                             minutes_per_return: float, hours_per_period: float,
                             set_by: str):
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO stg_labor_receiving_returns
                (accrual_period, customer_name, return_count,
                 minutes_per_return, hours_per_period, set_by, set_at)
            VALUES
                (:period, :customer, :count, :mpr, :hpp, :by, :at)
            ON CONFLICT (accrual_period, customer_name)
            DO UPDATE SET
                return_count       = EXCLUDED.return_count,
                minutes_per_return = EXCLUDED.minutes_per_return,
                hours_per_period   = EXCLUDED.hours_per_period,
                set_by             = EXCLUDED.set_by,
                set_at             = EXCLUDED.set_at
        """), {
            "period":   period,
            "customer": customer_name,
            "count":    return_count,
            "mpr":      minutes_per_return,
            "hpp":      hours_per_period,
            "by":       set_by,
            "at":       now,
        })


def delete_receiving_return(period: str, customer_name: str):
    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM stg_labor_receiving_returns
            WHERE accrual_period = :period AND customer_name = :customer
        """), {"period": period, "customer": customer_name})
    
# ---------------------------------------------------------------------------
# Data helpers — review tabs
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_available_periods() -> list[str]:
    df = pd.read_sql(
        text("""
            SELECT DISTINCT accrual_period FROM stg_labor_direct_hire
            UNION
            SELECT DISTINCT accrual_period FROM stg_labor_temp
            ORDER BY 1
        """),
        engine,
    )
    return df["accrual_period"].tolist()


def get_prior_period(period: str) -> str | None:
    periods = get_available_periods()
    periods_sorted = sorted(periods)
    try:
        idx = periods_sorted.index(period)
        return periods_sorted[idx - 1] if idx > 0 else None
    except ValueError:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def get_approved_cogs_pools_weekly(period: str) -> pd.DataFrame:
    """
    Weekly labor pool by (iso_week, cost_center, labor_type). For the
    activity driver overview display. Only includes rows with iso_week populated
    (weekly drivers); period-level rows have iso_week = None.
    """
    try:
        emp_alloc = _cached_employee_alloc(period)
    except Exception:
        return pd.DataFrame(columns=["iso_week", "effective_bucket", "labor_type", "labor_pool"])

    if emp_alloc.empty:
        return pd.DataFrame(columns=["iso_week", "effective_bucket", "labor_type", "labor_pool"])

    weekly = emp_alloc[emp_alloc["iso_week"].notna() & (emp_alloc["cost_type"] == "COGS")].copy()
    if weekly.empty:
        return pd.DataFrame(columns=["iso_week", "effective_bucket", "labor_type", "labor_pool"])

    weekly["labor_type"] = weekly["labor_source"].map({
        "Direct COGS": "direct_cogs",
        "Temp":        "temp",
    })
    weekly["iso_week"] = weekly["iso_week"].astype(int)

    grouped = weekly.groupby(["iso_week", "source_bucket", "labor_type"], as_index=False)["allocated_cost"].sum()
    grouped.rename(columns={"source_bucket": "effective_bucket", "allocated_cost": "labor_pool"}, inplace=True)
    return grouped


def _normalize_bucket(b: str) -> str:
    if not b:
        return b
    bu = b.upper()
    if ('LIFE' in bu or 'TIME' in bu) and ('LIFETIME' in bu.replace(' ', '') or 'LIFE TIME' in bu):
        return 'LifeTime'
    return b


@st.cache_data(ttl=300, show_spinner=False)
def get_purchasing_programs() -> pd.DataFrame:
    sql = text("""
        SELECT customer_name, activity_class, activity_subclass
        FROM dim_customer
        WHERE active = TRUE
          AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
          AND is_purchasing_program = TRUE
        ORDER BY activity_subclass, customer_name
    """)
    return pd.read_sql(sql, engine)


@st.cache_data(ttl=300, show_spinner=False)
def get_facilities_programs() -> pd.DataFrame:
    sql = text("""
        SELECT customer_name, activity_class, activity_subclass
        FROM dim_customer
        WHERE active = TRUE
          AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
          AND is_purchasing_program = FALSE
          AND is_third_party_managed = FALSE
          AND customer_name NOT ILIKE '%altria%'
        ORDER BY activity_subclass, customer_name
    """)
    return pd.read_sql(sql, engine)


@st.cache_data(ttl=60, show_spinner=False)
def get_ecomm_programs_for_period(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT e.customer_name
        FROM public.stg_labor_ecomm_period_config e
        WHERE e.accrual_period = :period
          AND e.active = TRUE
        ORDER BY e.customer_name
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=60, show_spinner=False)
def get_labor_applied(period: str) -> pd.DataFrame:
    try:
        return pd.read_sql(text("""
            SELECT
                source, bucket, program, labor_type,
                activity_driver, activity_value, weight,
                wip_units_forward, wip_cost_forward,
                wip_units_remaining, wip_cost_remaining,
                applied_units, applied_cost,
                locked_by, locked_at
            FROM stg_labor_applied
            WHERE accrual_period = :period
            ORDER BY source, bucket, program, labor_type
        """), engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def check_ukg_coverage(period: str) -> dict:
    """
    Returns coverage status for the period.
    Checks that both boundary paychecks are present in stg_labor_direct_hire.
    prior_tail  = PPE started before month start but prorated into this period
    following_head = PPE ends after month end but prorated into this period
    """
    df = pd.read_sql(
        text("""
            SELECT
                COUNT(CASE WHEN pay_period_start < DATE_TRUNC('month', TO_DATE(:period, 'YYYY-MM'))::date
                           AND accrual_period = :period THEN 1 END) AS prior_tail,
                COUNT(CASE WHEN pay_period_end > (DATE_TRUNC('month', TO_DATE(:period, 'YYYY-MM'))
                                                  + INTERVAL '1 month - 1 day')::date
                           AND accrual_period = :period THEN 1 END) AS following_head
            FROM stg_labor_direct_hire
            WHERE accrual_period = :period
        """),
        engine,
        params={"period": period},
    )
    prior_ok    = int(df["prior_tail"].iloc[0]) > 0
    following_ok = int(df["following_head"].iloc[0]) > 0

    year, month = int(period[:4]), int(period[5:7])
    next_month  = (month % 12) + 1
    next_year   = year + (1 if month == 12 else 0)
    next_month_name = datetime(next_year, next_month, 1).strftime("%B")

    return {
        "prior_ok":       prior_ok,
        "following_ok":   following_ok,
        "all_ok":         prior_ok and following_ok,
        "next_month_name": next_month_name,
        "period":         period,
    }

    
# ---------------------------------------------------------------------------
# E-Commerce config — data helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def get_ecomm_customers() -> pd.DataFrame:
    sql = text("""
        SELECT
            c.canonical_key,
            c.customer_name,
            COALESCE(parent.customer_name, '') AS parent_name
        FROM public.dim_customer c
        LEFT JOIN public.dim_customer parent ON parent.customer_id = c.parent_id
        WHERE c.active = TRUE
          AND c.is_revenue_customer = TRUE
          AND c.roll_up_for_cost = FALSE
        ORDER BY c.customer_name
    """)
    return pd.read_sql(sql, engine)


@st.cache_data(ttl=60, show_spinner=False)
def get_ecomm_config(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT canonical_key, customer_name, set_by, set_at
        FROM public.stg_labor_ecomm_period_config
        WHERE accrual_period = :period AND active = TRUE
        ORDER BY customer_name
    """)
    try:
        return pd.read_sql(sql, engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


def is_ecomm_configured(period: str) -> bool:
    return not get_ecomm_config(period).empty


def save_ecomm_config(period: str, selected_keys: list[str], reviewer_name: str):
    now = datetime.now(timezone.utc).isoformat()
    all_customers = get_ecomm_customers()
    key_to_name = dict(zip(all_customers["canonical_key"], all_customers["customer_name"]))
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM public.stg_labor_ecomm_period_config WHERE accrual_period = :period"),
            {"period": period},
        )
        for key in selected_keys:
            conn.execute(
                text("""
                    INSERT INTO public.stg_labor_ecomm_period_config
                        (accrual_period, canonical_key, customer_name, active, set_by, set_at)
                    VALUES (:period, :key, :name, TRUE, :by, :at)
                """),
                {"period": period, "key": key, "name": key_to_name.get(key, key), "by": reviewer_name, "at": now},
            )


def get_ecomm_revenue_weights(period: str) -> pd.DataFrame:
    config_df  = get_ecomm_config(period)
    if config_df.empty:
        return pd.DataFrame(columns=["canonical_key", "customer_name", "revenue", "weight"])
    revenue_df = get_revenue_by_program(period)
    if revenue_df.empty:
        return pd.DataFrame(columns=["canonical_key", "customer_name", "revenue", "weight"])
    merged = config_df.merge(
        revenue_df.rename(columns={"customer_program": "customer_name"}),
        on="customer_name", how="left",
    ).fillna({"revenue": 0.0})
    total_rev = float(merged["revenue"].sum())
    merged["weight"] = merged["revenue"] / total_rev if total_rev > 0 else 0.0
    return merged[["canonical_key", "customer_name", "revenue", "weight"]]


# ---------------------------------------------------------------------------
# Data helpers — allocation tab
# ---------------------------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def get_alias_map() -> dict[str, str]:
    """Returns lower(alias) -> canonical_name for active non-excluded aliases."""
    df = pd.read_sql(
        text("""
            SELECT alias, canonical_name
            FROM dim_customer_alias
            WHERE active = TRUE
              AND exclude = FALSE
              AND canonical_name IS NOT NULL
        """),
        engine,
    )
    return {row["alias"].lower(): row["canonical_name"] for _, row in df.iterrows()}


@st.cache_data(ttl=600, show_spinner=False)
def get_approved_cogs_pools(period: str) -> pd.DataFrame:
    """
    Approved labor pool by (cost_center, labor_source) for COGS lines.
    Replaces the old heuristic that joined UKG -> stg_labor_program_map.
    Now sources from the new fanned-out allocation result.
    """
    try:
        emp_alloc = _cached_employee_alloc(period)
    except Exception:
        return pd.DataFrame(columns=["effective_bucket", "labor_type", "labor_pool"])

    if emp_alloc.empty:
        return pd.DataFrame(columns=["effective_bucket", "labor_type", "labor_pool"])

    cogs = emp_alloc[emp_alloc["cost_type"] == "COGS"].copy()
    if cogs.empty:
        return pd.DataFrame(columns=["effective_bucket", "labor_type", "labor_pool"])

    # Map labor_source display label back to labor_type for downstream
    cogs["labor_type"] = cogs["labor_source"].map({
        "Direct COGS": "direct_cogs",
        "Temp":        "temp",
    })

    grouped = cogs.groupby(["source_bucket", "labor_type"], as_index=False)["allocated_cost"].sum()
    grouped.rename(columns={"source_bucket": "effective_bucket", "allocated_cost": "labor_pool"}, inplace=True)
    return grouped


@st.cache_data(ttl=60, show_spinner=False)
def get_approved_sga_pool(period: str) -> float:
    sql = text("""
        SELECT COALESCE(SUM(total_labor_cost), 0) AS pool
        FROM stg_labor_direct_hire
        WHERE accrual_period = :period
          AND reviewed = TRUE
          AND UPPER(COALESCE(nmf_role, '')) = 'SG&A'
    """)
    result = pd.read_sql(sql, engine, params={"period": period})
    return float(result["pool"].iloc[0])


@st.cache_data(ttl=300, show_spinner=False)
def get_experiential_programs() -> pd.DataFrame:
    sql = text("""
        SELECT customer_name, activity_class, activity_subclass
        FROM dim_customer
        WHERE active = TRUE
          AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
          AND is_experiential = TRUE
        ORDER BY customer_name
    """)
    return pd.read_sql(sql, engine)


@st.cache_data(ttl=300, show_spinner=False)
def get_advexp_inventory_pool(period: str) -> float:
    """
    Returns total average pallet count for ADVEXP/generic Advantage rows
    in stg_extensiv_stock_status. This pool gets allocated to experiential
    customers by revenue weight rather than by pallet count.
    """
    sql = text("""
        WITH aliased AS (
            SELECT
                COALESCE(a.canonical_name, s.customer_clean) AS canonical,
                a.exclude,
                CAST(s.as_of_ts AS TIMESTAMP)::date          AS snap_date,
                CASE
                    WHEN COUNT(DISTINCT NULLIF(TRIM(s.movable_unit_label_1), '')) > 0
                        THEN COUNT(DISTINCT NULLIF(TRIM(s.movable_unit_label_1), ''))::numeric
                    ELSE COALESCE(SUM(s.sum_on_hand_qty), 0)::numeric
                END AS mu_count
            FROM stg_extensiv_stock_status s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer_clean)
                AND a.active = TRUE
            WHERE s.accrual_period = :period
              AND NULLIF(TRIM(s.customer_clean), '') IS NOT NULL
              AND NULLIF(TRIM(s.as_of_ts),       '') IS NOT NULL
              AND (a.exclude = TRUE OR a.alias IS NULL AND s.customer_clean ILIKE '%advexp%')
            GROUP BY 1, 2, 3
        )
        SELECT COALESCE(ROUND(AVG(mu_count)::numeric, 2), 0) AS pool
        FROM aliased
        WHERE exclude = TRUE
    """)
    result = pd.read_sql(sql, engine, params={"period": period})
    return float(result["pool"].iloc[0]) if not result.empty else 0.0


@st.cache_data(ttl=300, show_spinner=False)
def get_demo_units(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            DATE_TRUNC('week', normalized_date::date)     AS week_start,
            EXTRACT(WEEK FROM normalized_date::date)::int AS iso_week,
            COALESCE(a.canonical_name, s.customer)        AS customer,
            SUM(s.number_of_cases_completed)              AS units
        FROM stg_smartsheet_demo s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer)
            AND a.active = TRUE
        WHERE s.accrual_month = :period
          AND s.number_of_cases_completed > 0
          AND s.normalized_date IS NOT NULL
          AND TRIM(s.normalized_date) != ''
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1, 2, 3
        ORDER BY 1, units DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=300, show_spinner=False)
def get_ogp_units(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            DATE_TRUNC('week', s.date)                    AS week_start,
            EXTRACT(WEEK FROM s.date)::int                AS iso_week,
            COALESCE(a.canonical_name, s.job_name)        AS customer,
            SUM(s.daily_production_complete)              AS units
        FROM stg_smartsheet_ogp s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.job_name)
            AND a.active = TRUE
        WHERE s.accrual_month = :period
          AND s.daily_production_complete > 0
          AND s.date IS NOT NULL
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1, 2, 3
        ORDER BY 1, units DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=300, show_spinner=False)
def get_ow_units(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            week_start,
            iso_week,
            customer,
            SUM(units) AS units
        FROM (
            SELECT
                DATE_TRUNC('week', date_finished)         AS week_start,
                EXTRACT(WEEK FROM date_finished)::int     AS iso_week,
                resolve_overwrap_customer(
                    customer, project_name, work_order_number
                )                                          AS customer,
                units_produced                            AS units
            FROM stg_smartsheet_overwrap
            WHERE accrual_month = :period
              AND units_produced > 0
              AND date_finished IS NOT NULL
        ) r
        WHERE customer IS NOT NULL
        GROUP BY 1, 2, 3
        ORDER BY 1, units DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=300, show_spinner=False)
def get_inventory_units(period: str) -> pd.DataFrame:
    sql = text("""
        WITH aliased AS (
            SELECT
                CASE
                    WHEN s.customer_clean ILIKE '%LT-STG%'
                      OR s.customer_clean ILIKE '%LT STG%'
                      OR s.customer_clean ILIKE 'Life Time%'
                      OR s.customer_clean ILIKE 'LifeTime%'
                      OR s.customer_clean ILIKE 'FINISHED PALLETS LT%'
                        THEN 'Life Time'
                    ELSE COALESCE(a.canonical_name, s.customer_clean)
                END                                                  AS customer,
                a.exclude,
                CAST(s.as_of_ts AS TIMESTAMP)::date                  AS snap_date,
                CASE
                    WHEN COUNT(DISTINCT NULLIF(TRIM(s.movable_unit_label_1), '')) > 0
                        THEN COUNT(DISTINCT NULLIF(TRIM(s.movable_unit_label_1), ''))::numeric
                    ELSE COALESCE(SUM(s.sum_on_hand_qty), 0)::numeric
                END AS mu_count
            FROM stg_extensiv_stock_status s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer_clean)
                AND a.active = TRUE
            WHERE s.accrual_period = :period
              AND NULLIF(TRIM(s.customer_clean), '') IS NOT NULL
              AND NULLIF(TRIM(s.as_of_ts),       '') IS NOT NULL
            GROUP BY 1, 2, 3
        ),
        -- Average pallet count per customer across snapshots
        per_customer AS (
            SELECT
                customer,
                exclude,
                ROUND(AVG(mu_count)::numeric, 2) AS avg_pallets
            FROM aliased
            GROUP BY 1, 2
            HAVING AVG(mu_count) > 0
        ),
        -- ADVEXP pool = sum of all excluded rows (ADVEXP + Advantage Solutions + Nautical internal)
        -- but only the ones that are actually ADVEXP-type (not Nautical internal which has no revenue)
        advexp_pool AS (
            SELECT COALESCE(SUM(avg_pallets), 0) AS pool
            FROM per_customer
            WHERE exclude = TRUE
              AND LOWER(customer) NOT LIKE '%nautical%'
        ),
        -- Non-excluded customers keep their own counts
        real_customers AS (
            SELECT customer, avg_pallets
            FROM per_customer
            WHERE COALESCE(exclude, FALSE) = FALSE
        ),
        -- Revenue weights for experiential customers only
        exp_revenue AS (
            SELECT
                COALESCE(a.canonical_name,
                    CASE
                        WHEN TRIM(SPLIT_PART(p.customer_full_name, ':', 3)) != ''
                            THEN TRIM(SPLIT_PART(p.customer_full_name, ':', 3))
                        WHEN TRIM(SPLIT_PART(p.customer_full_name, ':', 2)) != ''
                            THEN TRIM(SPLIT_PART(p.customer_full_name, ':', 2))
                        ELSE TRIM(p.customer_full_name)
                    END
                ) AS customer_program,
                SUM(p.amount) AS revenue
            FROM stg_product_service_detail p
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(
                    CASE
                        WHEN TRIM(SPLIT_PART(p.customer_full_name, ':', 3)) != ''
                            THEN TRIM(SPLIT_PART(p.customer_full_name, ':', 3))
                        WHEN TRIM(SPLIT_PART(p.customer_full_name, ':', 2)) != ''
                            THEN TRIM(SPLIT_PART(p.customer_full_name, ':', 2))
                        ELSE TRIM(p.customer_full_name)
                    END
                ) AND a.active = TRUE
            -- Only experiential customers
            INNER JOIN dim_customer dc
                ON LOWER(dc.customer_name) = LOWER(
                    COALESCE(a.canonical_name,
                        CASE
                            WHEN TRIM(SPLIT_PART(p.customer_full_name, ':', 3)) != ''
                                THEN TRIM(SPLIT_PART(p.customer_full_name, ':', 3))
                            WHEN TRIM(SPLIT_PART(p.customer_full_name, ':', 2)) != ''
                                THEN TRIM(SPLIT_PART(p.customer_full_name, ':', 2))
                            ELSE TRIM(p.customer_full_name)
                        END
                    )
                )
                AND dc.is_experiential = TRUE
                AND dc.active = TRUE
            WHERE p.contract_completion_date IS NOT NULL
              AND TRIM(p.contract_completion_date::text) != ''
              AND DATE_TRUNC('month', p.contract_completion_date::date) = TO_DATE(:period, 'YYYY-MM')
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
        ),
        exp_total AS (
            SELECT SUM(revenue) AS total_exp_revenue FROM exp_revenue
        ),
        -- ADVEXP redistribution: each experiential customer gets their revenue share of ADVEXP pool
        advexp_alloc AS (
            SELECT
                e.customer_program AS customer,
                ROUND(
                    ((e.revenue / NULLIF(t.total_exp_revenue, 0)) * ap.pool)::numeric
                , 2) AS advexp_pallets
            FROM exp_revenue e
            CROSS JOIN exp_total t
            CROSS JOIN advexp_pool ap
            WHERE ap.pool > 0
        )
        -- Final: real pallet count + ADVEXP redistribution for experiential customers
        SELECT
            r.customer,
            ROUND((r.avg_pallets + COALESCE(aa.advexp_pallets, 0))::numeric, 2) AS units,
            1 AS snapshot_count   -- not meaningful after redistribution
        FROM real_customers r
        LEFT JOIN advexp_alloc aa ON LOWER(aa.customer) = LOWER(r.customer)
        ORDER BY units DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=300, show_spinner=False)
def get_receiving_units(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
            COUNT(DISTINCT s.transaction_id)                   AS units,
            MIN(TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY')) AS report_start,
            MAX(TO_DATE(NULLIF(TRIM(s.report_end_raw),   ''), 'MM/DD/YYYY')) AS report_end
        FROM stg_extensiv_receipts s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer_report_raw)
            AND a.active = TRUE
        WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
          AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
          AND s.accrual_period = :period
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1
        HAVING COUNT(DISTINCT s.transaction_id) > 0
        ORDER BY units DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=300, show_spinner=False)
def get_shipment_units(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
            COUNT(DISTINCT s.transaction_id)                   AS units,
            MIN(TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY')) AS report_start,
            MAX(TO_DATE(NULLIF(TRIM(s.report_end_raw),   ''), 'MM/DD/YYYY')) AS report_end
        FROM stg_extensiv_shipments s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer_report_raw)
            AND a.active = TRUE
        WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
          AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
          AND s.transaction_type_raw NOT ILIKE '%return%'
          AND s.accrual_period = :period
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1
        HAVING COUNT(DISTINCT s.transaction_id) > 0
        ORDER BY units DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=600, show_spinner=False)
def get_revenue_by_program(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT
            COALESCE(
                a.canonical_name,
                CASE
                    WHEN TRIM(SPLIT_PART(s.customer_full_name, ':', 3)) != ''
                        THEN TRIM(SPLIT_PART(s.customer_full_name, ':', 3))
                    WHEN TRIM(SPLIT_PART(s.customer_full_name, ':', 2)) != ''
                        THEN TRIM(SPLIT_PART(s.customer_full_name, ':', 2))
                    ELSE TRIM(s.customer_full_name)
                END
            ) AS customer_program,
            SUM(s.amount) AS revenue
        FROM stg_product_service_detail s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(
                CASE
                    WHEN TRIM(SPLIT_PART(s.customer_full_name, ':', 3)) != ''
                        THEN TRIM(SPLIT_PART(s.customer_full_name, ':', 3))
                    WHEN TRIM(SPLIT_PART(s.customer_full_name, ':', 2)) != ''
                        THEN TRIM(SPLIT_PART(s.customer_full_name, ':', 2))
                    ELSE TRIM(s.customer_full_name)
                END
            )
            AND a.active = TRUE
        WHERE s.contract_completion_date IS NOT NULL
          AND TRIM(s.contract_completion_date::text) != ''
          AND DATE_TRUNC('month', s.contract_completion_date::date) = TO_DATE(:period, 'YYYY-MM')
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1
        ORDER BY revenue DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


@st.cache_data(ttl=60, show_spinner=False)
def get_existing_allocation(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT * FROM stg_labor_allocation
        WHERE accrual_period = :period ORDER BY labor_type, bucket, program
    """)
    try:
        return pd.read_sql(sql, engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


def ensure_allocation_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS stg_labor_allocation (
                id              SERIAL PRIMARY KEY,
                accrual_period  TEXT        NOT NULL,
                labor_type      TEXT        NOT NULL,
                bucket          TEXT        NOT NULL,
                program         TEXT        NOT NULL,
                labor_pool      NUMERIC,
                activity_value  NUMERIC,
                total_activity  NUMERIC,
                weight          NUMERIC,
                allocated_cost  NUMERIC,
                locked          BOOLEAN     DEFAULT FALSE,
                committed_by    TEXT,
                committed_at    TIMESTAMPTZ,
                created_at      TIMESTAMPTZ DEFAULT NOW()
            )
        """))


def commit_allocation(rows: list[dict], period: str, committed_by: str):
    now = datetime.now(timezone.utc).isoformat()
    ensure_allocation_table()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_labor_allocation WHERE accrual_period = :period"),
            {"period": period},
        )
        for r in rows:
            conn.execute(
                text("""
                    INSERT INTO stg_labor_allocation
                        (accrual_period, labor_type, bucket, program,
                         labor_pool, activity_value, total_activity, weight,
                         allocated_cost, locked, committed_by, committed_at)
                    VALUES
                        (:period, :labor_type, :bucket, :program,
                         :labor_pool, :activity_value, :total_activity, :weight,
                         :allocated_cost, TRUE, :committed_by, :committed_at)
                """),
                {**r, "period": period, "committed_by": committed_by, "committed_at": now},
            )


def unlock_allocation(period: str):
    """
    Unlocks a committed period by deleting all artifacts scoped to that period.
    Manually-matched work order rows are preserved (operator intent shouldn't
    be lost on unlock).

    Architecture note — why this is a pure DELETE:
    --------------------------------------------------------------------------
    All "remaining" or "available" quantities in the labor pipeline are
    computed from events (stg_wip_fifo_applied) at read time, not stored
    as mutable counters. Production layer availability is derived as:

        available = units_produced - SUM(fifo_applied.units_applied)

    So when this function deletes a period's fifo_applied events, the layers
    those events were consuming automatically become available again on the
    next read — there is no counter to reverse.

    A common scenario: March's commit consumed some of Feb's layers (March
    fifo_applied rows reference Feb iso_weeks). Unlocking March deletes those
    March events; Feb's layers automatically show their consumption reduced
    on next query. No cross-period restoration logic needed.

    The DELETEs are ordered output-first → events → source artifacts → input,
    so each table is gone before anything that might still reference it on
    read. All run inside a single transaction; partial failure rolls back.
    """
    with engine.begin() as conn:
        # 1. The persisted end-state (P&L + WIP rollup) — nothing reads from it
        #    after this delete; safe to remove first.
        conn.execute(
            text("DELETE FROM stg_labor_applied WHERE accrual_period = :period"),
            {"period": period},
        )

        # 2. Event tables. These are the source of truth for what consumed
        #    what; removing them is what restores availability on layers
        #    (current period and any prior periods this period consumed from).
        conn.execute(
            text("DELETE FROM stg_wip_fifo_applied WHERE accrual_period = :period"),
            {"period": period},
        )
        conn.execute(
            text("""
                DELETE FROM stg_wip_work_order_applied
                WHERE accrual_period = :period AND match_type != 'manual'
            """),
            {"period": period},
        )
        conn.execute(
            text("DELETE FROM stg_wip_program_labor_accrual WHERE accrual_period = :period"),
            {"period": period},
        )

        # 3. Production layer artifacts for this period. Other periods' layers
        #    are not touched (they're independent records owned by their own
        #    accrual_period).
        conn.execute(
            text("DELETE FROM stg_wip_production_layers WHERE accrual_period = :period"),
            {"period": period},
        )

        # 4. Allocated labor and per-employee detail (regenerated from
        #    stg_labor_employee_allocation + activity data on re-commit).
        conn.execute(
            text("DELETE FROM stg_labor_incurred_employee WHERE accrual_period = :period"),
            {"period": period},
        )
        conn.execute(
            text("DELETE FROM stg_labor_incurred WHERE accrual_period = :period"),
            {"period": period},
        )

        # 5. The committed allocation lock itself.
        conn.execute(
            text("DELETE FROM stg_labor_allocation WHERE accrual_period = :period"),
            {"period": period},
        )


def write_production_layers(period: str, committed_by: str):
    """
    Writes stg_wip_production_layers using pool amounts sourced from
    stg_labor_incurred (the committed truth) and units from smartsheet.

    Per (cost_center, customer_program):
      1. Total pool = SUM(allocated_cost) from stg_labor_incurred
      2. Pull units split by (iso_week, output_type) from the appropriate smartsheet
      3. Pool is fungible across output_types — single cost_per_unit derived from
         total units across all output_types
      4. Write one layer row per (iso_week, output_type) per cost_center per program
    """
    now = datetime.now(timezone.utc).isoformat()

    pools = pd.read_sql(text("""
        SELECT
            source_bucket       AS cost_center,
            program             AS customer_program,
            SUM(allocated_cost) AS labor_pool
        FROM stg_labor_incurred
        WHERE accrual_period = :period
          AND source_bucket IN ('Demo', 'OGP', 'Overwrap')
          AND labor_type IN ('direct_cogs', 'temp')
        GROUP BY 1, 2
    """), engine, params={"period": period})

    if pools.empty:
        return

    # Demo: single output_type 'kit'
    demo_units = pd.read_sql(text("""
        SELECT
            EXTRACT(WEEK FROM normalized_date::date)::int AS iso_week,
            COALESCE(a.canonical_name, s.customer)        AS customer,
            'kit'::text                                    AS output_type,
            SUM(number_of_cases_completed)                AS units
        FROM stg_smartsheet_demo s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer) AND a.active = TRUE
        WHERE accrual_month = :period
          AND number_of_cases_completed > 0
          AND normalized_date IS NOT NULL
          AND TRIM(normalized_date) != ''
        GROUP BY 1, 2
    """), engine, params={"period": period})

    # OGP: split bag vs packout by bag_version
    ogp_units = pd.read_sql(text("""
        SELECT
            EXTRACT(WEEK FROM s.date)::int                AS iso_week,
            COALESCE(a.canonical_name, s.job_name)        AS customer,
            CASE
                WHEN s.bag_version ILIKE '%packout%' THEN 'packout'
                ELSE 'bag'
            END                                            AS output_type,
            SUM(s.daily_production_complete)              AS units
        FROM stg_smartsheet_ogp s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.job_name) AND a.active = TRUE
        WHERE s.accrual_month = :period
          AND s.daily_production_complete > 0
          AND s.date IS NOT NULL
        GROUP BY 1, 2, 3
    """), engine, params={"period": period})

    # OW: split overwrap vs packout by pack_out_job; resolve customer via SQL function
    ow_units = pd.read_sql(text("""
        SELECT
            EXTRACT(WEEK FROM date_finished)::int           AS iso_week,
            resolve_overwrap_customer(
                s.customer, s.project_name, s.work_order_number
            )                                                AS customer,
            CASE
                WHEN s.pack_out_job = 'Yes - Pack Out' THEN 'packout'
                ELSE 'overwrap'
            END                                              AS output_type,
            SUM(units_produced)                              AS units
        FROM stg_smartsheet_overwrap s
        WHERE accrual_month = :period
          AND units_produced > 0
          AND date_finished IS NOT NULL
          AND resolve_overwrap_customer(
                s.customer, s.project_name, s.work_order_number
              ) IS NOT NULL
        GROUP BY 1, 2, 3
    """), engine, params={"period": period})

    units_by_cc = {
        "Demo":     demo_units,
        "OGP":      ogp_units,
        "Overwrap": ow_units,
    }

    layer_rows = []

    for _, pool_row in pools.iterrows():
        cost_center      = str(pool_row["cost_center"])
        customer_program = str(pool_row["customer_program"])
        total_pool       = float(pool_row["labor_pool"])

        units_df = units_by_cc.get(cost_center, pd.DataFrame())
        if units_df.empty:
            continue

        prog_units = units_df[
            units_df["customer"].str.lower() == customer_program.lower()
        ].copy()

        if prog_units.empty:
            continue

        # Labor is fungible — single cost_per_unit across all output_types
        total_units = float(prog_units["units"].sum())
        if total_units == 0:
            continue

        for _, unit_row in prog_units.iterrows():
            iso_week    = int(unit_row["iso_week"])
            output_type = str(unit_row["output_type"])
            units       = float(unit_row["units"])
            weight      = units / total_units
            pool        = round(total_pool * weight, 2)

            layer_rows.append({
                "accrual_period":   period,
                "iso_week":         iso_week,
                "cost_center":      cost_center,
                "customer_program": customer_program,
                "output_type":      output_type,
                "units_produced":   units,
                "labor_pool":       pool,
                "cost_per_unit":    round(pool / units, 6) if units > 0 else 0.0,
                "layer_locked":     True,
                "locked_by":        committed_by,
                "locked_at":        now,
            })

    if not layer_rows:
        return

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_wip_production_layers WHERE accrual_period = :period"),
            {"period": period},
        )
        for r in layer_rows:
            conn.execute(text("""
                INSERT INTO stg_wip_production_layers
                    (accrual_period, iso_week, cost_center, customer_program, output_type,
                     units_produced, labor_pool, cost_per_unit,
                     layer_locked, locked_by, locked_at)
                VALUES
                    (:accrual_period, :iso_week, :cost_center, :customer_program, :output_type,
                     :units_produced, :labor_pool, :cost_per_unit,
                     :layer_locked, :locked_by, :locked_at)
                ON CONFLICT (accrual_period, iso_week, cost_center, customer_program, output_type)
                DO UPDATE SET
                    units_produced  = EXCLUDED.units_produced,
                    labor_pool      = EXCLUDED.labor_pool,
                    cost_per_unit   = EXCLUDED.cost_per_unit,
                    layer_locked    = EXCLUDED.layer_locked,
                    locked_by       = EXCLUDED.locked_by,
                    locked_at       = EXCLUDED.locked_at
            """), r)


def run_fifo_matching(period: str, applied_by: str):
    alias_map_df = pd.read_sql(text("""
        SELECT alias, LOWER(alias) AS alias_lc, canonical_name
        FROM dim_customer_alias
        WHERE active = TRUE AND exclude = FALSE
    """), engine)
    alias_map = dict(zip(alias_map_df["alias_lc"], alias_map_df["canonical_name"]))

    # Reverse map: canonical -> set of raw aliases that resolve to it
    # Used to bridge invoice (canonical) -> layer (raw) when OW labor was allocated under raw names
    reverse_alias_map = {}
    for _, row in alias_map_df.iterrows():
        canonical = row["canonical_name"]
        if canonical not in reverse_alias_map:
            reverse_alias_map[canonical] = set()
        reverse_alias_map[canonical].add(row["alias"])

    now = datetime.now(timezone.utc).isoformat()

    def _get_sales(view_name):
        return pd.read_sql(text(f"""
            SELECT iso_week, iso_year, doc_number, customer_name,
                ROUND(SUM(total_units)::numeric, 2) AS units
            FROM {view_name}
            WHERE contract_completion_date IS NOT NULL
            AND DATE_TRUNC('month', contract_completion_date::date)
                = TO_DATE(:period, 'YYYY-MM')
            GROUP BY 1, 2, 3, 4
            ORDER BY iso_week, doc_number
        """), engine, params={"period": period})

    demo_sales     = _get_sales("v_kit_sales_by_iso_week")
    bag_sales      = _get_sales("v_bag_sales_by_iso_week")
    ow_sales       = _get_sales("v_overwrap_sales_by_iso_week")
    pickpack_sales = _get_sales("v_pickpack_sales_by_iso_week")

    # Pull all layers and compute current availability from fifo_applied events.
    # units_remaining is no longer stored — it's derived from events so we can't
    # get out of sync with the consumption history.
    #
    # Availability join handles the raw-vs-canonical customer_program mismatch
    # between layers (raw 'Walmart OGP') and fifo_applied (canonical
    # 'Advantage - Walmart OGP') by normalizing both sides.
    #
    # IMPORTANT: this DELETE is run BEFORE we compute availability so that
    # availability reflects state with this period's prior FIFO matches
    # already removed. Otherwise re-running matching for the same period
    # would see its own historical consumption and refuse to re-match.
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_wip_fifo_applied WHERE accrual_period = :period"),
            {"period": period},
        )

    layers = pd.read_sql(text("""
        WITH consumed AS (
            SELECT
                f.cost_center,
                f.iso_week_produced AS iso_week,
                f.output_type,
                COALESCE(
                    (SELECT a.canonical_name 
                     FROM dim_customer_alias a 
                     WHERE LOWER(a.alias) = LOWER(f.customer_program)
                       AND a.active = TRUE 
                       AND COALESCE(a.exclude, FALSE) = FALSE
                     LIMIT 1),
                    f.customer_program
                ) AS canonical_program,
                SUM(f.units_applied) AS units_consumed
            FROM stg_wip_fifo_applied f
            GROUP BY 1, 2, 3, 4
        )
        SELECT
            l.id,
            l.accrual_period,
            l.iso_week,
            l.cost_center,
            l.customer_program,
            l.output_type,
            l.cost_per_unit,
            l.units_produced,
            l.units_produced - COALESCE(c.units_consumed, 0) AS units_remaining
        FROM stg_wip_production_layers l
        LEFT JOIN consumed c
            ON c.cost_center = l.cost_center
           AND c.iso_week    = l.iso_week
           AND c.output_type = l.output_type
           AND c.canonical_program = COALESCE(
                (SELECT a.canonical_name 
                 FROM dim_customer_alias a 
                 WHERE LOWER(a.alias) = LOWER(l.customer_program)
                   AND a.active = TRUE 
                   AND COALESCE(a.exclude, FALSE) = FALSE
                 LIMIT 1),
                l.customer_program
            )
        WHERE l.units_produced - COALESCE(c.units_consumed, 0) > 0
        ORDER BY l.iso_week ASC, l.accrual_period ASC
    """), engine)

    if layers.empty:
        return

    remaining = {int(r["id"]): float(r["units_remaining"])
                 for _, r in layers.iterrows()}

    applied_rows = []

    def _process(sales_df, output_type, alias_map, cost_center=None):
        """
        FIFO-consume eligible production layers for the given sales.

        output_type : which layer type these sales consume from ('bag', 'overwrap',
                    'packout', 'kit'). REQUIRED.
        cost_center : optional filter. None = pickpack mode, consumes packout
                    layers across all cost_centers (OGP packout + OW packout).

        Hard period gate: layers can only be consumed by sales in the same
        accrual_period or later. A Feb invoice cannot consume a March layer.
        Physically impossible — you can't ship what hasn't been produced yet.
        """
        if sales_df.empty:
            return
        for _, sale in sales_df.iterrows():
            units_to_apply = float(sale["units"])
            if units_to_apply <= 0:
                continue

            raw_name      = str(sale["customer_name"])
            raw_lc        = raw_name.lower()
            program_label = alias_map.get(raw_lc, raw_name)

            # Candidate names: invoice raw, its canonical, AND all raw aliases of that canonical
            # (bridges OW labor allocated under raw names against invoices using canonical names)
            candidates_lc = {raw_lc, program_label.lower()}
            candidates_lc.update(a.lower() for a in reverse_alias_map.get(program_label, set()))

            eligible = layers[
                (layers["customer_program"].str.lower().isin(candidates_lc)) &
                (layers["output_type"]      == output_type) &
                (layers["accrual_period"]   <= period)   # HARD GATE: no future layers
            ]
            if cost_center is not None:
                eligible = eligible[eligible["cost_center"] == cost_center]

            eligible = eligible.sort_values(["iso_week", "accrual_period"])

            for _, layer in eligible.iterrows():
                if units_to_apply <= 0:
                    break
                lid   = int(layer["id"])
                avail = remaining.get(lid, 0.0)
                if avail <= 0:
                    continue

                applied = min(avail, units_to_apply)
                cost    = round(applied * float(layer["cost_per_unit"]), 2)

                applied_rows.append({
                    "accrual_period":    period,
                    "invoice_num":       str(sale["doc_number"]),
                    "customer_name":     str(sale["customer_name"]),
                    "cost_center":       str(layer["cost_center"]),
                    "customer_program":  program_label,
                    "iso_week_produced": int(layer["iso_week"]),
                    "output_type":       str(layer["output_type"]),
                    "units_applied":     applied,
                    "cost_per_unit":     float(layer["cost_per_unit"]),
                    "applied_cost":      cost,
                    "match_type":        "auto",
                    "applied_by":        applied_by,
                    "applied_at":        now,
                })

                remaining[lid]  = avail - applied
                units_to_apply -= applied

    # Bag invoices consume OGP/bag layers
    _process(bag_sales,      output_type="bag",      alias_map=alias_map, cost_center="OGP")
    # Demo invoices consume Demo/kit layers
    _process(demo_sales,     output_type="kit",      alias_map=alias_map, cost_center="Demo")
    # Overwrap invoices consume Overwrap/overwrap layers
    _process(ow_sales,       output_type="overwrap", alias_map=alias_map, cost_center="Overwrap")
    # Pickpack invoices consume packout layers from ANY cost_center (OGP+OW combined FIFO)
    _process(pickpack_sales, output_type="packout",  alias_map=alias_map, cost_center=None)

    if not applied_rows:
        return

    # DELETE already done above (before availability was computed).
    # Just insert the new consumption events. No layer UPDATE — availability
    # is derived from these events on read, not stored.
    with engine.begin() as conn:
        for r in applied_rows:
            conn.execute(text("""
                INSERT INTO stg_wip_fifo_applied
                    (accrual_period, invoice_num, customer_name, cost_center,
                     customer_program, iso_week_produced, output_type, units_applied,
                     cost_per_unit, applied_cost, match_type, applied_by, applied_at)
                VALUES
                    (:accrual_period, :invoice_num, :customer_name, :cost_center,
                     :customer_program, :iso_week_produced, :output_type, :units_applied,
                     :cost_per_unit, :applied_cost, :match_type, :applied_by, :applied_at)
            """), r)


def write_program_labor_accrual(period: str, committed_by: str):
    """No-op as of phase 2 AC/R refactor (2026-05-23).
    AC/R labor now flows through standard fifo + production_wip sources
    via stg_wip_production_layers + stg_wip_fifo_applied. The legacy
    stg_wip_program_labor_accrual table is retained for historical audit
    but is no longer written to."""
    return


# ---------------------------------------------------------------------------
# Production WIP data helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def get_production_layers(period: str) -> pd.DataFrame:
    return pd.read_sql(text("""
        WITH consumed AS (
            SELECT
                f.cost_center,
                f.iso_week_produced AS iso_week,
                f.output_type,
                COALESCE(
                    (SELECT a.canonical_name 
                     FROM dim_customer_alias a 
                     WHERE LOWER(a.alias) = LOWER(f.customer_program)
                       AND a.active = TRUE 
                       AND COALESCE(a.exclude, FALSE) = FALSE
                     LIMIT 1),
                    f.customer_program
                ) AS canonical_program,
                SUM(f.units_applied) AS units_consumed
            FROM stg_wip_fifo_applied f
            GROUP BY 1, 2, 3, 4
        )
        SELECT
            l.id,
            l.accrual_period,
            l.iso_week,
            l.cost_center,
            l.customer_program,
            l.units_produced,
            l.labor_pool,
            l.cost_per_unit,
            l.units_produced - COALESCE(c.units_consumed, 0)         AS units_remaining,
            COALESCE(c.units_consumed, 0)                            AS units_consumed,
            CASE WHEN l.units_produced > 0
                 THEN ROUND(COALESCE(c.units_consumed, 0)::numeric / l.units_produced * 100, 1)
                 ELSE 0 END                                          AS pct_consumed,
            l.layer_locked,
            l.locked_by,
            l.locked_at
        FROM stg_wip_production_layers l
        LEFT JOIN consumed c
            ON c.cost_center = l.cost_center
           AND c.iso_week    = l.iso_week
           AND c.output_type = l.output_type
           AND c.canonical_program = COALESCE(
                (SELECT a.canonical_name 
                 FROM dim_customer_alias a 
                 WHERE LOWER(a.alias) = LOWER(l.customer_program)
                   AND a.active = TRUE 
                   AND COALESCE(a.exclude, FALSE) = FALSE
                 LIMIT 1),
                l.customer_program
            )
        WHERE l.accrual_period = :period
        ORDER BY l.cost_center, l.iso_week, l.customer_program
    """), engine, params={"period": period})


@st.cache_data(ttl=60, show_spinner=False)
def get_fifo_applied(period: str) -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT
            id, accrual_period, invoice_num, customer_name,
            cost_center, customer_program, iso_week_produced,
            units_applied, cost_per_unit, applied_cost,
            match_type, applied_by, applied_at
        FROM stg_wip_fifo_applied
        WHERE accrual_period = :period
        ORDER BY cost_center, customer_program, invoice_num
    """), engine, params={"period": period})


@st.cache_data(ttl=60, show_spinner=False)
def get_wip_summary(period: str) -> pd.DataFrame:
    return pd.read_sql(text("""
        WITH layers AS (
            SELECT 
                accrual_period, cost_center, customer_program, output_type,
                iso_week, units_produced, cost_per_unit,
                units_produced * cost_per_unit AS layer_pool
            FROM stg_wip_production_layers
            WHERE accrual_period = :period
            -- AC/R filter REMOVED in phase 2 refactor (2026-05-23).
            -- AC/R is now standard Production WIP, no special exclusion.
        ),
        consumed AS (
            SELECT
                f.cost_center,
                f.iso_week_produced AS iso_week,
                f.output_type,
                COALESCE(
                    (SELECT a.canonical_name 
                     FROM dim_customer_alias a 
                     WHERE LOWER(a.alias) = LOWER(f.customer_program)
                       AND a.active = TRUE 
                       AND COALESCE(a.exclude, FALSE) = FALSE
                     LIMIT 1),
                    f.customer_program
                ) AS canonical_program,
                SUM(f.units_applied) AS units_consumed,
                SUM(f.applied_cost)  AS cost_consumed
            FROM stg_wip_fifo_applied f
            WHERE TO_DATE(f.accrual_period, 'YYYY-MM') <= TO_DATE(:period, 'YYYY-MM')
            GROUP BY 1, 2, 3, 4
        )
        SELECT
            l.cost_center,
            l.customer_program,
            SUM(l.units_produced)                                       AS units_produced,
            SUM(l.units_produced) - COALESCE(SUM(c.units_consumed), 0)  AS units_remaining,
            COALESCE(SUM(c.units_consumed), 0)                          AS units_consumed,
            SUM(l.layer_pool)                                           AS total_labor_pool,
            COALESCE(SUM(c.cost_consumed), 0)                           AS recognized_cost,
            SUM(l.layer_pool) - COALESCE(SUM(c.cost_consumed), 0)       AS outstanding_wip
        FROM layers l
        LEFT JOIN consumed c
            ON c.cost_center = l.cost_center
           AND c.iso_week    = l.iso_week
           AND c.output_type = l.output_type
           AND c.canonical_program = COALESCE(
                (SELECT a.canonical_name 
                 FROM dim_customer_alias a 
                 WHERE LOWER(a.alias) = LOWER(l.customer_program)
                   AND a.active = TRUE 
                   AND COALESCE(a.exclude, FALSE) = FALSE
                 LIMIT 1),
                l.customer_program
            )
        GROUP BY 1, 2
        ORDER BY 1, 2
    """), engine, params={"period": period})


def get_outstanding_wip_all_periods() -> pd.DataFrame:
    """
    Production WIP outstanding across all periods. Reads directly from
    stg_labor_applied (source = 'production_wip') so values match the
    Period Summary view's Production WIP figure exactly — no float
    drift from independently re-deriving units * cost_per_unit at the
    layer grain.

    Returns one row per (accrual_period, cost_center, customer_program)
    with units_remaining and outstanding_wip columns to match the
    pre-refactor row shape.
    """
    return pd.read_sql(text("""
        SELECT
            accrual_period,
            bucket           AS cost_center,
            program          AS customer_program,
            SUM(COALESCE(wip_units_remaining, 0)) AS units_remaining,
            SUM(applied_cost) AS outstanding_wip
        FROM stg_labor_applied
        WHERE source = 'production_wip'
        GROUP BY 1, 2, 3
        HAVING SUM(applied_cost) > 0
        ORDER BY 1, 2, 3
    """), engine)


@st.cache_data(ttl=60, show_spinner=False)
def get_fulfillment_wip(period: str) -> pd.DataFrame:
    """
    Returns fulfillment WIP for the period — labor allocated to programs
    with no current-period revenue. Reads from stg_labor_applied where
    source = 'current_fulfillment_wip', which is written by
    write_labor_applied Source 4 during commit.
    """
    try:
        return pd.read_sql(text("""
            SELECT
                la.program,
                la.bucket           AS cost_center,
                la.labor_type,
                la.activity_driver,
                SUM(la.activity_value)  AS activity_value,
                SUM(la.applied_cost)    AS applied_cost,
                la.accrual_period
            FROM stg_labor_applied la
            WHERE la.accrual_period = :period
              AND la.source = 'current_fulfillment_wip'
            GROUP BY 1, 2, 3, 4, 7
            ORDER BY applied_cost DESC
        """), engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def get_prior_fulfillment_wip_applicable(period: str) -> pd.DataFrame:
    """
    Returns prior period fulfillment WIP rows where:
    - Labor was written as source = 'current_fulfillment_wip' (program had
      no revenue when labor was incurred)
    - The program HAS revenue in the current period
    - The WIP has not already been applied to the current period

    Surfaces stale WIP that should be expensed against current-period revenue.
    Used by both the auto-apply path during commit and the manual review UI.
    """
    try:
        return pd.read_sql(text("""
            SELECT
                la.accrual_period           AS origin_period,
                la.program,
                la.bucket                   AS cost_center,
                la.labor_type,
                la.activity_driver,
                SUM(la.activity_value)      AS activity_value,
                SUM(la.applied_cost)        AS accrued_cost
            FROM stg_labor_applied la
            WHERE la.source = 'current_fulfillment_wip'
              AND la.accrual_period != :period
              -- Has revenue in the current period
              AND EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start = TO_DATE(:period, 'YYYY-MM')
                    AND mv.customer_program = la.program
              )
              -- Not already applied to the current period
              AND NOT EXISTS (
                  SELECT 1 FROM stg_labor_applied la2
                  WHERE la2.accrual_period = :period
                    AND la2.source = 'fulfillment_wip_applied'
                    AND la2.program = la.program
                    AND la2.bucket  = la.bucket
                    AND la2.labor_type = la.labor_type
              )
            GROUP BY 1, 2, 3, 4, 5
            ORDER BY la.accrual_period, la.program
        """), engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


def write_fulfillment_wip_applied(period: str, rows: list[dict], locked_by: str):
    """
    Writes selected prior fulfillment WIP rows into stg_labor_applied
    for the current period as source = fulfillment_wip_applied.
    """
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        for r in rows:
            conn.execute(text("""
                INSERT INTO stg_labor_applied (
                    accrual_period, source, bucket, program, labor_type,
                    activity_driver, activity_value, weight,
                    wip_units_forward, wip_cost_forward,
                    wip_units_remaining, wip_cost_remaining,
                    applied_units, applied_cost,
                    locked, locked_by, locked_at
                )
                VALUES (
                    :period,
                    'fulfillment_wip_applied',
                    :bucket,
                    :program,
                    :labor_type,
                    :activity_driver,
                    :activity_value,
                    NULL, NULL, NULL, NULL, NULL, NULL,
                    :applied_cost,
                    TRUE, :locked_by, :locked_at
                )
                ON CONFLICT DO NOTHING
            """), {
                "period":           period,
                "bucket":           r["cost_center"],
                "program":          r["program"],
                "labor_type":       r["labor_type"],
                "activity_driver":  f"WIP from {r['origin_period']}: {r['activity_driver']}",
                "activity_value":   float(r["activity_value"]),
                "applied_cost":     float(r["accrued_cost"]),
                "locked_by":        locked_by,
                "locked_at":        now,
            })


def auto_apply_prior_fulfillment_wip(period: str, committed_by: str) -> dict:
    """
    Auto-applies all applicable prior-period fulfillment WIP to the current
    period. Called at commit time, after write_labor_applied, before
    close checks.

    Returns a summary dict for post-commit display:
        {
            'rows_applied':       int,
            'programs_applied':   int,
            'total_amount':       float,
            'detail':             pd.DataFrame,   # full per-row breakdown
        }
    """
    applicable = get_prior_fulfillment_wip_applicable(period)

    if applicable.empty:
        return {
            'rows_applied':     0,
            'programs_applied': 0,
            'total_amount':     0.0,
            'detail':           pd.DataFrame(),
        }

    rows = applicable.to_dict('records')
    write_fulfillment_wip_applied(period, rows, committed_by)

    return {
        'rows_applied':     len(rows),
        'programs_applied': int(applicable['program'].nunique()),
        'total_amount':     float(applicable['accrued_cost'].sum()),
        'detail':           applicable.copy(),
    }


@st.cache_data(ttl=60, show_spinner=False)
def get_accrual_balance(period: str) -> pd.DataFrame:
    try:
        return pd.read_sql(text("""
            SELECT
                accrual_period, customer,
                labor_pool_attributed, units_produced, cost_per_unit,
                applied_cost, unapplied_cost,
                locked, locked_by, locked_at
            FROM stg_wip_program_labor_accrual
            WHERE accrual_period = :period
            ORDER BY customer
        """), engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


def write_labor_incurred(period: str, locked_by: str, employee_alloc_df: pd.DataFrame):
    """Persists program-level allocation to stg_labor_incurred."""
    if employee_alloc_df.empty:
        return

    now = datetime.now(timezone.utc).isoformat()

    agg_raw = employee_alloc_df.copy()
    agg_raw["labor_type"] = agg_raw["labor_source"].map({
        "Direct COGS": "direct_cogs",
        "Temp":        "temp",
        "Direct SG&A": "direct_sga",
    })

    # Drop error rows
    agg_raw = agg_raw[~agg_raw["target_program"].isin(
        ["NO ACTIVITY DATA", "NO PROGRAM DATA", "NO REVENUE DATA"]
    )].copy()
    agg_raw = agg_raw[~agg_raw["target_program"].str.startswith("UNKNOWN DRIVER:", na=False)]
    agg_raw = agg_raw[~agg_raw["target_program"].str.startswith("NO DRIVER:", na=False)]

    agg = agg_raw.groupby(
        ["source_bucket", "target_program", "labor_type"],
        as_index=False
    ).agg(
        activity_driver=("activity_driver", "first"),
        activity_value=("activity_value",  "sum"),
        weight=("weight",          "sum"),
        allocated_cost=("allocated_cost",  "sum"),
    )

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_labor_incurred WHERE accrual_period = :period"),
            {"period": period},
        )
        for _, r in agg.iterrows():
            conn.execute(text("""
                INSERT INTO stg_labor_incurred
                    (accrual_period, source_bucket, program, labor_type,
                     activity_driver, activity_value, weight, allocated_cost,
                     locked, locked_by, locked_at)
                VALUES
                    (:period, :source_bucket, :program, :labor_type,
                     :activity_driver, :activity_value, :weight, :allocated_cost,
                     TRUE, :locked_by, :locked_at)
                ON CONFLICT (accrual_period, source_bucket, program, labor_type)
                DO UPDATE SET
                    activity_driver = EXCLUDED.activity_driver,
                    activity_value  = EXCLUDED.activity_value,
                    weight          = EXCLUDED.weight,
                    allocated_cost  = EXCLUDED.allocated_cost,
                    locked_by       = EXCLUDED.locked_by,
                    locked_at       = EXCLUDED.locked_at
            """), {
                "period":          period,
                "source_bucket":   r["source_bucket"],
                "program":         r["target_program"],
                "labor_type":      r["labor_type"],
                "activity_driver": r["activity_driver"],
                "activity_value":  float(r["activity_value"]),
                "weight":          float(r["weight"]),
                "allocated_cost":  float(r["allocated_cost"]),
                "locked_by":       locked_by,
                "locked_at":       now,
            })


def write_labor_incurred_employee(period: str, locked_by: str, employee_alloc_df: pd.DataFrame):
    """Persists employee-level fan-out to stg_labor_incurred_employee."""
    if employee_alloc_df.empty:
        return

    now = datetime.now(timezone.utc).isoformat()

    df = employee_alloc_df.copy()
    df["labor_type"] = df["labor_source"].map({
        "Direct COGS": "direct_cogs",
        "Temp":        "temp",
        "Direct SG&A": "direct_sga",
    })

    df = df[~df["target_program"].isin(
        ["NO ACTIVITY DATA", "NO PROGRAM DATA", "NO REVENUE DATA"]
    )].copy()
    df = df[~df["target_program"].str.startswith("UNKNOWN DRIVER:", na=False)]
    df = df[~df["target_program"].str.startswith("NO DRIVER:", na=False)]

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_labor_incurred_employee WHERE accrual_period = :period"),
            {"period": period},
        )
        for _, r in df.iterrows():
            conn.execute(text("""
                INSERT INTO stg_labor_incurred_employee
                    (accrual_period, source_bucket, target_program, labor_type,
                     employee_name, labor_source, role_detail,
                     activity_driver, activity_value, weight, allocated_cost,
                     locked, locked_by, locked_at)
                VALUES
                    (:period, :source_bucket, :target_program, :labor_type,
                     :employee_name, :labor_source, :role_detail,
                     :activity_driver, :activity_value, :weight, :allocated_cost,
                     TRUE, :locked_by, :locked_at)
                ON CONFLICT (accrual_period, source_bucket, target_program, labor_type, employee_name)
                DO UPDATE SET
                    activity_driver = EXCLUDED.activity_driver,
                    activity_value  = EXCLUDED.activity_value,
                    weight          = EXCLUDED.weight,
                    allocated_cost  = EXCLUDED.allocated_cost,
                    locked_by       = EXCLUDED.locked_by,
                    locked_at       = EXCLUDED.locked_at
            """), {
                "period":          period,
                "source_bucket":   str(r["source_bucket"]),
                "target_program":  str(r["target_program"]),
                "labor_type":      str(r["labor_type"]),
                "employee_name":   str(r["employee_name"]),
                "labor_source":    str(r["labor_source"]),
                "role_detail":     str(r.get("role_detail") or ""),
                "activity_driver": str(r.get("activity_driver") or ""),
                "activity_value":  float(r.get("activity_value") or 0),
                "weight":          float(r.get("weight") or 0),
                "allocated_cost":  float(r.get("allocated_cost") or 0),
                "locked_by":       locked_by,
                "locked_at":       now,
            })


def write_labor_applied(period: str, locked_by: str):
    now = datetime.now(timezone.utc).isoformat()

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_labor_applied WHERE accrual_period = :period"),
            {"period": period},
        )

        # ----------------------------------------------------------------
        # Source 1 — period_allocation
        # All fanned-out rows from stg_labor_allocation
        # Excludes WIP production buckets — those come from FIFO
        # Excludes fulfillment WIP — programs with no revenue this period
        # ----------------------------------------------------------------
        conn.execute(text("""
            INSERT INTO stg_labor_applied (
                accrual_period, source, bucket, program, labor_type,
                activity_driver, activity_value, weight,
                wip_units_forward, wip_cost_forward,
                wip_units_remaining, wip_cost_remaining,
                applied_units, applied_cost,
                locked, locked_by, locked_at
            )
            SELECT
                li.accrual_period,
                'period_allocation',
                li.source_bucket,
                li.program,
                li.labor_type,
                li.activity_driver,
                li.activity_value,
                li.weight,
                NULL, NULL, NULL, NULL, NULL,
                li.allocated_cost,
                TRUE, :locked_by, :locked_at
            FROM stg_labor_incurred li
            WHERE li.accrual_period = :period
            AND li.source_bucket NOT IN ('Demo', 'OGP', 'Overwrap')
            AND EXISTS (
                SELECT 1 FROM mv_program_profitability mv
                WHERE mv.month_start = TO_DATE(li.accrual_period, 'YYYY-MM')
                AND mv.customer_program = li.program
            )
        """), {"period": period, "locked_by": locked_by, "locked_at": now})

        # ----------------------------------------------------------------
        # Source 2 — fifo
        # Rolled up to program level per cost center
        # WIP forward balance from production layers
        # ----------------------------------------------------------------
        conn.execute(text("""
            INSERT INTO stg_labor_applied (
                accrual_period, source, bucket, program, labor_type,
                activity_driver, activity_value, weight,
                wip_units_forward, wip_cost_forward,
                wip_units_remaining, wip_cost_remaining,
                applied_units, applied_cost,
                locked, locked_by, locked_at
            )
            SELECT
                f.accrual_period,
                'fifo',
                f.cost_center                   AS bucket,
                f.customer_program              AS program,
                'direct_cogs'                   AS labor_type,
                'FIFO Applied'                  AS activity_driver,
                SUM(f.units_applied)            AS activity_value,
                NULL                            AS weight,
                l.units_produced                AS wip_units_forward,
                l.labor_pool                    AS wip_cost_forward,
                l.units_remaining               AS wip_units_remaining,
                l.wip_cost_remaining            AS wip_cost_remaining,
                SUM(f.units_applied)            AS applied_units,
                SUM(f.applied_cost)             AS applied_cost,
                TRUE, :locked_by, :locked_at
            FROM stg_wip_fifo_applied f
            LEFT JOIN (
                -- Per-program layer pool with units_remaining and wip_cost_remaining
                -- computed from fifo_applied events (units_remaining is no longer
                -- a stored column on stg_wip_production_layers).
                SELECT
                    lay.accrual_period,
                    lay.cost_center,
                    lay.customer_program,
                    SUM(lay.units_produced)                                         AS units_produced,
                    SUM(lay.units_produced * lay.cost_per_unit)                     AS labor_pool,
                    SUM(lay.units_produced - COALESCE(cons.units_consumed, 0))      AS units_remaining,
                    SUM((lay.units_produced - COALESCE(cons.units_consumed, 0))
                         * lay.cost_per_unit)                                       AS wip_cost_remaining
                FROM stg_wip_production_layers lay
                LEFT JOIN (
                    SELECT
                        f2.cost_center,
                        f2.iso_week_produced AS iso_week,
                        f2.output_type,
                        COALESCE(
                            (SELECT a.canonical_name 
                             FROM dim_customer_alias a 
                             WHERE LOWER(a.alias) = LOWER(f2.customer_program)
                               AND a.active = TRUE 
                               AND COALESCE(a.exclude, FALSE) = FALSE
                             LIMIT 1),
                            f2.customer_program
                        ) AS canonical_program,
                        SUM(f2.units_applied) AS units_consumed
                    FROM stg_wip_fifo_applied f2
                    GROUP BY 1, 2, 3, 4
                ) cons
                    ON cons.cost_center = lay.cost_center
                   AND cons.iso_week    = lay.iso_week
                   AND cons.output_type = lay.output_type
                   AND cons.canonical_program = COALESCE(
                        (SELECT a.canonical_name 
                         FROM dim_customer_alias a 
                         WHERE LOWER(a.alias) = LOWER(lay.customer_program)
                           AND a.active = TRUE 
                           AND COALESCE(a.exclude, FALSE) = FALSE
                         LIMIT 1),
                        lay.customer_program
                    )
                GROUP BY 1, 2, 3
            ) l ON  l.accrual_period   = f.accrual_period
                AND l.cost_center      = f.cost_center
                AND COALESCE(
                        (SELECT a.canonical_name FROM dim_customer_alias a
                         WHERE LOWER(a.alias) = LOWER(l.customer_program)
                           AND a.active = TRUE
                           AND COALESCE(a.exclude, FALSE) = FALSE
                         LIMIT 1),
                        l.customer_program
                    ) = COALESCE(
                        (SELECT a.canonical_name FROM dim_customer_alias a
                         WHERE LOWER(a.alias) = LOWER(f.customer_program)
                           AND a.active = TRUE
                           AND COALESCE(a.exclude, FALSE) = FALSE
                         LIMIT 1),
                        f.customer_program
                    )
            WHERE f.accrual_period = :period
            GROUP BY f.accrual_period, f.cost_center, f.customer_program,
                    l.units_produced, l.labor_pool, l.units_remaining, l.wip_cost_remaining
        """), {"period": period, "locked_by": locked_by, "locked_at": now})

        # Source 3 — REMOVED in phase 2 AC/R refactor (2026-05-23).
        # AC/R labor now flows through the standard fifo + production_wip
        # sources via the unified sales views (which no longer exclude AC/R).

        # ----------------------------------------------------------------
        # Source 4 — current_fulfillment_wip
        # Current period programs with no revenue (fulfillment WIP)
        # ----------------------------------------------------------------
        conn.execute(text("""
            INSERT INTO stg_labor_applied (
                accrual_period, source, bucket, program, labor_type,
                activity_driver, activity_value, weight,
                wip_units_forward, wip_cost_forward,
                wip_units_remaining, wip_cost_remaining,
                applied_units, applied_cost,
                locked, locked_by, locked_at
            )
            SELECT
                li.accrual_period,
                'current_fulfillment_wip',
                li.source_bucket,
                li.program,
                li.labor_type,
                li.activity_driver,
                li.activity_value,
                li.weight,
                NULL, NULL, NULL, NULL, NULL,
                li.allocated_cost,
                TRUE, :locked_by, :locked_at
            FROM stg_labor_incurred li
            WHERE li.accrual_period = :period
            AND li.source_bucket NOT IN ('Demo', 'OGP', 'Overwrap')
            AND NOT EXISTS (
                SELECT 1 FROM mv_program_profitability mv
                WHERE mv.month_start = TO_DATE(li.accrual_period, 'YYYY-MM')
                AND mv.customer_program = li.program
            )
        """), {"period": period, "locked_by": locked_by, "locked_at": now})

        # ----------------------------------------------------------------
        # Source 5 — production_wip
        # Demo/OGP/Overwrap layer labor produced this period but not yet
        # consumed by FIFO. Bridges the gap between stg_labor_incurred
        # (which includes production allocations) and stg_labor_applied
        # (which only included consumed labor via the 'fifo' source).
        # Without this, production WIP would be invisible to any KPI
        # that reads from stg_labor_applied, breaking the
        # incurred = applied identity used as a close-out sanity check.
        #
        # Math: per layer, unconsumed_cost = units_remaining_at_close * cost_per_unit
        # Where units_remaining_at_close = units_produced - units consumed
        # by THIS period's FIFO (not later periods — those will be
        # written into their own period's stg_labor_applied as 'fifo' rows).
        # ----------------------------------------------------------------
        conn.execute(text("""
            INSERT INTO stg_labor_applied (
                accrual_period, source, bucket, program, labor_type,
                activity_driver, activity_value, weight,
                wip_units_forward, wip_cost_forward,
                wip_units_remaining, wip_cost_remaining,
                applied_units, applied_cost,
                locked, locked_by, locked_at
            )
            WITH this_period_consumption AS (
                SELECT
                    f.cost_center,
                    f.iso_week_produced AS iso_week,
                    f.output_type,
                    COALESCE(
                        (SELECT a.canonical_name 
                         FROM dim_customer_alias a 
                         WHERE LOWER(a.alias) = LOWER(f.customer_program)
                           AND a.active = TRUE 
                           AND COALESCE(a.exclude, FALSE) = FALSE
                         LIMIT 1),
                        f.customer_program
                    ) AS canonical_program,
                    SUM(f.units_applied) AS units_consumed
                FROM stg_wip_fifo_applied f
                WHERE f.accrual_period = :period
                GROUP BY 1, 2, 3, 4
            ),
            unconsumed_layers AS (
                SELECT
                    l.accrual_period,
                    l.cost_center,
                    l.customer_program,
                    SUM(l.units_produced) - COALESCE(SUM(c.units_consumed), 0) AS units_unconsumed,
                    SUM(l.units_produced * l.cost_per_unit) 
                        - COALESCE(SUM(c.units_consumed * l.cost_per_unit), 0) AS cost_unconsumed
                FROM stg_wip_production_layers l
                LEFT JOIN this_period_consumption c
                    ON c.cost_center = l.cost_center
                   AND c.iso_week    = l.iso_week
                   AND c.output_type = l.output_type
                   AND c.canonical_program = COALESCE(
                        (SELECT a.canonical_name 
                         FROM dim_customer_alias a 
                         WHERE LOWER(a.alias) = LOWER(l.customer_program)
                           AND a.active = TRUE 
                           AND COALESCE(a.exclude, FALSE) = FALSE
                         LIMIT 1),
                        l.customer_program
                    )
                WHERE l.accrual_period = :period
                  -- Canonicalization via dim_customer_alias (replaces inline CASE WHEN).
                  -- Join includes output_type to handle same-week-same-program
                  -- with multiple output types (e.g., AC/R Overwrap producing
                  -- both 'overwrap' and 'packout' layers in week 9).
                GROUP BY 1, 2, 3
                HAVING SUM(l.units_produced * l.cost_per_unit) 
                       - COALESCE(SUM(c.units_consumed * l.cost_per_unit), 0) > 0.005
            )
            SELECT
                accrual_period,
                'production_wip'                AS source,
                cost_center                     AS bucket,
                customer_program                AS program,
                'direct_cogs'                   AS labor_type,
                'Production Layer Unconsumed'   AS activity_driver,
                units_unconsumed                AS activity_value,
                NULL                            AS weight,
                units_unconsumed                AS wip_units_forward,
                cost_unconsumed                 AS wip_cost_forward,
                units_unconsumed                AS wip_units_remaining,
                cost_unconsumed                 AS wip_cost_remaining,
                0                               AS applied_units,
                cost_unconsumed                 AS applied_cost,
                TRUE, :locked_by, :locked_at
            FROM unconsumed_layers
        """), {"period": period, "locked_by": locked_by, "locked_at": now})


# ---------------------------------------------------------------------------
# Close checks — post-commit reconciliation
# ---------------------------------------------------------------------------

def run_close_checks(period: str, committed_by: str) -> list[dict]:
    """
    Runs all post-commit reconciliation checks. Writes findings to
    stg_labor_close_check. Returns the list of check results for inline
    display in the UI.

    Each check returns a dict with:
        severity      : 'pass' | 'warn' | 'fail'
        metric_value  : numeric headline (variance, count, etc.)
        details       : list of offending rows (capped at 50 on write)

    Checks are non-blocking — they record findings, they don't abort the
    commit. The UI surfaces severity so a reviewer can decide whether to
    investigate before closing the books.
    """
    findings = []

    checks = [
        ("scaffolding_test",                 _check_scaffolding_test,                None),
        ("identity_holds",                   _check_identity_holds,                  1.00),
        ("unmatched_allocation_targets",     _check_unmatched_allocation_targets,    500.0),
        ("orphan_invoices",                  _check_orphan_invoices,                 1000.0),
        ("future_period_consumption",        _check_future_period_consumption,       None),
        ("layers_without_smartsheet_basis",  _check_layers_without_smartsheet_basis, 500.0),
    ]

    with engine.begin() as conn:
        # Clear prior run for this period — we keep only the latest set of
        # findings per period. Change to append-only if you want full history.
        conn.execute(
            text("DELETE FROM stg_labor_close_check WHERE accrual_period = :period"),
            {"period": period},
        )

        for check_name, check_fn, threshold in checks:
            result = check_fn(period, threshold)
            findings.append({"check_name": check_name, **result})

            conn.execute(text("""
                INSERT INTO stg_labor_close_check
                    (accrual_period, check_name, severity, metric_value,
                     threshold, details, committed_by)
                VALUES
                    (:period, :check_name, :severity, :metric_value,
                     :threshold, CAST(:details AS jsonb), :by)
            """), {
                "period":       period,
                "check_name":   check_name,
                "severity":     result["severity"],
                "metric_value": result.get("metric_value"),
                "threshold":    threshold,
                "details":      json.dumps(result.get("details", [])[:50]),
                "by":           committed_by,
            })

    return findings


def _check_scaffolding_test(period: str, threshold) -> dict:
    """
    Placeholder check that always returns 'pass'. Used to verify the
    runner plumbing works before we write any real checks.
    """
    return {
        "severity":     "pass",
        "metric_value": 0,
        "details":      [],
    }


def _check_identity_holds(period: str, threshold) -> dict:
    """
    Identity: SUM(applied) - SUM(incurred) - SUM(fifo_from_prior_periods) = 0

    fifo_from_prior is the consumption events in this period that reached back
    into earlier periods' layers. Computed by joining fifo events to their
    source layers via the canonical-name bridge that the rest of the pipeline
    uses (dim_customer_alias canonicalization).
    """
    df = pd.read_sql(text("""
        WITH metrics AS (
            SELECT
                (SELECT COALESCE(SUM(allocated_cost), 0)
                 FROM stg_labor_incurred
                 WHERE accrual_period = :period) AS incurred,

                (SELECT COALESCE(SUM(applied_cost), 0)
                 FROM stg_labor_applied
                 WHERE accrual_period = :period
                   AND source IN (
                       'period_allocation', 'fifo', 'current_fulfillment_wip',
                       'fulfillment_wip_applied', 'production_wip'
                   )) AS applied,

                -- FIFO consumption from prior-period layers.
                -- Both sides resolved to canonical via dim_customer_alias so
                -- raw-vs-canonical mismatches (e.g. 'Walmart OGP' on layer,
                -- 'Advantage - Walmart OGP' on fifo event) don't drop rows.
                (SELECT COALESCE(SUM(f.applied_cost), 0)
                 FROM stg_wip_fifo_applied f
                 JOIN stg_wip_production_layers l
                   ON l.cost_center      = f.cost_center
                  AND l.iso_week         = f.iso_week_produced
                  AND l.output_type      = f.output_type
                  AND COALESCE(
                        (SELECT a.canonical_name FROM dim_customer_alias a
                         WHERE LOWER(a.alias) = LOWER(l.customer_program)
                           AND a.active = TRUE
                           AND COALESCE(a.exclude, FALSE) = FALSE
                         LIMIT 1),
                        l.customer_program
                      ) = COALESCE(
                        (SELECT a.canonical_name FROM dim_customer_alias a
                         WHERE LOWER(a.alias) = LOWER(f.customer_program)
                           AND a.active = TRUE
                           AND COALESCE(a.exclude, FALSE) = FALSE
                         LIMIT 1),
                        f.customer_program
                      )
                 WHERE f.accrual_period = :period
                   AND l.accrual_period < :period) AS fifo_from_prior,

                -- Fulfillment WIP pulled forward from prior periods.
                -- Auto-apply (and manual apply) write rows with source =
                -- 'fulfillment_wip_applied' to the current period whose
                -- ORIGIN was an earlier period's current_fulfillment_wip.
                -- The origin period is encoded in activity_driver as
                -- 'WIP from YYYY-MM: ...'. This applied amount has no
                -- matching incurred this period, so back it out of the
                -- identity (same shape as fifo_from_prior).
                (SELECT COALESCE(SUM(applied_cost), 0)
                 FROM stg_labor_applied
                 WHERE accrual_period = :period
                   AND source = 'fulfillment_wip_applied'
                   AND activity_driver ~ '^WIP from \d{4}-\d{2}:') AS fulfillment_wip_from_prior
        )
        SELECT
            incurred,
            applied,
            fifo_from_prior,
            fulfillment_wip_from_prior,
            ROUND((applied - incurred - fifo_from_prior - fulfillment_wip_from_prior)::numeric, 2) AS variance
        FROM metrics
    """), engine, params={"period": period})

    if df.empty:
        return {"severity": "pass", "metric_value": 0, "details": []}

    row = df.iloc[0]
    variance = float(row["variance"] or 0)
    tolerance = float(threshold) if threshold is not None else 1.00

    severity = "pass" if abs(variance) <= tolerance else "fail"

    return {
        "severity":     severity,
        "metric_value": variance,
        "details": [{
            "incurred":                    float(row["incurred"] or 0),
            "applied":                     float(row["applied"] or 0),
            "fifo_from_prior":             float(row["fifo_from_prior"] or 0),
            "fulfillment_wip_from_prior":  float(row["fulfillment_wip_from_prior"] or 0),
            "variance":                    variance,
            "tolerance":                   tolerance,
        }],
    }


def _check_unmatched_allocation_targets(period: str, threshold) -> dict:
    """
    Flags programs in stg_labor_incurred that aren't valid allocation targets
    per dim_customer, AFTER resolving via dim_customer_alias. A valid target:
      - resolves to a row in dim_customer
      - active = TRUE
      - is_revenue_customer = TRUE

    Note: roll_up_for_cost is intentionally NOT checked. That flag controls
    P&L grouping for reporting (mv_program_profitability), not allocation
    eligibility. A sub-program can have its own WMS records, receipts, and
    shipments while still being grouped under a parent for reporting.

    threshold is the $ amount above which severity escalates from warn
    to fail. Default 500.
    """
    df = pd.read_sql(text("""
        WITH resolved AS (
            SELECT
                li.program,
                li.source_bucket,
                li.allocated_cost,
                COALESCE(
                    (SELECT a.canonical_name FROM dim_customer_alias a
                     WHERE LOWER(a.alias) = LOWER(li.program)
                       AND a.active = TRUE
                       AND COALESCE(a.exclude, FALSE) = FALSE
                     LIMIT 1),
                    li.program
                ) AS resolved_program
            FROM stg_labor_incurred li
            WHERE li.accrual_period = :period
        )
        SELECT
            r.program,
            r.source_bucket,
            r.resolved_program,
            ROUND(SUM(r.allocated_cost)::numeric, 2) AS allocated_cost,
            CASE
                WHEN dc.customer_name IS NULL              THEN 'not_in_dim_customer'
                WHEN dc.active = FALSE                     THEN 'inactive_customer'
                WHEN dc.is_revenue_customer = FALSE        THEN 'not_revenue_customer'
                ELSE 'unknown'
            END AS issue
        FROM resolved r
        LEFT JOIN dim_customer dc
            ON LOWER(dc.customer_name) = LOWER(r.resolved_program)
        WHERE dc.customer_name IS NULL
           OR dc.active = FALSE
           OR dc.is_revenue_customer = FALSE
        GROUP BY 1, 2, 3, dc.customer_name, dc.active, dc.is_revenue_customer
        ORDER BY allocated_cost DESC
    """), engine, params={"period": period})

    if df.empty:
        return {"severity": "pass", "metric_value": 0, "details": []}

    total_bad = float(df["allocated_cost"].sum())
    fail_threshold = float(threshold) if threshold is not None else 500.0

    severity = "fail" if total_bad > fail_threshold else "warn"

    return {
        "severity":     severity,
        "metric_value": total_bad,
        "details":      df.to_dict(orient="records"),
    }


def _check_orphan_invoices(period: str, threshold) -> dict:
    """
    Invoices in the production sales views (kit / bag / overwrap / pickpack)
    for PRODUCTION customers (activity_class = 'Production') where FIFO
    consumed zero units per view-line.

    Special rule: pickpack lines on invoices that ALSO have a bag or overwrap
    line are excluded from orphan checking. The bag/overwrap production
    records cover the pickpack labor — there's no separate pickpack
    smartsheet entry expected for those shipments. Only stand-alone pickpack
    invoices (no bag, no overwrap on the same doc_number) are checked.

    threshold is the total unmatched-units count above which severity
    escalates from warn to fail. Default 1000 units.
    """
    df = pd.read_sql(text("""
        WITH all_invoices AS (
            SELECT 'demo' AS view_name, doc_number, customer_name,
                   SUM(total_units) AS units
            FROM v_kit_sales_by_iso_week
            WHERE contract_completion_date IS NOT NULL
              AND DATE_TRUNC('month', contract_completion_date::date)
                  = TO_DATE(:period, 'YYYY-MM')
            GROUP BY 1, 2, 3
            UNION ALL
            SELECT 'bag', doc_number, customer_name, SUM(total_units)
            FROM v_bag_sales_by_iso_week
            WHERE contract_completion_date IS NOT NULL
              AND DATE_TRUNC('month', contract_completion_date::date)
                  = TO_DATE(:period, 'YYYY-MM')
            GROUP BY 1, 2, 3
            UNION ALL
            SELECT 'overwrap', doc_number, customer_name, SUM(total_units)
            FROM v_overwrap_sales_by_iso_week
            WHERE contract_completion_date IS NOT NULL
              AND DATE_TRUNC('month', contract_completion_date::date)
                  = TO_DATE(:period, 'YYYY-MM')
            GROUP BY 1, 2, 3
            UNION ALL
            SELECT 'pickpack', doc_number, customer_name, SUM(total_units)
            FROM v_pickpack_sales_by_iso_week
            WHERE contract_completion_date IS NOT NULL
              AND DATE_TRUNC('month', contract_completion_date::date)
                  = TO_DATE(:period, 'YYYY-MM')
            GROUP BY 1, 2, 3
        ),
        -- Set of doc_numbers that have bag or overwrap lines. Pickpack on
        -- these invoices is implicitly covered by the bag/overwrap production
        -- and should not be checked separately.
        covered_pickpack_docs AS (
            SELECT DISTINCT doc_number
            FROM all_invoices
            WHERE view_name IN ('bag', 'overwrap')
        ),
        invoices_filtered AS (
            SELECT *
            FROM all_invoices ai
            WHERE NOT (
                ai.view_name = 'pickpack'
                AND ai.doc_number IN (SELECT doc_number FROM covered_pickpack_docs)
            )
        ),
        invoices_resolved AS (
            SELECT
                f.view_name,
                f.doc_number,
                f.customer_name,
                f.units,
                COALESCE(
                    (SELECT a.canonical_name FROM dim_customer_alias a
                     WHERE LOWER(a.alias) = LOWER(f.customer_name)
                       AND a.active = TRUE
                       AND COALESCE(a.exclude, FALSE) = FALSE
                     LIMIT 1),
                    f.customer_name
                ) AS resolved_customer
            FROM invoices_filtered f
        ),
        invoices_production_only AS (
            SELECT ir.*
            FROM invoices_resolved ir
            JOIN dim_customer dc
              ON LOWER(dc.customer_name) = LOWER(ir.resolved_customer)
            WHERE dc.activity_class = 'Production'
              AND dc.active = TRUE
        ),
        fifo_consumed AS (
            SELECT
                invoice_num,
                CASE
                    WHEN output_type IN ('kit', 'demo')             THEN 'demo'
                    WHEN output_type = 'bag'                         THEN 'bag'
                    WHEN output_type = 'overwrap'                    THEN 'overwrap'
                    WHEN output_type IN ('packout', 'pickpack',
                                          'pack_out', 'pick_pack')   THEN 'pickpack'
                    ELSE output_type
                END AS view_name,
                SUM(units_applied) AS units_applied
            FROM stg_wip_fifo_applied
            WHERE accrual_period = :period
            GROUP BY 1, 2
        )
        SELECT
            ipo.view_name,
            ipo.doc_number,
            ipo.customer_name,
            ipo.resolved_customer,
            ipo.units                                       AS invoiced_units,
            COALESCE(fc.units_applied, 0)                   AS fifo_units,
            ipo.units - COALESCE(fc.units_applied, 0)       AS unmatched_units
        FROM invoices_production_only ipo
        LEFT JOIN fifo_consumed fc
            ON fc.invoice_num = ipo.doc_number
           AND fc.view_name   = ipo.view_name
        WHERE ROUND((ipo.units - COALESCE(fc.units_applied, 0))::numeric, 0) > 0
        ORDER BY unmatched_units DESC
    """), engine, params={"period": period})

    if df.empty:
        return {"severity": "pass", "metric_value": 0, "details": []}

    total_unmatched = float(df["unmatched_units"].sum())

    # Orphan invoices represent operational data quality gaps (smartsheet
    # production wasn't logged for billed units), not system bugs. The
    # unmatched portion gets zero FIFO consumption, which surfaces as
    # inflated margin on profitability until production is logged. Severity
    # is capped at 'warn' — flags for ops follow-up but never blocks close.
    severity = "warn"

    return {
        "severity":     severity,
        "metric_value": total_unmatched,
        "details":      df.to_dict(orient="records"),
    }


def _check_future_period_consumption(period: str, threshold) -> dict:
    """
    Tripwire. FIFO events in this period that consumed layers from a period
    AFTER this period's accrual date. Should never happen — FIFO can only
    consume layers from the current period or earlier periods.

    Any row returned = fail. This is a regression detector for the period
    gate. threshold is ignored.
    """
    df = pd.read_sql(text("""
        SELECT
            f.accrual_period       AS event_period,
            l.accrual_period       AS layer_period,
            f.invoice_num,
            f.customer_program,
            f.iso_week_produced,
            f.output_type,
            f.units_applied,
            ROUND(f.applied_cost::numeric, 2) AS applied_cost
        FROM stg_wip_fifo_applied f
        JOIN stg_wip_production_layers l
          ON l.cost_center      = f.cost_center
         AND l.iso_week         = f.iso_week_produced
         AND l.output_type      = f.output_type
         AND COALESCE(
                (SELECT a.canonical_name FROM dim_customer_alias a
                 WHERE LOWER(a.alias) = LOWER(l.customer_program)
                   AND a.active = TRUE
                   AND COALESCE(a.exclude, FALSE) = FALSE
                 LIMIT 1),
                l.customer_program
            ) = COALESCE(
                (SELECT a.canonical_name FROM dim_customer_alias a
                 WHERE LOWER(a.alias) = LOWER(f.customer_program)
                   AND a.active = TRUE
                   AND COALESCE(a.exclude, FALSE) = FALSE
                 LIMIT 1),
                f.customer_program
            )
        WHERE f.accrual_period = :period
          AND TO_DATE(l.accrual_period, 'YYYY-MM')
              > TO_DATE(f.accrual_period, 'YYYY-MM')
    """), engine, params={"period": period})

    if df.empty:
        return {"severity": "pass", "metric_value": 0, "details": []}

    return {
        "severity":     "fail",
        "metric_value": float(len(df)),
        "details":      df.to_dict(orient="records"),
    }


def _check_layers_without_smartsheet_basis(period: str, threshold) -> dict:
    """
    Production layers in stg_wip_production_layers for customer_programs
    that don't appear in any smartsheet (demo / ogp / overwrap) for the
    period, AFTER both sides resolve via dim_customer_alias.

    The overwrap smartsheet uses resolve_overwrap_customer() which returns
    a raw customer name; that raw name is then run through
    dim_customer_alias to get to canonical so it matches the layer side's
    canonicalized form.

    threshold is the $ amount above which severity escalates from warn
    to fail. Default 500.
    """
    df = pd.read_sql(text("""
        WITH smartsheet_programs AS (
            SELECT DISTINCT 
                LOWER(COALESCE(a.canonical_name, s.customer)) AS resolved
            FROM stg_smartsheet_demo s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer)
               AND a.active = TRUE
               AND COALESCE(a.exclude, FALSE) = FALSE
            WHERE s.accrual_month = :period
              AND s.number_of_cases_completed > 0

            UNION

            SELECT DISTINCT 
                LOWER(COALESCE(a.canonical_name, s.job_name))
            FROM stg_smartsheet_ogp s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.job_name)
               AND a.active = TRUE
               AND COALESCE(a.exclude, FALSE) = FALSE
            WHERE s.accrual_month = :period
              AND s.daily_production_complete > 0

            UNION

            SELECT DISTINCT 
                LOWER(COALESCE(
                    (SELECT a.canonical_name FROM dim_customer_alias a
                     WHERE LOWER(a.alias) = LOWER(
                         resolve_overwrap_customer(s.customer, s.project_name, s.work_order_number)
                     )
                       AND a.active = TRUE
                       AND COALESCE(a.exclude, FALSE) = FALSE
                     LIMIT 1),
                    resolve_overwrap_customer(s.customer, s.project_name, s.work_order_number)
                ))
            FROM stg_smartsheet_overwrap s
            WHERE s.accrual_month = :period
              AND s.units_produced > 0
        ),
        resolved_layers AS (
            SELECT
                l.cost_center,
                l.customer_program,
                l.output_type,
                l.units_produced,
                l.cost_per_unit,
                LOWER(COALESCE(
                    (SELECT a.canonical_name FROM dim_customer_alias a
                     WHERE LOWER(a.alias) = LOWER(l.customer_program)
                       AND a.active = TRUE
                       AND COALESCE(a.exclude, FALSE) = FALSE
                     LIMIT 1),
                    l.customer_program
                )) AS resolved_program
            FROM stg_wip_production_layers l
            WHERE l.accrual_period = :period
        )
        SELECT
            rl.cost_center,
            rl.customer_program,
            rl.output_type,
            rl.resolved_program,
            SUM(rl.units_produced)                                       AS units,
            ROUND(SUM(rl.units_produced * rl.cost_per_unit)::numeric, 2) AS pool
        FROM resolved_layers rl
        WHERE rl.resolved_program NOT IN (
            SELECT resolved FROM smartsheet_programs WHERE resolved IS NOT NULL
        )
        GROUP BY 1, 2, 3, 4
        ORDER BY pool DESC
    """), engine, params={"period": period})

    if df.empty:
        return {"severity": "pass", "metric_value": 0, "details": []}

    total_pool = float(df["pool"].sum())
    fail_threshold = float(threshold) if threshold is not None else 500.0

    severity = "fail" if total_pool > fail_threshold else "warn"

    return {
        "severity":     severity,
        "metric_value": total_pool,
        "details":      df.to_dict(orient="records"),
    }


# ---------------------------------------------------------------------------
# Allocation compute
# ---------------------------------------------------------------------------

def _activity_dfs(period: str) -> dict[str, pd.DataFrame]:
    return {
        "demo":      get_demo_units(period),
        "ogp":       get_ogp_units(period),
        "ow":        get_ow_units(period),
        "receiving": get_receiving_units(period),
        "shipments": get_shipment_units(period),
        "inventory": get_inventory_units(period),
    }


@st.cache_data(ttl=120, show_spinner="Computing employee allocations...")
def _cached_employee_alloc_with_warnings(period: str):
    """
    Single underlying compute slot for this module.

    All other consumers (_cached_employee_alloc, get_approved_cogs_pools,
    get_approved_cogs_pools_weekly, build_approved_employee_overview,
    build_employee_heuristic_allocations) derive from this slot, so
    wlc.build_employee_allocations runs at most once per period per render
    cycle. Returns the (df, warnings) tuple.
    """
    return wlc.build_employee_allocations(
        period,
        _activity_dfs(period),
        get_revenue_by_program(period),
        return_warnings=True,
    )


@st.cache_data(ttl=600, show_spinner=False)
def _cached_employee_alloc(period: str) -> pd.DataFrame:
    """
    No-warnings view of the cached compute. Derives from
    _cached_employee_alloc_with_warnings so the underlying pandas work runs
    once, not twice. Both slots are retained for call-site stability.
    """
    df, _ = _cached_employee_alloc_with_warnings(period)
    return df


@st.cache_data(ttl=300, show_spinner=False)
def _load_employee_alloc_from_persisted(period: str) -> pd.DataFrame:
    """
    Returns the equivalent of employee_alloc_df, read directly from
    stg_labor_incurred_employee. Used by the Allocation tab when the period
    is locked, so the locked view does not recompute from raw inputs.

    Shape match notes:
      - cost_type is derived from labor_type
          ('direct_cogs', 'temp') -> 'COGS'
          ('direct_sga')           -> 'SGA'
      - iso_week is set to NULL. Weekly granularity is not preserved on
        commit; the locked rendering path does not surface the weekly
        Activity Driver Overview, so this is fine.
      - source_assignment is set to '' to match the unlocked-path shape.
    """
    df = pd.read_sql(text("""
        SELECT
            source_bucket,
            target_program,
            labor_type,
            employee_name,
            labor_source,
            role_detail,
            activity_driver,
            activity_value,
            weight,
            allocated_cost,
            CASE
                WHEN labor_type IN ('direct_cogs', 'temp') THEN 'COGS'
                WHEN labor_type = 'direct_sga'             THEN 'SGA'
                ELSE 'COGS'
            END                          AS cost_type,
            CAST(NULL AS INTEGER)        AS iso_week,
            ''::text                     AS source_assignment
        FROM stg_labor_incurred_employee
        WHERE accrual_period = :period
    """), engine, params={"period": period})
    return df


def _clear_employee_alloc_caches() -> None:
    """Bust the full employee-allocation compute chain so the next render
    rebuilds against current driver data (containers, returns, e-comm config,
    revenue, demo/ogp/ow units, etc.). The compute slot is cached with a
    10-min TTL and does not self-invalidate when driver inputs change
    underneath it, so any code path that mutates driver data must call this
    to avoid serving a stale build.

    Note: container data is read fresh inside build_employee_allocations
    (not cached at the reader level), so clearing the compute slot alone is
    sufficient for container changes. The activity feeder caches are cleared
    here too so the same button handles demo/ogp/ow/receiving/shipments/
    inventory and revenue updates."""
    _cached_employee_alloc_with_warnings.clear()
    _cached_employee_alloc.clear()
    _activity_dfs.clear()
    get_revenue_by_program.clear()

    
def compute_cogs_allocation(pools_df: pd.DataFrame, activity: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Deprecated under 1b.3 — returns empty DataFrame.
    The allocation tab now reads the program-level view directly from
    build_employee_allocations() via employee_alloc_df. Kept as a
    no-op for the few callers that still expect a DataFrame.
    """
    return pd.DataFrame()


def compute_sga_allocation(sga_pool: float, revenue_df: pd.DataFrame) -> pd.DataFrame:
    """Deprecated under 1b.3 — returns empty DataFrame. SG&A now flows through
    build_employee_allocations() based on each line's cost_type."""
    return pd.DataFrame()

# ---------------------------------------------------------------------------
# Allocation overview helpers
# ---------------------------------------------------------------------------

def build_approved_employee_overview(
    period: str,
    cost_type_filter: str = "All",
    emp_alloc: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reads from new employee allocation table. Groups by cost center.

    If emp_alloc is provided, uses it directly. This is the locked-period
    short-circuit path where the page has already loaded the snapshot from
    stg_labor_incurred_employee and wants to avoid triggering the cached
    compute.
    """
    if emp_alloc is None:
        emp_alloc = _cached_employee_alloc(period)
    if emp_alloc.empty:
        return pd.DataFrame(columns=["Program","Employees","Approved Labor","Sources"]), pd.DataFrame()

    if cost_type_filter in ("COGS", "SGA"):
        emp_alloc = emp_alloc[emp_alloc["cost_type"] == cost_type_filter].copy()

    # Detail: one row per (cost_center, employee, source) aggregating lines
    detail = emp_alloc.groupby(
        ["source_bucket", "employee_name", "role_detail", "labor_source"],
        as_index=False,
    )["allocated_cost"].sum()
    detail.rename(columns={
        "source_bucket":   "effective_bucket",
        "allocated_cost":  "total_labor_cost",
        "role_detail":     "role_detail",
    }, inplace=True)
    detail["source_assignment"] = ""

    grouped = detail.groupby("effective_bucket", as_index=False).agg(
        employees=("employee_name", lambda s: s.nunique()),
        approved_labor=("total_labor_cost", "sum"),
        sources=("labor_source", lambda s: ", ".join(sorted(set(s)))),
    )
    grouped.rename(columns={
        "effective_bucket": "Program", "employees": "Employees",
        "approved_labor": "Approved Labor", "sources": "Sources",
    }, inplace=True)
    grouped = grouped.sort_values(["Approved Labor","Program"], ascending=[False,True]).reset_index(drop=True)

    return grouped, detail.sort_values(["effective_bucket","employee_name","labor_source"]).reset_index(drop=True)


def build_activity_driver_overview(
    cogs_alloc: pd.DataFrame,
    activity: dict[str, pd.DataFrame],
    pools_weekly: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """
    Builds the per-bucket activity driver display DataFrame.

    Weekly drivers (demo, ogp, ow): have week_start/iso_week — broken out by
    ISO week with weekly labor cost from pools_weekly.

    Period drivers (receiving, shipments, inventory): no week_start — shown as
    a flat period-total with the full period cost pool. Weekly cost breakdown
    is not applicable for these drivers.
    """
    # Map cost center name -> activity key used in the activity dict
    # Only for the 6 core drivers that have dedicated activity readers in _activity_dfs
    bucket_to_activity_key = {
        "Demo":             "demo",
        "OGP":              "ogp",
        "Overwrap":         "ow",
        "Receiving Pallet": "receiving",
        "Receiving Parcel": "receiving",
        "Shipping LTL":     "shipments",
        "Shipping Parcel":  "shipments",
        "Inventory":        "inventory",
    }

    output = {}

    for bucket, activity_key in bucket_to_activity_key.items():
        act_df = activity.get(activity_key, pd.DataFrame()).copy()
        if act_df.empty:
            continue

        # Determine whether this driver has ISO-week granularity
        has_week = "week_start" in act_df.columns and act_df["week_start"].notna().any()

        if has_week:
            act_df["total_week_units"] = act_df.groupby("week_start")["units"].transform("sum")
            act_df["weight"]           = act_df["units"] / act_df["total_week_units"].replace(0, 1)
            act_df["Week"]             = act_df["week_start"].dt.strftime("%Y-%m-%d")
        else:
            total_units                = float(act_df["units"].sum())
            act_df["total_week_units"] = total_units
            act_df["weight"]           = act_df["units"] / total_units if total_units > 0 else 0.0
            act_df["week_start"]       = pd.NaT
            act_df["iso_week"]         = 0
            act_df["Week"]             = "Period Total"

        if not pools_weekly.empty:
            weekly_sub = pools_weekly[pools_weekly["effective_bucket"] == bucket].copy()
            if not weekly_sub.empty and has_week:
                pivot = weekly_sub.pivot_table(
                    index="iso_week", columns="labor_type",
                    values="labor_pool", aggfunc="sum", fill_value=0,
                ).reset_index()
                for col in ["direct_cogs", "temp"]:
                    if col not in pivot.columns:
                        pivot[col] = 0.0
                pivot.rename(columns={"direct_cogs": "Direct Cost", "temp": "Temp Cost"}, inplace=True)
                act_df = act_df.merge(pivot, on="iso_week", how="left").fillna(
                    {"Direct Cost": 0.0, "Temp Cost": 0.0}
                )
            else:
                direct_total          = float(weekly_sub[weekly_sub["labor_type"] == "direct_cogs"]["labor_pool"].sum()) if not weekly_sub.empty else 0.0
                temp_total            = float(weekly_sub[weekly_sub["labor_type"] == "temp"]["labor_pool"].sum()) if not weekly_sub.empty else 0.0
                act_df["Direct Cost"] = act_df["weight"] * direct_total
                act_df["Temp Cost"]   = act_df["weight"] * temp_total
        else:
            act_df["Direct Cost"] = act_df["weight"] * 0.0
            act_df["Temp Cost"]   = act_df["weight"] * 0.0

        act_df["Total Allocated"] = act_df["Direct Cost"] + act_df["Temp Cost"]
        act_df = act_df.rename(columns={"iso_week": "ISO Week", "customer": "Program", "units": "Units"})
        act_df = act_df.sort_values(
            ["week_start", "Total Allocated"] if has_week else ["Total Allocated"],
            ascending=[True, False]             if has_week else [False],
        ).reset_index(drop=True)
        output[bucket] = act_df

    return output


def build_employee_heuristic_allocations(
    period: str, activity: dict[str, pd.DataFrame], revenue_df: pd.DataFrame,
    cost_type_filter: str = "All",
    return_warnings: bool = False,
):
    """
    Thin wrapper around the new Phase 1b.3 engine. Now reads from the
    cached _cached_employee_alloc / _cached_employee_alloc_with_warnings
    wrappers so all four call sites in this module share one compute.

    The activity and revenue_df parameters are accepted for backward-compat
    with the existing call signature but are unused — the cached wrappers
    compute their own (and read those from their own cache slots).
    """
    if return_warnings:
        result, warnings = _cached_employee_alloc_with_warnings(period)
        if cost_type_filter in ("COGS", "SGA") and not result.empty:
            result = result[result["cost_type"] == cost_type_filter].copy()
        return result, warnings

    result = _cached_employee_alloc(period)
    if result.empty or cost_type_filter == "All":
        return result
    return result[result["cost_type"] == cost_type_filter].copy() if cost_type_filter in ("COGS", "SGA") else result
 
 
def build_program_reconciliation(pools_df, cogs_alloc, sga_pool, sga_alloc, employee_alloc_df) -> pd.DataFrame:
    approved_rows = []
    if not pools_df.empty:
        for _, row in pools_df.iterrows():
            approved_rows.append({
                "bucket": row["effective_bucket"], "labor_type": row["labor_type"],
                "approved_pool": float(row["labor_pool"]),
            })
    if sga_pool:
        approved_rows.append({"bucket": "SGA", "labor_type": "direct_sga", "approved_pool": float(sga_pool)})

    approved_df = pd.DataFrame(approved_rows)
    if approved_df.empty:
        return pd.DataFrame()

    approved_summary = approved_df.groupby(["bucket","labor_type"], as_index=False)["approved_pool"].sum()

    alloc_frames = []
    if not cogs_alloc.empty:
        alloc_frames.append(cogs_alloc[["bucket","labor_type","activity_value","allocated_cost"]].copy())
    if not sga_alloc.empty:
        alloc_frames.append(sga_alloc[["bucket","labor_type","activity_value","allocated_cost"]].copy())

    if alloc_frames:
        alloc_df      = pd.concat(alloc_frames, ignore_index=True)
        alloc_summary = alloc_df.groupby(["bucket","labor_type"], as_index=False).agg(
            driver_total=("activity_value","sum"), allocated_total=("allocated_cost","sum"),
        )
    else:
        alloc_summary = pd.DataFrame(columns=["bucket","labor_type","driver_total","allocated_total"])

    if employee_alloc_df.empty:
        employee_summary = pd.DataFrame(columns=["bucket","labor_type","program_count","employee_count"])
    else:
        employee_tmp = employee_alloc_df.copy()
        employee_tmp["labor_type"] = employee_tmp["labor_source"].map({
            "Direct COGS": "direct_cogs", "Temp": "temp", "Direct SG&A": "direct_sga",
        })
        employee_tmp["bucket"] = employee_tmp["source_bucket"]
        employee_summary = employee_tmp.groupby(["bucket","labor_type"], as_index=False).agg(
            program_count=("target_program", lambda s: s.nunique()),
            employee_count=("employee_name", lambda s: s.nunique()),
        )

    driver_label_map = {
        "Demo":             "Units (Demo Kits)",
        "OGP":              "Units (OGP Bags)",
        "Overwrap":         "Units (OW)",
        "Receiving Pallet": "Pallets (v6)",
        "Receiving Parcel": "Parcels (non-v6)",
        "Shipping LTL":     "LTL Orders (v6)",
        "Shipping Parcel":  "Parcel Orders (non-v6)",
        "Container Unload": "Container Pallets",
        "Inventory":        "Pallets (3mo Avg)",
        "Returns":          "Return Count",
        "E-Commerce Picking": "E-Comm Orders",
        "Facilities":       "Square Feet",
        "Purchasing":       "Revenue (Purchasing)",
        "IT":               "Revenue",
        "Marketing":        "Revenue",
        "Finance":          "Revenue",
        "Executive":        "Revenue",
    }

    def _driver_type(bucket):
        return driver_label_map.get(bucket, "Direct Assignment")

    recon = approved_summary.merge(alloc_summary,  on=["bucket","labor_type"], how="left")
    recon = recon.merge(employee_summary,          on=["bucket","labor_type"], how="left")
    recon["driver_total"]    = recon["driver_total"].fillna(0.0)
    recon["allocated_total"] = recon["allocated_total"].fillna(0.0)
    recon["program_count"]   = recon["program_count"].fillna(0).astype(int)
    recon["employee_count"]  = recon["employee_count"].fillna(0).astype(int)
    recon["variance"]        = recon["approved_pool"] - recon["allocated_total"]
    recon["driver_type"]     = recon["bucket"].map(_driver_type)
    return recon.sort_values(["bucket","labor_type"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def _pct(v: float) -> str:
    return f"{v * 100:.2f}%"


def _dollar(v):
    if pd.isna(v) or v == "":
        return ""
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


# ---------------------------------------------------------------------------
# E-Commerce config tab renderer
# ---------------------------------------------------------------------------

def render_ecomm_config_tab(period: str, reviewer_name: str):
    st.subheader("E-Commerce Program Election")
    st.caption(
        "Select which E-Commerce programs are active for this period. "
        "Labor costs routed to the E-Commerce cost center will be split "
        "by revenue weight across elected programs only. "
        "Allocation cannot be committed until at least one program is elected."
    )

    all_customers_df = get_ecomm_customers()
    current_config   = get_ecomm_config(period)

    if all_customers_df.empty:
        st.warning("No E-Commerce customers found in dim_customer.")
        return

    period_committed = wla.is_period_committed(period)
    if period_committed:
        st.warning(
            f"Period {period} allocation is **committed**. E-Commerce "
            "elections are read-only — adding or removing programs would "
            "silently invalidate the locked allocation. Unlock from the "
            "Allocation tab before making changes."
        )

    def display_label(row) -> str:
        return f"{row['customer_name']}  ({row['parent_name']})" if row["parent_name"] else row["customer_name"]

    all_customers_df["display_label"] = all_customers_df.apply(display_label, axis=1)
    label_to_key = dict(zip(all_customers_df["display_label"], all_customers_df["canonical_key"]))
    key_to_label = dict(zip(all_customers_df["canonical_key"], all_customers_df["display_label"]))

    current_keys   = current_config["canonical_key"].tolist() if not current_config.empty else []
    current_labels = [key_to_label[k] for k in current_keys if k in key_to_label]

    st.markdown("#### Active Programs for Period")

    if current_config.empty:
        st.warning("No E-Commerce programs elected for this period. Allocation will be blocked until configured.")
    else:
        display_config = current_config.rename(columns={
            "customer_name": "Program", "set_by": "Set By", "set_at": "Set At",
        })[["Program","Set By","Set At"]]
        st.dataframe(display_config, use_container_width=True, hide_index=True)

        weights_df = get_ecomm_revenue_weights(period)
        if not weights_df.empty:
            with st.expander("Revenue weights for elected programs", expanded=False):
                show_weights = weights_df[["customer_name","revenue","weight"]].copy()
                show_weights.columns = ["Program","Revenue","Weight"]
                show_weights["Revenue"] = show_weights["Revenue"].map(_dollar)
                show_weights["Weight"]  = show_weights["Weight"].map(_pct)
                st.dataframe(show_weights, use_container_width=True, hide_index=True)
                if weights_df["revenue"].sum() == 0:
                    st.warning(
                        "No revenue found for elected programs in this period. "
                        "Check stg_product_service_detail for contract_completion_date matches."
                    )

    if period_committed:
        return

    st.markdown("---")
    st.markdown("#### Update Elections")

    selected_labels = st.multiselect(
        "Select active E-Commerce programs for this period",
        options=all_customers_df["display_label"].tolist(),
        default=current_labels,
        key=f"ecomm_multiselect_{period}",
    )
    selected_keys = [label_to_key[lbl] for lbl in selected_labels]

    col_save, col_clear, _ = st.columns([2, 2, 4])
    with col_save:
        if st.button("Save Elections", key=f"ecomm_save_{period}", type="primary", use_container_width=True):
            if not reviewer_name:
                st.warning("Enter your name in the Reviewer's Name field above before saving.")
            elif not selected_keys:
                st.warning("Select at least one program before saving.")
            else:
                save_ecomm_config(period, selected_keys, reviewer_name)
                get_ecomm_config.clear()
                st.success(f"Saved {len(selected_keys)} E-Commerce program(s) for {period}.")
                st.rerun()
    with col_clear:
        if st.button("Clear Elections", key=f"ecomm_clear_{period}", type="secondary", use_container_width=True):
            if not reviewer_name:
                st.warning("Enter your name above before clearing.")
            else:
                save_ecomm_config(period, [], reviewer_name)
                get_ecomm_config.clear()
                st.rerun()


# ---------------------------------------------------------------------------
# Allocation tab renderer
# ---------------------------------------------------------------------------
SECTION_HEADER_COLOR = "#2E86C1"


def render_allocation_section(title, color, alloc_df, activity_label="Units"):
    if alloc_df.empty:
        st.info(f"No data for {title}.")
        return
    if title:
        st.markdown(f"#### {title}")
    pool_total = alloc_df["allocated_cost"].sum()
    pool_val = float(alloc_df['labor_pool'].iloc[0])
    st.caption("Pool: ${:,.2f}     Allocated: ${:,.2f}     Programs: {}".format(
            pool_val, pool_total, alloc_df['program'].nunique()
        ))
    # Only render chart when there are multiple programs to compare
    if len(alloc_df) > 2:
        chart = (
            alt.Chart(alloc_df).mark_bar(color=color)
            .encode(
                x=alt.X("allocated_cost:Q", title="Allocated Cost ($)", axis=alt.Axis(format="$,.0f")),
                y=alt.Y("program:N", sort="-x", title=None),
                tooltip=[
                    alt.Tooltip("program:N",        title="Program"),
                    alt.Tooltip("activity_value:Q", title=activity_label, format=",.0f"),
                    alt.Tooltip("weight:Q",         title="Weight",       format=".2%"),
                    alt.Tooltip("allocated_cost:Q", title="Allocated $",  format="$,.2f"),
                ],
            )
            .properties(height=min(300, max(120, len(alloc_df) * 26)))
        )
        st.altair_chart(chart, use_container_width=True)
    display = alloc_df.groupby("program", as_index=False).agg(
        activity_value=("activity_value", "sum"),
        total_activity=("total_activity", "first"),
        weight=("weight",         "sum"),
        allocated_cost=("allocated_cost", "sum"),
        labor_pool=("labor_pool",     "first"),
    ).copy()
    display = display.drop(columns=["labor_pool"])
    display.columns = ["Program", activity_label, f"Total {activity_label}", "Weight", "Allocated Cost"]
    display[activity_label]            = display[activity_label].map("{:,.0f}".format)
    display[f"Total {activity_label}"] = display[f"Total {activity_label}"].map("{:,.0f}".format)
    display["Weight"]                  = display["Weight"].map(_pct)
    display["Allocated Cost"]          = display["Allocated Cost"].map(_dollar)
    st.dataframe(display, use_container_width=True, hide_index=True)
    st.markdown("")


def _render_coverage_warning(act_df: pd.DataFrame, driver_label: str, period: str):
    if act_df.empty or "report_start" not in act_df.columns:
        return
    report_start = pd.to_datetime(act_df["report_start"].min(), errors="coerce")
    report_end   = pd.to_datetime(act_df["report_end"].max(), errors="coerce") if "report_end" in act_df.columns else None

    if pd.isna(report_start):
        return

    period_start = pd.to_datetime(period + "-01")
    period_end   = period_start + pd.offsets.MonthEnd(0)

    coverage_ok = (report_start <= period_start) and (report_end is not None and report_end >= period_end)

    if coverage_ok:
        st.caption(
            f"{driver_label} coverage: {report_start.strftime('%m/%d/%Y')} "
            f"to {report_end.strftime('%m/%d/%Y')} — full month confirmed."
        )
    else:
        end_str = report_end.strftime("%m/%d/%Y") if report_end and not pd.isna(report_end) else "unknown"
        st.warning(
            f"{driver_label} report coverage: {report_start.strftime('%m/%d/%Y')} to {end_str}. "
            f"Expected full month {period_start.strftime('%m/%d/%Y')} to {period_end.strftime('%m/%d/%Y')}. "
            "Pull the full-month report before committing allocation."
        )


def render_allocation_tab(period: str, reviewer_name: str, cost_type_filter: str = "All"):
    st.subheader("Labor Allocation")
    st.caption(
        "Allocates approved labor pools to programs based on activity drivers for COGS "
        "and revenue for SG&A."
    )

    # -------------------------------------------------------------------------
    # 1. Guards — collect all, show all, then block
    # -------------------------------------------------------------------------
    errors = []

    if not is_ecomm_configured(period):
        errors.append(
            "E-Commerce programs have not been elected for this period. "
            "Go to the E-Commerce Config tab and select the active programs "
            "before running allocation."
        )

    coverage = check_ukg_coverage(period)
    if not coverage["following_ok"]:
        errors.append(
            f"The {coverage['next_month_name']} boundary paycheck has not been loaded. "
            f"Upload the UKG direct hire report containing the first pay date of {coverage['next_month_name']} "
            f"before committing {period} allocation. "
            "Committing without it will understate labor cost for this period."
        )

    if errors:
        for msg in errors:
            st.error(msg)
        return
    
    # -------------------------------------------------------------------------
    # 2. Load review data
    # -------------------------------------------------------------------------
    direct_emp = wla.list_employees_for_review(period, 'direct')
    temp_emp   = wla.list_employees_for_review(period, 'temp')

    total_rows    = len(direct_emp) + len(temp_emp)
    reviewed_rows = (
        int(direct_emp['reviewed'].fillna(False).sum()) +
        int(temp_emp['reviewed'].fillna(False).sum())
    )
    all_reviewed  = (total_rows > 0) and (reviewed_rows == total_rows)

    # -------------------------------------------------------------------------
    # 3. Load OR compute — locked periods read from persisted snapshot
    # -------------------------------------------------------------------------
    existing  = get_existing_allocation(period)
    is_locked = not existing.empty

    existing  = get_existing_allocation(period)
    is_locked = not existing.empty

    if is_locked:
        # Locked path — single SQL read, zero recompute.
        employee_alloc_df = _load_employee_alloc_from_persisted(period)
        alloc_warnings = []
        approved_summary, approved_detail = build_approved_employee_overview(
            period, cost_type_filter, emp_alloc=employee_alloc_df,
        )
        # Stubs for sections that are skipped or unused in the locked path.
        pools_df          = pd.DataFrame()
        pools_weekly      = pd.DataFrame()
        sga_pool          = 0.0
        activity          = {}
        revenue_df        = pd.DataFrame()
        cogs_alloc        = pd.DataFrame()
        sga_alloc         = pd.DataFrame()
        reconciliation_df = pd.DataFrame()
        driver_overview   = {}
    else:
        pools_df = get_approved_cogs_pools(period)

        sga_pool = get_approved_sga_pool(period)
        if cost_type_filter == "WIP":
            sga_pool = 0.0

        activity = _activity_dfs(period)
        revenue_df = get_revenue_by_program(period)
        pools_weekly = get_approved_cogs_pools_weekly(period)
        pools_weekly["effective_bucket"] = pools_weekly["effective_bucket"].map(_normalize_bucket)
        cogs_alloc = compute_cogs_allocation(pools_df, activity)
        sga_alloc  = compute_sga_allocation(sga_pool, revenue_df)
        employee_alloc_df, alloc_warnings = build_employee_heuristic_allocations(
            period, activity, revenue_df, cost_type_filter, return_warnings=True,
        )
        reconciliation_df = build_program_reconciliation(
            pools_df, cogs_alloc, sga_pool, sga_alloc, employee_alloc_df,
        )
        driver_overview = build_activity_driver_overview(cogs_alloc, activity, pools_weekly)
        approved_summary, approved_detail = build_approved_employee_overview(
            period, cost_type_filter,
        )

    # -------------------------------------------------------------------------
    # 4. Display
    # -------------------------------------------------------------------------

    if not all_reviewed:
        st.warning(
            f"{reviewed_rows} of {total_rows} rows reviewed. "
            "Complete review on the other tabs before committing allocation."
        )

    # =========================================================================
    # PROGRAM ALLOCATION OVERVIEW — top of page, this is the headline output
    # =========================================================================
    st.markdown(f'<h3 style="color:{SECTION_HEADER_COLOR};">Incurred Labor by Program - Summary</h3>', unsafe_allow_html=True)
    st.caption(
        "Incurred labor cost by revenue program across all cost centers and labor sources. Pending application to revenue."
    )

    if employee_alloc_df.empty:
        st.info("No reviewed employees available for allocation detail.")
    else:
        def _assignment_type(row) -> str:
            bucket = str(row.get("source_bucket") or "")
            source = str(row.get("labor_source") or "")
            driver = str(row.get("activity_driver") or "")
            if source == "Direct SG&A" or bucket in {"SGA", "E-Commerce"}:
                return "Revenue Weighted"
            if bucket in {"Purchasing", "Facilities"} or "Revenue (" in driver:
                return "Revenue Weighted"
            if driver == "Direct Assignment":
                return "Direct Assignment"
            return "Activity Weighted"

        labeled = employee_alloc_df.copy()
        labeled["Assignment Type"] = labeled.apply(_assignment_type, axis=1)

        # --- Stacked bar chart by program and labor source ---
        chart_df = labeled.groupby(
            ["target_program", "labor_source"], as_index=False
        )["allocated_cost"].sum()

        # Sort programs by total allocated descending for chart order
        program_order = (
            chart_df.groupby("target_program")["allocated_cost"]
            .sum()
            .sort_values(ascending=True)  # ascending so largest is at top in horizontal bar
            .index.tolist()
        )

        _SOURCE_COLORS = {
            "Direct COGS": "#1f77b4",
            "Temp":        "#ff7f0e",
            "Direct SG&A": "#5470c6",
        }

        if not chart_df.empty and chart_df["allocated_cost"].sum() > 0:
            # Top 15 programs by total allocated
            top_programs = (
                chart_df.groupby("target_program")["allocated_cost"]
                .sum()
                .nlargest(15)
                .index.tolist()
            )
            chart_df_top = chart_df[chart_df["target_program"].isin(top_programs)].copy()

            program_order = (
                chart_df_top.groupby("target_program")["allocated_cost"]
                .sum()
                .sort_values(ascending=True)
                .index.tolist()
            )

            chart = (
                alt.Chart(chart_df_top).mark_bar()
                .encode(
                    x=alt.X(
                        "allocated_cost:Q",
                        title="Allocated Labor ($)",
                        axis=alt.Axis(format="$,.0f"),
                    ),
                    y=alt.Y(
                        "target_program:N",
                        sort=program_order,
                        title=None,
                    ),
                    color=alt.Color(
                        "labor_source:N",
                        title="Source",
                        scale=alt.Scale(
                            domain=list(_SOURCE_COLORS.keys()),
                            range=list(_SOURCE_COLORS.values()),
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip("target_program:N", title="Program"),
                        alt.Tooltip("labor_source:N",   title="Source"),
                        alt.Tooltip("allocated_cost:Q", title="Allocated $", format="$,.2f"),
                    ],
                )
                .properties(height=400)
            )
            st.altair_chart(chart, use_container_width=True)
            total_programs = chart_df["target_program"].nunique()
            st.caption(
                f"Showing top 15 of {total_programs} programs by allocated labor. "
                "Full breakdown in the table below."
            )

        # --- Summary table by program ---
        program_view = labeled.groupby(
            ["Assignment Type", "target_program", "labor_source"], as_index=False
        ).agg(
            allocated_labor=("allocated_cost", "sum"),
            employees=("employee_name", lambda s: s.nunique()),
            cost_centers=("source_bucket", lambda s: ", ".join(sorted(set(s)))),
        )
        program_view.rename(columns={
            "Assignment Type":  "Assignment Type",
            "target_program":   "Target Program",
            "labor_source":     "Source",
            "allocated_labor":  "Allocated Labor",
            "employees":        "Employees",
            "cost_centers":     "Cost Centers",
        }, inplace=True)
        program_view = program_view.sort_values(
            ["Target Program", "Assignment Type", "Source"],
            ascending=[True, True, True],
        ).reset_index(drop=True)
        display_pv = program_view.copy()
        display_pv["Allocated Labor"] = display_pv["Allocated Labor"].map(_dollar)
        st.dataframe(display_pv, use_container_width=True, hide_index=True)

        with st.expander("Employee-level detail", expanded=False):
            detail = labeled.copy()
            type_opts = ["All"] + sorted(detail["Assignment Type"].unique().tolist())
            col_f1, col_f2 = st.columns(2)
            with col_f1:
                sel_type = st.selectbox(
                    "Assignment Type",
                    options=type_opts,
                    index=0,
                    key=f"heuristic_type_filter_{period}_{cost_type_filter}",
                )
            with col_f2:
                prog_opts = ["All"] + sorted(
                    detail["target_program"].dropna().astype(str).unique().tolist(), key=str.upper
                )
                sel_prog = st.selectbox(
                    "Target Program",
                    options=prog_opts,
                    index=0,
                    key=f"heuristic_program_filter_{period}_{cost_type_filter}",
                )

            if sel_type != "All":
                detail = detail[detail["Assignment Type"] == sel_type].copy()
            if sel_prog != "All":
                detail = detail[detail["target_program"].astype(str) == sel_prog].copy()

            detail.rename(columns={
                "target_program":    "Target Program",
                "employee_name":     "Employee",
                "labor_source":      "Source",
                "source_bucket":     "Cost Center",
                "source_assignment": "Original Assignment",
                "role_detail":       "Role / Detail",
                "activity_driver":   "Driver",
                "activity_value":    "Driver Value",
                "weight":            "Weight",
                "allocated_cost":    "Allocated Labor",
            }, inplace=True)
            detail["Weight"]          = detail["Weight"].map(_pct)
            detail["Driver Value"]    = detail["Driver Value"].map("{:,.2f}".format)
            detail["Allocated Labor"] = detail["Allocated Labor"].map(_dollar)
            st.dataframe(
                detail[[
                    "Assignment Type", "Target Program", "Employee", "Source",
                    "Cost Center", "Role / Detail", "Driver", "Driver Value",
                    "Weight", "Allocated Labor",
                ]],
                use_container_width=True,
                hide_index=True,
            )

    st.markdown("---")

    # =========================================================================
    # ACTIVITY DRIVER OVERVIEW — skipped when locked
    # The driver breakdown is computed from raw activity inputs. For locked
    # periods we skip it entirely (the committed cost center pools below
    # already show the locked allocation). Users can Unlock and Recommit to
    # recompute and view weekly driver breakdowns.
    # =========================================================================
    st.markdown(f'<h3 style="color:{SECTION_HEADER_COLOR};">Activity Driver Overview</h3>', unsafe_allow_html=True)
    if is_locked:
        st.info(
            "Activity driver detail is not computed for locked periods. "
            "Unlock and recommit to recompute and view weekly driver breakdowns."
        )
    elif not driver_overview:
        st.info("No COGS activity drivers found for this period.")
    else:
        _render_coverage_warning(activity.get("receiving", pd.DataFrame()), "Receipts", period)
        _render_coverage_warning(activity.get("shipments", pd.DataFrame()), "Shipments", period)

        for bucket, driver_df in driver_overview.items():
            label    = ACTIVITY_LABEL_MAP.get(bucket, "Activity")
            has_week = "week_start" in driver_df.columns and driver_df["week_start"].notna().any()
            st.markdown(f"#### {bucket} driver")
            if driver_df.empty:
                st.info(f"No {label.lower()} data available for {bucket}.")
                continue

            if has_week:
                show = driver_df[["ISO Week", "Week", "Program", "Units", "weight",
                                   "Direct Cost", "Temp Cost", "Total Allocated"]].copy()
                show.rename(columns={"Units": label, "weight": "Weight"}, inplace=True)
                show[label]    = show[label].map("{:,.0f}".format)
                show["Weight"] = show["Weight"].map(_pct)
                for mc in ["Direct Cost", "Temp Cost", "Total Allocated"]:
                    show[mc] = show[mc].map(_dollar)
                st.dataframe(show, use_container_width=True, hide_index=True)
            else:
                show = driver_df[["Program", "Units", "weight",
                                   "Direct Cost", "Temp Cost", "Total Allocated"]].copy()
                show.rename(columns={"Units": label, "weight": "Weight"}, inplace=True)

                total_allocated = driver_df["Total Allocated"].sum()
                total_direct    = driver_df["Direct Cost"].sum()
                total_temp      = driver_df["Temp Cost"].sum()

                show[label]    = show[label].map("{:,.0f}".format)
                show["Weight"] = show["Weight"].map(_pct)
                for mc in ["Direct Cost", "Temp Cost", "Total Allocated"]:
                    show[mc] = show[mc].map(_dollar)

                st.dataframe(show, use_container_width=True, hide_index=True)
                st.caption(
                    f"Total Allocated: \\${total_allocated:,.2f}  \u00b7  "
                    f"Direct: \\${total_direct:,.2f}  \u00b7  "
                    f"Temp: \\${total_temp:,.2f}"
                )

    st.markdown("---")

    # =========================================================================
    # APPROVED EMPLOYEE OVERVIEW
    # =========================================================================
    st.markdown(f'<h3 style="color:{SECTION_HEADER_COLOR};">Approved Employee Overview</h3>', unsafe_allow_html=True)
    if approved_summary.empty:
        st.info("No reviewed employees yet for this filter and period.")
    else:
        display_summary = approved_summary.copy()
        display_summary.rename(columns={"Program": "Cost Center"}, inplace=True)
        display_summary["Approved Labor"] = display_summary["Approved Labor"].map(_dollar)
        st.dataframe(display_summary, use_container_width=True, hide_index=True)

        with st.expander("Approved employee detail by cost center", expanded=False):
            detail = approved_detail.copy()
            detail.rename(columns={
                "effective_bucket":  "Cost Center",
                "employee_name":     "Employee",
                "role_detail":       "Role / Detail",
                "source_assignment": "Original Assignment",
                "total_labor_cost":  "Approved Labor",
                "labor_source":      "Source",
            }, inplace=True)
        program_options = sorted(detail["Cost Center"].dropna().unique().tolist())
        selected_program = st.selectbox(
            "Cost Center",
            options=["-- Select Cost Center --"] + program_options,
            index=0,
            key=f"approved_employee_program_filter_{period}_{cost_type_filter}",
        )
        if selected_program == "-- Select Cost Center --":
            st.info("Select a cost center to view approved employee detail.")
        else:
            detail = detail[detail["Cost Center"] == selected_program].copy()
            detail = detail.sort_values(["Cost Center", "Employee"])
            detail["Approved Labor"] = pd.to_numeric(
                detail["Approved Labor"], errors="coerce"
            ).map(_dollar)
            st.dataframe(detail, use_container_width=True, hide_index=True)

    st.markdown("---")

    # =========================================================================
    # LOCKED / COMMITTED VIEW
    # =========================================================================
    if is_locked:
        locked_by = existing["committed_by"].iloc[0] if "committed_by" in existing.columns else "unknown"
        locked_at = existing["committed_at"].iloc[0] if "committed_at" in existing.columns else ""
        st.success(f"Allocation committed by **{locked_by}** at {locked_at}")

        # Post-commit auto-apply summary, if present from this session's commit
        result = st.session_state.get(f'auto_apply_result_{period}')
        if result and result['rows_applied'] > 0:
            st.info(
                f"Auto-applied {_dollar(result['total_amount'])} of prior fulfillment WIP "
                f"across {result['programs_applied']} programs "
                f"({result['rows_applied']} line(s))"
            )
            with st.expander("Auto-applied WIP detail", expanded=False):
                detail = result['detail'].copy()
                detail['accrued_cost']   = detail['accrued_cost'].map(_dollar)
                detail['activity_value'] = detail['activity_value'].map("{:,.2f}".format)
                detail = detail.rename(columns={
                    'origin_period':   'Origin Period',
                    'program':         'Program',
                    'cost_center':     'Cost Center',
                    'labor_type':      'Labor Type',
                    'activity_driver': 'Driver',
                    'activity_value':  'Driver Value',
                    'accrued_cost':    'Applied Cost',
                })
                st.dataframe(
                    detail[[
                        'Origin Period', 'Program', 'Cost Center', 'Labor Type',
                        'Driver', 'Driver Value', 'Applied Cost',
                    ]],
                    use_container_width=True,
                    hide_index=True,
                )

        can_unlock = auth.has_role("admin", "controller")
        if st.button(
            "Unlock and Recommit",
            key=f"unlock_{period}",
            type="secondary",
            disabled=not can_unlock,
        ):
            unlock_allocation(period)
            get_existing_allocation.clear()
            wla.is_period_committed.clear()
            st.rerun()

        st.markdown("### Committed Cost Center Pools")
        for ltype, ltype_label in [
            ("direct_cogs", "Direct COGS"),
            ("temp",        "Temp COGS"),
            ("direct_sga",  "Direct SG&A"),
        ]:
            sub = existing[existing["labor_type"] == ltype]
            if sub.empty:
                continue
            with st.expander(f"{ltype_label} — {_dollar(sub['allocated_cost'].sum())}", expanded=False):
                display = sub[["bucket", "program", "labor_pool", "activity_value", "weight", "allocated_cost"]].copy()
                display.columns = ["Bucket", "Program", "Labor Pool", "Activity", "Weight", "Allocated Cost"]
                display["Weight"]         = display["Weight"].map(_pct)
                display["Labor Pool"]     = display["Labor Pool"].map(_dollar)
                display["Allocated Cost"] = display["Allocated Cost"].map(_dollar)
                display["Activity"]       = display["Activity"].map("{:,.2f}".format)
                st.dataframe(display, use_container_width=True, hide_index=True)

        # =====================================================================
        # APPLIED LABOR
        # =====================================================================
        st.markdown("---")
        st.markdown(f'<h3 style="color:{SECTION_HEADER_COLOR};">Applied Labor</h3>', unsafe_allow_html=True)
        st.caption("Persisted end state — labor applied to revenue programs this period.")

        applied_df = get_labor_applied(period)
        if applied_df.empty:
            st.info("No applied labor for this period. Commit allocation to generate.")
        else:
            # Labor to P&L — sources that hit current period income statement.
            # period_allocation: current labor against programs with current revenue
            # fifo: current invoices consuming prior-period production layers
            # fulfillment_wip_applied: prior WIP the reviewer applied to current period
            # Labor to P&L — sources that hit current period income statement.
            labor_to_pnl = applied_df[
                applied_df["source"].isin(["period_allocation", "fifo", "fulfillment_wip_applied"])
            ]["applied_cost"].sum()

            # New WIP Generated — current-period labor that did NOT hit P&L,
            # added to balance sheet WIP for future application.
            #   current_fulfillment_wip: labor for programs with no current revenue
            #   work_order_assigned: ArrivedCo/Recess labor not yet billed
            #   production_wip: Demo/OGP/Overwrap layer labor not yet FIFO-consumed
            new_wip_generated = applied_df[
                applied_df["source"].isin([
                    "current_fulfillment_wip", 
                    "work_order_assigned",
                    "production_wip",
                ])
            ]["applied_cost"].sum()

            # Total committed = everything written to stg_labor_applied this period.
            # Should reconcile to SUM(allocated_cost) from stg_labor_incurred for
            # this period (plus the ArrivedCo/Recess work_order_assigned amount
            # which originates in stg_wip_program_labor_accrual, not stg_labor_incurred).
            total_committed = labor_to_pnl + new_wip_generated

            a1, a2, a3, a4, a5 = st.columns(5)
            a1.metric("Labor to P&L",          _dollar(labor_to_pnl))
            a2.metric("New WIP Generated",     _dollar(new_wip_generated))
            a3.metric("Total Labor Committed", _dollar(total_committed))
            a4.metric("Programs", applied_df["program"].nunique())
            a5.metric("Sources", applied_df["source"].nunique())

            # Check for pending fulfillment WIP applications.
            # Work order WIP checks removed in phase 2 AC/R refactor (2026-05-23) —
            # AC/R now flows through standard FIFO with no separate application step.
            pending_warnings = []
            
            fulfillment_wip_pending = get_prior_fulfillment_wip_applicable(period)
            if not fulfillment_wip_pending.empty:
                total_pending_fulfillment = fulfillment_wip_pending["accrued_cost"].sum()
                pending_warnings.append(
                    f"${total_pending_fulfillment:,.2f} of prior fulfillment WIP is available to apply "
                    f"(see Fulfillment WIP tab)"
                )
            
            if pending_warnings:
                st.warning(
                    "**Pending WIP Applications:**\n\n" + 
                    "\n".join(f"• {warning}" for warning in pending_warnings) +
                    f"\n\nReview and commit WIP applications to include in {period} P&L impact."
                )
                st.markdown("")

            chart_df = applied_df.groupby(["program", "labor_type"], as_index=False)["applied_cost"].sum()
            top_programs = (
                chart_df.groupby("program")["applied_cost"]
                .sum().nlargest(15).index.tolist()
            )
            chart_df_top = chart_df[chart_df["program"].isin(top_programs)].copy()
            program_order = (
                chart_df_top.groupby("program")["applied_cost"]
                .sum().sort_values(ascending=True).index.tolist()
            )
            _LTYPE_COLORS = {
                "direct_cogs": "#1f77b4",
                "temp":        "#ff7f0e",
                "direct_sga":  "#5470c6",
            }
            chart = (
                alt.Chart(chart_df_top).mark_bar()
                .encode(
                    x=alt.X("applied_cost:Q", title="Applied Labor ($)", axis=alt.Axis(format="$,.0f")),
                    y=alt.Y("program:N", sort=program_order, title=None),
                    color=alt.Color(
                        "labor_type:N", title="Type",
                        scale=alt.Scale(
                            domain=list(_LTYPE_COLORS.keys()),
                            range=list(_LTYPE_COLORS.values()),
                        ),
                    ),
                    tooltip=[
                        alt.Tooltip("program:N",      title="Program"),
                        alt.Tooltip("labor_type:N",   title="Type"),
                        alt.Tooltip("applied_cost:Q", title="Applied $", format="$,.2f"),
                    ],
                )
                .properties(height=400)
            )
            st.altair_chart(chart, use_container_width=True)

            display = applied_df.copy()
            display["applied_cost"]       = display["applied_cost"].map(_dollar)
            display["wip_cost_forward"]   = display["wip_cost_forward"].map(lambda x: _dollar(x) if pd.notna(x) else "")
            display["wip_cost_remaining"] = display["wip_cost_remaining"].map(lambda x: _dollar(x) if pd.notna(x) else "")
            display["weight"]             = display["weight"].map(lambda x: _pct(x) if pd.notna(x) else "")
            display = display.rename(columns={
                "source":             "Source",
                "bucket":             "Cost Center",
                "program":            "Program",
                "labor_type":         "Labor Type",
                "activity_driver":    "Driver",
                "activity_value":     "Driver Value",
                "weight":             "Weight",
                "wip_cost_forward":   "WIP Forward",
                "wip_cost_remaining": "WIP Remaining",
                "applied_cost":       "Applied Cost",
                "locked_by":          "Locked By",
            })
            st.dataframe(
                display[[
                    "Source", "Cost Center", "Program", "Labor Type",
                    "Driver", "Weight", "WIP Forward", "WIP Remaining",
                    "Applied Cost", "Locked By",
                ]],
                use_container_width=True,
                hide_index=True,
            )

        return
      
    # =========================================================================
    # NOTHING TO ALLOCATE GUARD
    # =========================================================================
    if employee_alloc_df.empty:
        st.info("No approved labor to allocate for this period.")
        return

    # =========================================================================
    # ALLOCATION ASSIGNMENTS (RECONCILIATION)
    # =========================================================================
    st.markdown("### Allocation Assignments")
    if reconciliation_df.empty:
        st.info("No approved pools found to reconcile.")
    else:
        show_recon = reconciliation_df.copy()
        show_recon.rename(columns={
            "bucket":          "Cost Center",
            "labor_type":      "Labor Type",
            "approved_pool":   "Approved Pool",
            "driver_total":    "Driver Total",
            "allocated_total": "Allocated Total",
            "variance":        "Variance",
            "program_count":   "Programs Hit",
            "employee_count":  "Employees Assigned",
            "driver_type":     "Driver Type",
        }, inplace=True)
        for col in ["Approved Pool", "Allocated Total", "Variance"]:
            show_recon[col] = show_recon[col].map(_dollar)
        show_recon["Driver Total"] = show_recon["Driver Total"].map("{:,.2f}".format)
        show_recon.loc[show_recon["Labor Type"] == "direct_sga", "Driver Total"] = "Revenue Weighted"
        st.dataframe(
            show_recon[[
                "Cost Center", "Labor Type", "Driver Type", "Approved Pool",
                "Driver Total", "Allocated Total", "Variance",
                "Programs Hit", "Employees Assigned",
            ]],
            use_container_width=True,
            hide_index=True,
        )

    st.markdown("---")

    # =========================================================================
    # COGS ALLOCATION
    # =========================================================================
    st.markdown(f'<h3 style="color:{SECTION_HEADER_COLOR};">COGS Allocation</h3>', unsafe_allow_html=True)

    _REVENUE_WEIGHTED_BUCKETS = {"E-Commerce", "Facilities", "Purchasing"}
    _LTYPE_LABEL = {"direct_cogs": "Direct Hire", "temp": "Temp"}
    _LTYPE_COLOR = {"direct_cogs": "#1f77b4",     "temp": "#ff7f0e"}
    _LTYPE_BADGE = {
        "direct_cogs": '<span style="background:#1f77b4;color:white;padding:2px 8px;border-radius:4px;font-size:0.8em;">Direct Hire</span>',
        "temp":        '<span style="background:#ff7f0e;color:white;padding:2px 8px;border-radius:4px;font-size:0.8em;">Temp</span>',
    }

    cogs_emp = employee_alloc_df[employee_alloc_df["cost_type"] == "COGS"].copy() if not employee_alloc_df.empty else pd.DataFrame()

    if not cogs_emp.empty:
        bucket_colors = {"Demo": "#1f77b4", "OGP": "#2ca02c", "Overwrap": "#ff7f0e"}
        default_color = "#9467bd"

        for bucket in sorted(cogs_emp["source_bucket"].unique()):
            bucket_sub = cogs_emp[cogs_emp["source_bucket"] == bucket]

            meaningful = bucket_sub[
                (bucket_sub["allocated_cost"] != 0) &
                (bucket_sub["target_program"] != "NO ACTIVITY DATA")
            ]
            if meaningful.empty:
                continue

            bucket_total = bucket_sub["allocated_cost"].sum()
            direct_total = float(bucket_sub[bucket_sub["labor_source"] == "Direct COGS"]["allocated_cost"].sum())
            temp_total   = float(bucket_sub[bucket_sub["labor_source"] == "Temp"]["allocated_cost"].sum())

            with st.expander(bucket, expanded=False):
                for labor_src_label, badge_color in [
                    ("Direct COGS", "#1f77b4"),
                    ("Temp",        "#ff7f0e"),
                ]:
                    sub = bucket_sub[bucket_sub["labor_source"] == labor_src_label].copy()
                    if sub.empty or sub["allocated_cost"].sum() == 0:
                        continue

                    badge = f'<span style="background:{badge_color};color:white;padding:2px 8px;border-radius:4px;font-size:0.8em;">{labor_src_label}</span>'
                    st.markdown(badge, unsafe_allow_html=True)

                    # Aggregate to program level for display
                    display_df = sub.groupby("target_program", as_index=False).agg(
                        activity_value=("activity_value", "sum"),
                        allocated_cost=("allocated_cost", "sum"),
                        weight=("weight", "sum"),
                    )
                    display_df["program"] = display_df["target_program"]
                    display_df["labor_pool"] = float(sub["allocated_cost"].sum())
                    display_df["total_activity"] = display_df["activity_value"].sum()

                    activity_label = ACTIVITY_LABEL_MAP.get(bucket, "Units")
                    render_allocation_section(
                        title="",
                        color=bucket_colors.get(bucket, default_color),
                        alloc_df=display_df,
                        activity_label=activity_label,
                    )
                    st.markdown("")

    # =========================================================================
    # SG&A ALLOCATION
    # =========================================================================
    sga_emp = employee_alloc_df[employee_alloc_df["cost_type"] == "SGA"].copy() if not employee_alloc_df.empty else pd.DataFrame()

    st.markdown("---")
    st.markdown(f'<h3 style="color:{SECTION_HEADER_COLOR};">SG&A Allocation</h3>', unsafe_allow_html=True)

    if sga_emp.empty:
        st.info("No SG&A labor allocated for this period.")
    else:
        sga_total = sga_emp["allocated_cost"].sum()
        st.caption(
            f"SG&A total: ${sga_total:,.2f}  ·  "
            f"{sga_emp['source_bucket'].nunique()} cost center(s)  ·  "
            f"Altria capped at 10% where revenue-weighted"
        )

        # Group by source_bucket (cost center) then by target_program
        for cc in sorted(sga_emp["source_bucket"].unique()):
            cc_sub = sga_emp[sga_emp["source_bucket"] == cc]
            cc_total = cc_sub["allocated_cost"].sum()
            if cc_total == 0:
                continue

            with st.expander(f"{cc} — {_dollar(cc_total)}", expanded=False):
                display_df = cc_sub.groupby("target_program", as_index=False).agg(
                    activity_value=("activity_value", "sum"),
                    allocated_cost=("allocated_cost", "sum"),
                    weight=("weight", "sum"),
                )
                display_df["program"] = display_df["target_program"]
                display_df["labor_pool"] = float(cc_total)
                display_df["total_activity"] = display_df["activity_value"].sum()
                render_allocation_section(
                    title="",
                    color="#5470c6",
                    alloc_df=display_df,
                    activity_label="Driver",
                )


    # =========================================================================
    # SUMMARY
    # =========================================================================
    st.markdown("---")
    st.markdown(f'<h3 style="color:{SECTION_HEADER_COLOR};">Summary</h3>', unsafe_allow_html=True)
    all_rows = []
    if not employee_alloc_df.empty:
        summary_df = employee_alloc_df.copy()
        summary_df["bucket"] = summary_df["source_bucket"]
        summary_df["labor_type"] = summary_df["labor_source"].map({
            "Direct COGS": "direct_cogs", "Direct SG&A": "direct_sga", "Temp": "temp",
        })
        totals = summary_df.groupby(["labor_type", "bucket"], as_index=False)["allocated_cost"].sum()
        totals.columns = ["Labor Type", "Bucket", "Total Allocated"]

        _LABOR_TYPE_LABELS = {
            "direct_cogs": "Direct COGS",
            "direct_sga":  "Direct SG&A",
            "temp":        "Temp COGS",
        }
        totals["Labor Type"]      = totals["Labor Type"].map(lambda x: _LABOR_TYPE_LABELS.get(x, x))
        totals["Total Allocated"] = totals["Total Allocated"].map(_dollar)
        grand = employee_alloc_df["allocated_cost"].sum()
        st.dataframe(totals, use_container_width=True, hide_index=True)
        st.metric("Grand Total Allocated", f"${grand:,.2f}")

   # =========================================================================
    # COMMIT
    # =========================================================================
    st.markdown("---")

    col_recompute, _ = st.columns([2, 6])
    with col_recompute:
        if st.button("Recompute Allocation", key="recompute_alloc_btn", use_container_width=True):
            _clear_employee_alloc_caches()
            st.rerun()
    st.caption(
        "Recompute after adding containers, returns, or other driver data so "
        "the summary reflects the latest inputs."
    )
    st.markdown("")

    # Surface any lines that had no driver data this period
    if alloc_warnings:
        warnings_total = sum(w["cost"] for w in alloc_warnings)
        st.warning(
            f"{len(alloc_warnings)} allocation line(s) totaling {_dollar(warnings_total)} "
            "have no driver data for this period. The cost will not be allocated until the "
            "underlying data is added, or the line is reassigned. Review before committing."
        )
        warnings_df = pd.DataFrame(alloc_warnings)
        warnings_df["cost"] = warnings_df["cost"].map(_dollar)
        warnings_df = warnings_df.rename(columns={
            "employee_name": "Employee",
            "role_name":     "Role",
            "cost_center":   "Cost Center",
            "driver_key":    "Driver",
            "cost":          "Line Cost",
            "reason":        "Reason",
        })
        with st.expander(f"Lines with no driver data ({len(alloc_warnings)})", expanded=False):
            st.dataframe(
                warnings_df[["Employee", "Role", "Cost Center", "Driver", "Line Cost", "Reason"]],
                use_container_width=True,
                hide_index=True,
            )
        st.markdown("")

    can_commit = auth.has_role("admin", "controller")

    if not can_commit:
        st.info(
            "Committing allocation requires admin or controller role. "
            f"Your current role is `{auth.current_user()['role']}`."
        )

    if st.button(
                "Commit Allocation",
                key=f"commit_{period}",
                type="primary",
                use_container_width=True,
                disabled=not can_commit,
            ):
                if not reviewer_name:
                    st.warning("Enter your name in the Reviewer's Name field above.")
                else:
                    # Build commit rows from the fanned-out allocation
                    commit_rows = []
                    if not employee_alloc_df.empty:
                        for (bucket, program, lsrc), group in employee_alloc_df.groupby(
                            ["source_bucket", "target_program", "labor_source"]
                        ):
                            commit_rows.append({
                                "labor_type":     {"Direct COGS": "direct_cogs", "Direct SG&A": "direct_sga", "Temp": "temp"}.get(lsrc, "direct_cogs"),
                                "bucket":         bucket,
                                "program":        program,
                                "labor_pool":     float(group["allocated_cost"].sum()),
                                "activity_value": float(group["activity_value"].sum()),
                                "total_activity": float(group["activity_value"].sum()),
                                "weight":         float(group["weight"].sum()),
                                "allocated_cost": float(group["allocated_cost"].sum()),
                            })
                    commit_allocation(commit_rows, period, reviewer_name)

                    write_labor_incurred(period, reviewer_name, employee_alloc_df)
                    write_labor_incurred_employee(period, reviewer_name, employee_alloc_df)

                    write_production_layers(period, reviewer_name)
                    run_fifo_matching(period, reviewer_name)
                    write_program_labor_accrual(period, reviewer_name)

                    write_labor_applied(period, reviewer_name)

                    # Auto-apply prior-period fulfillment WIP where revenue now exists
                    auto_apply_result = auto_apply_prior_fulfillment_wip(period, reviewer_name)
                    if auto_apply_result['rows_applied'] > 0:
                        st.session_state[f'auto_apply_result_{period}'] = auto_apply_result

                    run_close_checks(period, reviewer_name)
                    get_prior_fulfillment_wip_applicable.clear()
                    get_labor_applied.clear()
                    st.rerun()

# ---------------------------------------------------------------------------
# Shared UI components
# ---------------------------------------------------------------------------

def render_receiving_returns_tab(period: str, reviewer_name: str):
    st.subheader("Receiving Returns Entry")
    st.caption(
        "Record return counts by customer for this period. "
        "Returns specialist labor is split: return minutes (10 min/return out of 175 hr/period) "
        "allocated by return count, balance allocated by receipt count."
    )

    existing         = get_receiving_returns(period)
    all_customers_df = get_ecomm_customers()

    if all_customers_df.empty:
        st.warning("No active customers found in dim_customer.")
        return

    period_committed = wla.is_period_committed(period)
    if period_committed:
        st.warning(
            f"Period {period} allocation is **committed**. Receiving Returns "
            "entries are read-only — adding or modifying returns would "
            "silently invalidate the locked allocation. Unlock from the "
            "Allocation tab before making changes."
        )

    customer_options = sorted(all_customers_df["customer_name"].dropna().tolist(), key=str.upper)

    if not existing.empty:
        st.markdown("#### Recorded Returns for Period")
        display = existing.copy()

        total_return_minutes   = (display["return_count"] * display["minutes_per_return"]).sum()
        total_capacity_minutes = (display["hours_per_period"].iloc[0] * 60)
        returns_fraction       = min(total_return_minutes / total_capacity_minutes, 1.0) if total_capacity_minutes > 0 else 0.0

        display["Attributed Minutes"] = display["return_count"] * display["minutes_per_return"]
        display["Pct of Capacity"]    = (display["Attributed Minutes"] / total_capacity_minutes * 100).map("{:.2f}%".format)
        display["Attributed Minutes"] = display["Attributed Minutes"].map("{:,.1f}".format)

        st.dataframe(
            display.rename(columns={
                "customer_name":      "Customer",
                "return_count":       "Return Count",
                "minutes_per_return": "Min / Return",
                "hours_per_period":   "Hours / Period",
                "set_by":             "Set By",
                "set_at":             "Set At",
            })[[
                "Customer", "Return Count", "Min / Return", "Hours / Period",
                "Attributed Minutes", "Pct of Capacity", "Set By", "Set At",
            ]],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            f"Total return minutes: {total_return_minutes:,.1f}  |  "
            f"Capacity: {total_capacity_minutes:,.0f} min  |  "
            f"Returns fraction: {returns_fraction:.2%}  |  "
            f"Receipts fraction: {1 - returns_fraction:.2%}"
        )
    else:
        st.info("No return entries recorded for this period yet.")

    if period_committed:
        return

    st.markdown("---")
    st.markdown("#### Add / Update Entry")

    col_cust, col_count, col_mpr, col_hpp = st.columns([3, 2, 2, 2])
    with col_cust:
        selected_customer = st.selectbox(
            "Customer", options=customer_options, key=f"ret_customer_{period}",
        )
    with col_count:
        return_count = st.number_input(
            "Return Count", min_value=0, step=1, value=0, key=f"ret_count_{period}",
        )
    with col_mpr:
        minutes_per_return = st.number_input(
            "Min / Return", min_value=1.0, step=1.0, value=10.0, key=f"ret_mpr_{period}",
        )
    with col_hpp:
        hours_per_period = st.number_input(
            "Hours / Period", min_value=1.0, step=1.0, value=175.0, key=f"ret_hpp_{period}",
        )

    col_save, col_del, _ = st.columns([2, 2, 4])
    with col_save:
        if st.button("Save Entry", key=f"ret_save_{period}", type="primary", use_container_width=True):
            if not reviewer_name:
                st.warning("Enter your name above before saving.")
            elif return_count <= 0:
                st.warning("Return count must be greater than zero.")
            else:
                upsert_receiving_return(
                    period, selected_customer, return_count,
                    minutes_per_return, hours_per_period, reviewer_name,
                )
                get_receiving_returns.clear()
                st.rerun()
    with col_del:
        if st.button("Remove Entry", key=f"ret_del_{period}", type="secondary", use_container_width=True):
            if not reviewer_name:
                st.warning("Enter your name above before removing.")
            else:
                delete_receiving_return(period, selected_customer)
                get_receiving_returns.clear()
                st.rerun()


# ---------------------------------------------------------------------------
# Production WIP tab renderers
# ---------------------------------------------------------------------------

def _units(v):
    if pd.isna(v):
        return ""
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def render_production_wip_tab(period: str, reviewer_name: str):
    wip_tab1, wip_tab2, wip_tab3, wip_tab4 = st.tabs([
        "Period Summary",
        "Production Layers",
        "FIFO Applied",
        "Fulfillment WIP",
    ])

    with wip_tab1:
        _render_wip_period_summary(period)
        st.markdown("---")
        _render_outstanding_wip_combined()

    with wip_tab2:
        _render_production_layers(period)

    with wip_tab3:
        _render_fifo_applied(period)

    with wip_tab4:
        _render_fulfillment_wip(period)


def _render_fulfillment_wip(period: str):
    st.markdown(
        f'<h3 style="color:{SECTION_HEADER_COLOR};">Fulfillment WIP</h3>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Period allocation labor applied to programs with no revenue recognized this period. "
        "These amounts are accrued on the balance sheet pending future invoicing."
    )

    df = get_fulfillment_wip(period)

    if df.empty:
        st.success("No fulfillment WIP for this period — all allocated programs have matching revenue.")
        return

    total_wip = df["applied_cost"].sum()
    f1, f2, f3 = st.columns(3)
    f1.metric("Total Fulfillment WIP", _dollar(total_wip))
    f2.metric("Programs", df["program"].nunique())
    f3.metric("Cost Centers", df["cost_center"].nunique())

    # Chart
    chart_df = df.groupby(["program", "labor_type"], as_index=False)["applied_cost"].sum()
    if not chart_df.empty and chart_df["applied_cost"].sum() > 0:
        program_order = (
            chart_df.groupby("program")["applied_cost"]
            .sum().sort_values(ascending=True).index.tolist()
        )
        _LTYPE_COLORS = {
            "direct_cogs": "#1f77b4",
            "temp":        "#ff7f0e",
            "direct_sga":  "#5470c6",
        }
        chart = (
            alt.Chart(chart_df).mark_bar()
            .encode(
                x=alt.X("applied_cost:Q", title="Accrued Labor ($)", axis=alt.Axis(format="$,.0f")),
                y=alt.Y("program:N", sort=program_order, title=None),
                color=alt.Color(
                    "labor_type:N", title="Type",
                    scale=alt.Scale(
                        domain=list(_LTYPE_COLORS.keys()),
                        range=list(_LTYPE_COLORS.values()),
                    ),
                ),
                tooltip=[
                    alt.Tooltip("program:N",      title="Program"),
                    alt.Tooltip("labor_type:N",   title="Type"),
                    alt.Tooltip("applied_cost:Q", title="Accrued $", format="$,.2f"),
                ],
            )
            .properties(height=max(150, len(df["program"].unique()) * 28))
        )
        st.altair_chart(chart, use_container_width=True)

    # Detail table
    display = df.copy()
    display["applied_cost"]   = display["applied_cost"].map(_dollar)
    display["activity_value"] = display["activity_value"].map("{:,.2f}".format)
    display = display.rename(columns={
        "program":         "Program",
        "cost_center":     "Cost Center",
        "labor_type":      "Labor Type",
        "activity_driver": "Driver",
        "activity_value":  "Driver Value",
        "applied_cost":    "Accrued Cost",
        "accrual_period":  "Period",
    })
    st.dataframe(
        display[[
            "Program", "Cost Center", "Labor Type",
            "Driver", "Driver Value", "Accrued Cost",
        ]],
        use_container_width=True,
        hide_index=True,
    )
    st.caption(
        "These programs had activity (receipts, shipments, or inventory) in the period "
        "but no corresponding revenue invoice. Follow up to confirm billing is pending."
    )


def render_close_check_banner(period: str):
    """
    Renders a status banner showing the latest close check results for the
    period. Pulls from stg_labor_close_check. Shows nothing if no checks
    have been recorded yet.

    Streamlit does not allow nested expanders, so per-check drill-down
    detail is rendered as labeled tables stacked vertically inside the
    Reconciliation Detail expander.
    """
    df = pd.read_sql(text("""
        SELECT check_name, severity, metric_value, threshold, details, committed_at
        FROM stg_labor_close_check
        WHERE accrual_period = :period
        ORDER BY committed_at DESC
    """), engine, params={"period": period})

    if df.empty:
        return

    latest_ts = df["committed_at"].max()
    df = df[df["committed_at"] == latest_ts].reset_index(drop=True)

    fails  = df[df["severity"] == "fail"]
    warns  = df[df["severity"] == "warn"]
    passes = df[df["severity"] == "pass"]
    total  = len(df)

    if not fails.empty:
        st.error(
            f"Reconciliation: {len(fails)} failed, {len(warns)} warning, "
            f"{len(passes)} passed. Review failures before close."
        )
    elif not warns.empty:
        st.warning(
            f"Reconciliation: {len(warns)} warning, {len(passes)} passed. "
            f"Review warnings prior to close."
        )
    else:
        st.success(f"Reconciliation: {total} of {total} checks passed.")

    with st.expander("Reconciliation detail", expanded=not fails.empty):
        # Summary table
        summary = df[["check_name", "severity", "metric_value", "threshold"]].copy()
        summary.columns = ["Check", "Status", "Value", "Tolerance"]
        summary["Check"] = summary["Check"].str.replace("_", " ").str.title()
        summary["Status"] = summary["Status"].str.upper()
        st.dataframe(summary, use_container_width=True, hide_index=True)

        # Per-check drill-down as labeled sections (no nested expanders)
        checks_with_details = df[df["details"].apply(lambda d: bool(d))]
        if not checks_with_details.empty:
            st.markdown("---")
            st.markdown("**Check detail**")
            for _, row in checks_with_details.iterrows():
                check_label = row["check_name"].replace("_", " ").title()
                st.markdown(f"**{check_label}** ({len(row['details'])} item(s))")
                detail_df = pd.DataFrame(row["details"])
                st.dataframe(detail_df, use_container_width=True, hide_index=True)
                st.markdown("")


def _render_wip_period_summary(period: str):
    st.markdown(
        f'<h3 style="color:{SECTION_HEADER_COLOR};">Period Summary</h3>',
        unsafe_allow_html=True,
    )
    render_close_check_banner(period)

    summary            = get_wip_summary(period)
    fulfillment_wip_df = get_fulfillment_wip(period)
    applicable_wip_df  = get_prior_fulfillment_wip_applicable(period)
    work_order_wip_df  = get_accrual_balance(period)

    # =========================================================================
    # PRIOR PERIOD FULFILLMENT WIP WARNING + APPLY
    # =========================================================================
    if not applicable_wip_df.empty:
        total_applicable = applicable_wip_df["accrued_cost"].sum()
        st.warning(
            f"{applicable_wip_df['program'].nunique()} program(s) have prior period fulfillment WIP "
            f"totaling {_dollar(total_applicable)} that now have revenue in {period}. "
            "Review and apply below."
        )

        with st.expander("Apply Prior Period Fulfillment WIP", expanded=True):
            st.caption(
                "These programs had labor allocated in a prior period with no corresponding invoice. "
                "They now have revenue in the current period. Select rows to expense into this period."
            )

            # Build display with checkboxes via session state
            check_key = f"fulfillment_wip_checks_{period}"
            if check_key not in st.session_state:
                st.session_state[check_key] = {
                    i: True for i in range(len(applicable_wip_df))
                }

            # Header row
            h_chk, h_period, h_program, h_cc, h_ltype, h_driver, h_cost = st.columns(
                [0.5, 1.5, 2.5, 1.5, 1.5, 2, 1.5]
            )
            h_chk.markdown("**Apply**")
            h_period.markdown("**Origin Period**")
            h_program.markdown("**Program**")
            h_cc.markdown("**Cost Center**")
            h_ltype.markdown("**Labor Type**")
            h_driver.markdown("**Driver**")
            h_cost.markdown("**Accrued Cost**")
            st.markdown("---")

            for i, row in applicable_wip_df.iterrows():
                c_chk, c_period, c_program, c_cc, c_ltype, c_driver, c_cost = st.columns(
                    [0.5, 1.5, 2.5, 1.5, 1.5, 2, 1.5]
                )
                with c_chk:
                    checked = st.checkbox(
                        label="apply",
                        value=st.session_state[check_key].get(i, True),
                        key=f"fwip_chk_{period}_{i}",
                        label_visibility="collapsed",
                    )
                    st.session_state[check_key][i] = checked
                c_period.markdown(row["origin_period"])
                c_program.markdown(row["program"])
                c_cc.markdown(row["cost_center"])
                c_ltype.markdown(row["labor_type"])
                c_driver.markdown(row["activity_driver"])
                c_cost.markdown(_dollar(row["accrued_cost"]))

            st.markdown("")
            selected_indices = [
                i for i, checked in st.session_state[check_key].items() if checked
            ]
            selected_total = applicable_wip_df.loc[
                applicable_wip_df.index.isin(selected_indices), "accrued_cost"
            ].sum()

            col_apply, col_info = st.columns([2, 5])
            with col_apply:
                reviewer_key = f"wip_reviewer"
                reviewer = st.session_state.get(reviewer_key, "")
                if st.button(
                    f"Apply Selected ({len(selected_indices)} rows)",
                    key=f"btn_apply_fulfillment_wip_{period}",
                    type="primary",
                    use_container_width=True,
                    disabled=len(selected_indices) == 0,
                ):
                    if not reviewer:
                        st.warning("Enter your name in the Reviewer's Name field above before applying.")
                    else:
                        rows_to_write = applicable_wip_df.loc[
                            applicable_wip_df.index.isin(selected_indices)
                        ].to_dict("records")
                        write_fulfillment_wip_applied(period, rows_to_write, reviewer)
                        get_prior_fulfillment_wip_applicable.clear()
                        get_labor_applied.clear()
                        st.success(f"Applied {_dollar(selected_total)} of prior fulfillment WIP to {period}.")
                        st.rerun()
            with col_info:
                st.caption(
                    f"Selected total: {_dollar(selected_total)}  "
                    f"·  This will write to stg_labor_applied as source = fulfillment_wip_applied  "
                    f"·  MV will reflect on next refresh."
                )

        st.markdown("---")

    elif not summary.empty:
        st.success("No prior period fulfillment WIP applicable for this period.")

    # =========================================================================
    # METRICS
    # =========================================================================
    if summary.empty:
        st.info("No production layers found. Commit labor allocation to generate layers.")
        return

    total_pool            = summary["total_labor_pool"].sum()
    total_recognized      = summary["recognized_cost"].sum()
    total_production_wip  = summary["outstanding_wip"].sum()
    total_fulfillment_wip = float(fulfillment_wip_df["applied_cost"].sum()) if not fulfillment_wip_df.empty else 0.0
    total_wip_balance     = total_production_wip + total_fulfillment_wip
    total_produced        = summary["units_produced"].sum()
    total_remaining       = summary["units_remaining"].sum()

    # Top-line KPIs — Production WIP + Fulfillment WIP = Total WIP Balance.
    # Work Order WIP removed in phase 2 AC/R refactor (2026-05-23) —
    # AC/R labor now flows through the standard Production WIP path.
    k1, k2, k3 = st.columns(3)
    k1.metric("Production WIP",  _dollar(total_production_wip),
              help="Demo/OGP/Overwrap layer labor produced but not yet FIFO-consumed by sales (includes AC/R).")
    k2.metric("Fulfillment WIP", _dollar(total_fulfillment_wip),
              help="Period allocation labor for programs with no current revenue. Pending future invoice.")
    k3.metric("Total WIP Balance", _dollar(total_wip_balance))

    st.caption(
        "These two categories sum to total WIP on the balance sheet. "
        "This figure matches 'New WIP Generated' on the Allocation tab."
    )
    st.markdown("")

    k5, k6, k7 = st.columns(3)
    k5.metric("Total Labor Pool",  _dollar(total_pool),
              help="Total production-cost-center labor allocated this period (Demo + OGP + Overwrap).")
    k6.metric("Recognized COGS",   _dollar(total_recognized),
              help="Production labor consumed by sales invoices this period (FIFO matched).")
    k7.metric("Production Units Remaining", _units(total_remaining),
              help="Units in production layers not yet consumed by sales.")

    st.caption(
        f"Units Produced: {_units(total_produced)}  "
        f"·  Units Remaining: {_units(total_remaining)}"
    )

    st.markdown("")

    # =========================================================================
    # COMBINED DETAIL TABLE — all three WIP slices in one view
    # =========================================================================
    # Production WIP rows
    if not summary.empty:
        prod_display = summary.copy()
        prod_display.columns = [
            "Cost Center", "Program", "Units Produced", "Units Remaining",
            "Units Consumed", "Labor Pool", "Recognized Cost", "Outstanding WIP",
        ]
        prod_display["WIP Type"] = "Production"
    else:
        prod_display = pd.DataFrame(columns=[
            "Cost Center", "Program", "Units Produced", "Units Remaining",
            "Units Consumed", "Labor Pool", "Recognized Cost", "Outstanding WIP", "WIP Type",
        ])

    # Fulfillment WIP rows
    if not fulfillment_wip_df.empty:
        fulfillment_display = fulfillment_wip_df.groupby(
            ["program", "cost_center"], as_index=False
        )["applied_cost"].sum().rename(columns={
            "program":      "Program",
            "cost_center":  "Cost Center",
            "applied_cost": "Outstanding WIP",
        })
        fulfillment_display["Units Produced"]  = None
        fulfillment_display["Units Remaining"] = None
        fulfillment_display["Units Consumed"]  = None
        fulfillment_display["Labor Pool"]      = None
        fulfillment_display["Recognized Cost"] = 0.0
        fulfillment_display["WIP Type"]        = "Fulfillment"
    else:
        fulfillment_display = pd.DataFrame(columns=prod_display.columns)

    # Work Order WIP rows removed in phase 2 AC/R refactor (2026-05-23).
    wo_display = pd.DataFrame(columns=prod_display.columns)

    # Combine and format
    combined = pd.concat([prod_display, fulfillment_display, wo_display], ignore_index=True)

    if not combined.empty:
        for col in ["Labor Pool", "Recognized Cost", "Outstanding WIP"]:
            combined[col] = combined[col].map(_dollar)
        for col in ["Units Produced", "Units Remaining", "Units Consumed"]:
            combined[col] = combined[col].map(_units)

    st.dataframe(
        combined[[
            "WIP Type", "Cost Center", "Program",
            "Units Produced", "Units Remaining", "Units Consumed",
            "Labor Pool", "Recognized Cost", "Outstanding WIP",
        ]],
        use_container_width=True,
        hide_index=True,
    )


def _render_production_layers(period: str):
    st.markdown(
        f'<h3 style="color:{SECTION_HEADER_COLOR};">Production Layers</h3>',
        unsafe_allow_html=True,
    )
    st.caption(
        "One layer per ISO week per cost center per program. "
        "Written on labor commit. Units remaining decrements as sales are FIFO matched."
    )
    layers = get_production_layers(period)
    if layers.empty:
        st.info("No production layers for this period.")
        return

    for cost_center, cc_df in layers.groupby("cost_center"):
        with st.expander(f"{cost_center}", expanded=True):
            display = cc_df[[
                "iso_week", "customer_program", "units_produced",
                "units_consumed", "units_remaining", "pct_consumed",
                "labor_pool", "cost_per_unit",
            ]].copy()
            display.columns = [
                "ISO Week", "Program", "Units Produced",
                "Units Consumed", "Units Remaining", "% Consumed",
                "Labor Pool", "Cost / Unit",
            ]
            display["Units Produced"]  = display["Units Produced"].map(_units)
            display["Units Consumed"]  = display["Units Consumed"].map(_units)
            display["Units Remaining"] = display["Units Remaining"].map(_units)
            display["% Consumed"]      = display["% Consumed"].map(
                lambda x: f"{x:.1f}%" if pd.notna(x) else ""
            )
            display["Labor Pool"]  = display["Labor Pool"].map(_dollar)
            display["Cost / Unit"] = display["Cost / Unit"].map(
                lambda x: f"${float(x):.6f}" if pd.notna(x) else ""
            )
            st.dataframe(display, use_container_width=True, hide_index=True)


def _render_fifo_applied(period: str):
    st.markdown(
        f'<h3 style="color:{SECTION_HEADER_COLOR};">FIFO Applied</h3>',
        unsafe_allow_html=True,
    )
    st.caption("Sales matched to production layers. Applied cost is recognized COGS for the period.")

    applied = get_fifo_applied(period)
    if applied.empty:
        st.info("No FIFO applications for this period.")
        return

    total_applied = applied["applied_cost"].sum()
    total_units   = applied["units_applied"].sum()
    st.caption(
        f"Total recognized: {_dollar(total_applied)}  "
        f"·  Total units: {_units(total_units)}  "
        f"·  Invoices: {applied['invoice_num'].nunique()}"
    )

    for cost_center, cc_df in applied.groupby("cost_center"):
        with st.expander(
            f"{cost_center} — {_dollar(cc_df['applied_cost'].sum())}",
            expanded=False,
        ):
            display = cc_df[[
                "invoice_num", "customer_name", "customer_program",
                "iso_week_produced", "units_applied", "cost_per_unit",
                "applied_cost", "match_type", "applied_by",
            ]].copy()
            display.columns = [
                "Invoice", "Customer", "Program",
                "Week Produced", "Units Applied", "Cost / Unit",
                "Applied Cost", "Match Type", "Applied By",
            ]
            display["Units Applied"] = display["Units Applied"].map(_units)
            display["Cost / Unit"]   = display["Cost / Unit"].map(
                lambda x: f"${float(x):.6f}" if pd.notna(x) else ""
            )
            display["Applied Cost"]  = display["Applied Cost"].map(_dollar)
            st.dataframe(display, use_container_width=True, hide_index=True)


@st.cache_data(ttl=60, show_spinner=False)
def get_outstanding_fulfillment_wip_all_periods() -> pd.DataFrame:
    """
    Returns fulfillment WIP grouped by ORIGIN period — the period the labor
    was originally incurred. Nets applied amounts against original amounts
    at the (origin_period, program, cost_center, labor_type) grain.

    A row appearing here for origin_period = 2026-01 means: labor was
    accrued to a no-revenue program in Jan 2026 and is STILL not invoiced
    as of today. The longer the gap between origin_period and today, the
    bigger the audit concern.

    Origin period for applied rows is parsed from activity_driver, which
    is written as 'WIP from {origin_period}: {original_driver}' by
    write_fulfillment_wip_applied.

    All rows with non-zero outstanding amount are returned. 
    """
    return pd.read_sql(text("""
        WITH original AS (
            SELECT
                accrual_period    AS origin_period,
                bucket            AS cost_center,
                program,
                labor_type,
                SUM(applied_cost) AS original_amount
            FROM stg_labor_applied
            WHERE source = 'current_fulfillment_wip'
            GROUP BY 1, 2, 3, 4
        ),
        applied AS (
            SELECT
                SUBSTRING(activity_driver FROM 'WIP from (\d{4}-\d{2}):') AS origin_period,
                bucket            AS cost_center,
                program,
                labor_type,
                SUM(applied_cost) AS applied_amount
            FROM stg_labor_applied
            WHERE source = 'fulfillment_wip_applied'
              AND activity_driver ~ '^WIP from \d{4}-\d{2}:'
            GROUP BY 1, 2, 3, 4
        )
        SELECT
            o.origin_period,
            o.cost_center,
            o.program,
            o.labor_type,
            o.original_amount,
            COALESCE(a.applied_amount, 0)                         AS applied_amount,
            o.original_amount - COALESCE(a.applied_amount, 0)     AS outstanding_wip,
            EXTRACT(YEAR  FROM AGE(CURRENT_DATE, TO_DATE(o.origin_period, 'YYYY-MM'))) * 12
          + EXTRACT(MONTH FROM AGE(CURRENT_DATE, TO_DATE(o.origin_period, 'YYYY-MM'))) AS months_outstanding
        FROM original o
        LEFT JOIN applied a
            ON a.origin_period = o.origin_period
           AND a.cost_center   = o.cost_center
           AND a.program       = o.program
           AND a.labor_type    = o.labor_type
        WHERE o.original_amount - COALESCE(a.applied_amount, 0) > 0
        ORDER BY o.origin_period, o.program
    """), engine)


def _render_outstanding_wip_combined():
    """Combined Production + Fulfillment WIP across all periods.

    Top: by-program summary with both WIP types side-by-side and total.
    Below: expander with per-period breakdown, unified across both types.
    """
    st.markdown(
        f'<h3 style="color:{SECTION_HEADER_COLOR};">Outstanding WIP — All Periods</h3>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Combined view of Production and Fulfillment WIP on the balance "
        "sheet across all periods. Rows under $1.00 are hidden as "
        "immaterial rounding residual."
    )

    prod = get_outstanding_wip_all_periods()
    full = get_outstanding_fulfillment_wip_all_periods()

    if prod.empty and full.empty:
        st.success("No outstanding WIP across any period.")
        return

    # Normalize both into the same shape, then concat.
    if not prod.empty:
        prod_norm = prod.rename(columns={
            "accrual_period":  "period",
            "outstanding_wip": "outstanding_wip",
        })[["period", "cost_center", "customer_program", "outstanding_wip"]].copy()
        prod_norm["program"]    = prod_norm["customer_program"]
        prod_norm["wip_type"]   = "Production"
        prod_norm["labor_type"] = "direct_cogs"
        prod_norm["months_outstanding"] = pd.NA
        prod_norm = prod_norm[[
            "wip_type", "period", "cost_center", "program",
            "labor_type", "outstanding_wip", "months_outstanding",
        ]]
    else:
        prod_norm = pd.DataFrame(columns=[
            "wip_type", "period", "cost_center", "program",
            "labor_type", "outstanding_wip", "months_outstanding",
        ])

    if not full.empty:
        full_norm = full.rename(columns={"origin_period": "period"})[[
            "period", "cost_center", "program", "labor_type",
            "outstanding_wip", "months_outstanding",
        ]].copy()
        full_norm["wip_type"] = "Fulfillment"
        full_norm = full_norm[[
            "wip_type", "period", "cost_center", "program",
            "labor_type", "outstanding_wip", "months_outstanding",
        ]]
    else:
        full_norm = pd.DataFrame(columns=[
            "wip_type", "period", "cost_center", "program",
            "labor_type", "outstanding_wip", "months_outstanding",
        ])

    combined = pd.concat([prod_norm, full_norm], ignore_index=True)

    # =====================================================================
    # SUMMARY — by-program rollup with both WIP types side-by-side
    # =====================================================================
    summary = combined.pivot_table(
        index="program",
        columns="wip_type",
        values="outstanding_wip",
        aggfunc="sum",
        fill_value=0.0,
    ).reset_index()

    for col in ["Production", "Fulfillment"]:
        if col not in summary.columns:
            summary[col] = 0.0

    summary["Total"] = summary["Production"] + summary["Fulfillment"]
    summary = summary.sort_values("Total", ascending=False).reset_index(drop=True)

    grand_total       = float(summary["Total"].sum())
    grand_production  = float(summary["Production"].sum())
    grand_fulfillment = float(summary["Fulfillment"].sum())

    k1, k2, k3 = st.columns(3)
    k1.metric("Total Outstanding WIP",        _dollar(grand_total))
    k2.metric("Production",                   _dollar(grand_production))
    k3.metric("Fulfillment",                  _dollar(grand_fulfillment))
    st.markdown("")

    display_summary = summary.copy()
    display_summary = display_summary[["program", "Production", "Fulfillment", "Total"]]
    display_summary.columns = ["Program", "Production WIP", "Fulfillment WIP", "Total"]
    for c in ["Production WIP", "Fulfillment WIP", "Total"]:
        display_summary[c] = display_summary[c].map(_dollar)
    st.dataframe(display_summary, use_container_width=True, hide_index=True)

    # =====================================================================
    # DETAIL — by-period breakdown, both types interleaved
    # =====================================================================
    with st.expander("Detail by period", expanded=False):
        detail = combined.sort_values(
            ["period", "wip_type", "program"]
        ).reset_index(drop=True)

        display_detail = detail.copy()
        display_detail = display_detail[[
            "wip_type", "period", "cost_center", "program",
            "labor_type", "outstanding_wip", "months_outstanding",
        ]]
        display_detail.columns = [
            "Type", "Period", "Cost Center", "Program",
            "Labor Type", "Outstanding WIP", "Months Outstanding",
        ]
        display_detail["Outstanding WIP"]    = display_detail["Outstanding WIP"].map(_dollar)
        display_detail["Months Outstanding"] = display_detail["Months Outstanding"].map(
            lambda x: f"{int(x)}" if pd.notna(x) else ""
        )
        st.dataframe(display_detail, use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render():
    st.title("Labor Allocation Review")
    st.caption(
        "Review labor cost by program for each accrual period. "
        "Mark employees as cleared once allocation is confirmed correct."
    )

    periods = get_available_periods()
    if not periods:
        st.warning("No labor data found.")
        return

    user = auth.current_user()  # auth handled at app entry; just read here

    col1, col2 = st.columns([2, 6])
    with col1:
        selected_period = st.selectbox(
            "Accrual Period", periods, index=len(periods) - 1, key="wip_period",
        )
    with col2:
        st.markdown("**Reviewer**")
        st.markdown(f"{user['name']}  ·  `{user['role']}`")

    reviewer_name = user["name"]

    # ============================================================
    # Privacy toggle — defaults OFF so screen-share is safe
    # ============================================================
    show_amounts = st.sidebar.toggle(
        "Show dollar amounts",
        value=False,
        key="show_amounts_toggle",
        help="CFO/controller view. Off by default to protect privacy "
             "when reviewing with managers or supervisors.",
    )
    if not show_amounts:
        st.sidebar.caption("Amounts hidden on review tabs")

    # Preserved for render_allocation_tab compatibility until Phase 1b.3
    cost_type_filter = "All"

    existing_alloc = get_existing_allocation(selected_period)
    if not existing_alloc.empty:
        locked_by = existing_alloc["committed_by"].iloc[0] if "committed_by" in existing_alloc.columns else "unknown"
        locked_at = existing_alloc["committed_at"].iloc[0] if "committed_at" in existing_alloc.columns else ""
        st.success(f"Period {selected_period} allocation is committed by {locked_by} at {locked_at}. Use Unlock and Recommit in the Allocation tab to reopen.")
    else:
        st.warning(f"Period {selected_period} allocation has not been committed.")

    coverage = check_ukg_coverage(selected_period)
    if not coverage["all_ok"]:
        missing = []
        if not coverage["prior_ok"]:
            missing.append("prior month boundary paycheck")
        if not coverage["following_ok"]:
            missing.append(f"{coverage['next_month_name']} boundary paycheck")
        st.warning(
            f"UKG data may be incomplete for {selected_period} — "
            f"missing: {', '.join(missing)}. "
            "Employee review and allocation totals may be understated until all required reports are loaded."
        )

    # Lazy-loaded tabs. st.tabs renders ALL contents on every script run,
    # so an expensive tab (Allocation, with 4x calls into wlc.build_employee_allocations
    # plus 6 activity queries plus reconciliation plus charts) drags every
    # other interaction. st.segmented_control gives the same tab-bar UX but
    # only the active branch's render function runs.
    TABS = [
        "Direct Hire",
        "Temp Labor",
        "E-Commerce Config",
        "Receiving Returns",
        "Container Unload",
        "Allocation",
        "Outstanding WIP",
    ]
    active_tab = st.segmented_control(
        "View",
        options=TABS,
        default=TABS[0],
        key=f"labor_active_tab_{selected_period}",
        label_visibility="collapsed",
    ) or TABS[0]

    if active_tab == "Direct Hire":
        wlr.render_review_tab(selected_period, 'direct', reviewer_name, show_amounts=show_amounts)
    elif active_tab == "Temp Labor":
        wlr.render_review_tab(selected_period, 'temp', reviewer_name, show_amounts=show_amounts)
    elif active_tab == "E-Commerce Config":
        render_ecomm_config_tab(selected_period, reviewer_name)
    elif active_tab == "Receiving Returns":
        render_receiving_returns_tab(selected_period, reviewer_name)
    elif active_tab == "Container Unload":
        wcu.render_container_unload_tab(selected_period, reviewer_name)
    elif active_tab == "Allocation":
        render_allocation_tab(selected_period, reviewer_name, cost_type_filter)
    elif active_tab == "Outstanding WIP":
        render_production_wip_tab(selected_period, reviewer_name)
