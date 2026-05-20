"""
Warehouse overhead allocation compute engine.

Reads:
  - alloc_warehouse_cost_monthly          (total $ to allocate)
  - alloc_warehouse_shared_sqft_monthly   (manual sqft by bucket)
  - stg_extensiv_receipts                 (dock inbound driver)
  - stg_extensiv_shipments               (dock outbound driver)
  - stg_extensiv_stock_status            (storage/pallet driver)
  - stg_smartsheet_demo                  (demo kits driver)
  - stg_smartsheet_ogp                   (ogp bags driver)
  - stg_smartsheet_overwrap              (ow units driver)
  - stg_labor_ecomm_period_config        (e-comm program elections)
  - stg_labor_direct_hire                (office headcount split)
  - dim_customer_alias                   (customer name normalization)
  - dim_customer                         (is_experiential flag)
  - stg_product_service_detail           (revenue weights)

Writes:
  - stg_warehouse_allocation             (committed output)

Bucket → driver map:
  Storage:
    Experiential ADV        → inventory pallets (experiential customers only)
    OGP ADV - Overwrap      → OGP units
    Shared Storage - A Racks → a-rack count by customer
    Unallocated             → revenue weighted (all programs), cost_type=cogs

  Shared:
    Office/Inventory        → ops/SGA headcount split from approved labor
                              ops share → revenue weighted, cost_type=cogs
                              sga share → revenue weighted, cost_type=sga
    Shared/Unassigned       → revenue weighted, cost_type=sga

  Production:
    OGP ADV                 → OGP bags
    Demo ADV                → Demo kits
    E-Comm                  → revenue weight of elected programs
    OverWrap                → OW units

  Dock - Inbound:
    Demo - ADV - Inbound    → receipt count, experiential Demo ADV customers only
    OGP - ADV - Inbound     → receipt count, experiential OGP ADV customers only

  Dock - Outbound:
    Demo - ADV - Outbound   → shipment count, experiential Demo ADV customers only
    OGP - ADV - Outbound    → shipment count, experiential OGP ADV customers only
"""

import os
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# =====================================================
# DB connection
# =====================================================
load_dotenv()
_CONN = os.getenv("SUPABASE_CONN")
if not _CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

engine = create_engine(_CONN, pool_pre_ping=True)

# =====================================================
# Bucket definitions
# =====================================================

# cost_type for each bucket
BUCKET_COST_TYPE: dict[str, str] = {
    # Storage
    "Experiential ADV":         "cogs",
    "OGP ADV - Overwrap":       "cogs",
    "Shared Storage - A Racks": "cogs",
    "Overwrap Racking":         "cogs",   # NEW: 3mo avg MU, customer ILIKE recess/arrived
    "Demo Racking":             "cogs",   # NEW: 3mo avg MU, customer ILIKE demo
    "Walmart Bulk Area":        "cogs",   # NEW: 3mo avg MU, customer ILIKE walmart

    # Shared
    "Office/Inventory":         "mixed",  # split computed at runtime
    "Shared/Unassigned":        "sga",

    # Production
    "OGP ADV":                  "cogs",
    "Demo ADV":                 "cogs",
    "E-Comm":                   "cogs",
    "OverWrap":                 "cogs",
    "Gaylords":                 "cogs",   # NEW: ow units, customer ILIKE retailer set

    # Dock - Outbound
    "AMT/GP Bulk and Outbound": "cogs",   # NEW: shipments where reference ILIKE %v6%
    "E-Comm Dock Outbound":     "cogs",   # NEW: shipments for ecomm config customers

    # Deprecated buckets removed: Demo - ADV - Inbound, OGP - ADV - Inbound,
    # Demo - ADV - Outbound, OGP - ADV - Outbound. Historical periods retain
    # data in stg_warehouse_allocation; new periods will not seed or compute.
}

ALL_BUCKETS = list(BUCKET_COST_TYPE.keys())


# =====================================================
# Helper: period conversion
# =====================================================

def _period(month_start: date) -> str:
    """Convert date to YYYY-MM string for driver queries."""
    return month_start.strftime("%Y-%m")


# =====================================================
# Helper: alias map
# =====================================================

def _get_alias_map() -> dict[str, str]:
    df = pd.read_sql(
        text("""
            SELECT alias, canonical_name
            FROM dim_customer_alias
            WHERE active = TRUE AND exclude = FALSE AND canonical_name IS NOT NULL
        """),
        engine,
    )
    return {row["alias"].lower(): row["canonical_name"] for _, row in df.iterrows()}


def _apply_alias(name: str, alias_map: dict[str, str]) -> str:
    return alias_map.get(str(name).lower(), str(name))


# =====================================================
# Helper: revenue by program for the period
# =====================================================

def _get_revenue(period: str) -> pd.DataFrame:
    return pd.read_sql(
        text("""
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
                ) AND a.active = TRUE
            WHERE s.contract_completion_date IS NOT NULL
              AND TRIM(s.contract_completion_date::text) != ''
              AND DATE_TRUNC('month', s.contract_completion_date::date) = TO_DATE(:period, 'YYYY-MM')
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
            HAVING SUM(s.amount) > 0
            ORDER BY revenue DESC
        """),
        engine,
        params={"period": period},
    )


# =====================================================
# Driver: receipts (dock inbound)
# Returns all customers with receipt counts for the period.
# =====================================================

def _get_receipts(period: str, alias_map: dict) -> pd.DataFrame:
    df = pd.read_sql(
        text("""
            SELECT
                COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
                COUNT(DISTINCT s.transaction_id)                   AS units
            FROM stg_extensiv_receipts s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer_report_raw)
                AND a.active = TRUE
            WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
              AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
              AND NULLIF(TRIM(s.report_start_raw),    '') IS NOT NULL
              AND DATE_TRUNC('month', TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY'))
                  = TO_DATE(:period, 'YYYY-MM')
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
            HAVING COUNT(DISTINCT s.transaction_id) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )
    return df


# =====================================================
# Driver: shipments (dock outbound)
# =====================================================

def _get_shipments(period: str, alias_map: dict) -> pd.DataFrame:
    df = pd.read_sql(
        text("""
            SELECT
                COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
                COUNT(DISTINCT s.transaction_id)                   AS units
            FROM stg_extensiv_shipments s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer_report_raw)
                AND a.active = TRUE
            WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
              AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
              AND NULLIF(TRIM(s.report_start_raw),    '') IS NOT NULL
              AND s.transaction_type_raw NOT ILIKE '%return%'
              AND DATE_TRUNC('month', TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY'))
                  = TO_DATE(:period, 'YYYY-MM')
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
            HAVING COUNT(DISTINCT s.transaction_id) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )
    return df


# =====================================================
# Driver: inventory pallets (storage)
# Reuses the full ADVEXP redistribution logic from wip_labor.py
# =====================================================

def _get_inventory(period: str) -> pd.DataFrame:
    return pd.read_sql(
        text("""
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
                    END AS customer,
                    COALESCE(a.exclude, FALSE) AS exclude,
                    CAST(s.as_of_ts AS TIMESTAMP)::date AS snap_date,
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
                  AND NULLIF(TRIM(s.as_of_ts), '') IS NOT NULL
                GROUP BY 1, 2, 3
            ),
            per_customer AS (
                SELECT
                    customer,
                    exclude,
                    ROUND(AVG(mu_count)::numeric, 2) AS avg_pallets
                FROM aliased
                GROUP BY 1, 2
                HAVING AVG(mu_count) > 0
            ),
            exp_customers AS (
                SELECT LOWER(customer_name) AS customer_name
                FROM dim_customer
                WHERE active = TRUE
                  AND is_revenue_customer = TRUE
                  AND is_experiential = TRUE
                  AND roll_up_for_cost = FALSE
            ),
            advexp_pool AS (
                SELECT COALESCE(SUM(p.avg_pallets), 0) AS pool
                FROM per_customer p
                WHERE p.exclude = TRUE
                  AND LOWER(p.customer) NOT LIKE '%nautical%'
            ),
            real_customers AS (
                SELECT
                    p.customer,
                    p.avg_pallets
                FROM per_customer p
                INNER JOIN exp_customers e
                    ON LOWER(p.customer) = e.customer_name
                WHERE p.exclude = FALSE
            ),
            exp_revenue AS (
                SELECT
                    COALESCE(
                        a.canonical_name,
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
                    )
                   AND a.active = TRUE
                INNER JOIN dim_customer dc
                    ON LOWER(dc.customer_name) = LOWER(
                        COALESCE(
                            a.canonical_name,
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
                   AND dc.roll_up_for_cost = FALSE
                WHERE p.contract_completion_date IS NOT NULL
                  AND TRIM(p.contract_completion_date::text) != ''
                  AND DATE_TRUNC('month', p.contract_completion_date::date) = TO_DATE(:period, 'YYYY-MM')
                  AND COALESCE(a.exclude, FALSE) = FALSE
                GROUP BY 1
            ),
            exp_total AS (
                SELECT SUM(revenue) AS total_exp_revenue
                FROM exp_revenue
            ),
            advexp_alloc AS (
                SELECT
                    e.customer_program AS customer,
                    ROUND(((e.revenue / NULLIF(t.total_exp_revenue, 0)) * ap.pool)::numeric, 2) AS advexp_pallets
                FROM exp_revenue e
                CROSS JOIN exp_total t
                CROSS JOIN advexp_pool ap
                WHERE ap.pool > 0
            )
            SELECT
                r.customer,
                ROUND((r.avg_pallets + COALESCE(a.advexp_pallets, 0))::numeric, 2) AS units
            FROM real_customers r
            LEFT JOIN advexp_alloc a
                ON LOWER(a.customer) = LOWER(r.customer)
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )


# =====================================================
# Driver: a-rack count by customer
# =====================================================

def _get_aracks(period: str) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            WITH aliased AS (
                SELECT
                    CASE
                        WHEN s.customer_clean ILIKE '%LT-STG%'
                          OR s.customer_clean ILIKE '%LT STG%'
                          OR s.customer_clean ILIKE 'FINISHED PALLETS LT%'
                            THEN 'Life Time'
                        ELSE COALESCE(a.canonical_name, s.customer_clean)
                    END AS customer,
                    a.exclude,
                    s.a_rack_count AS rack_count
                FROM v_aracks_month_latest_by_customer s
                LEFT JOIN dim_customer_alias a
                    ON LOWER(a.alias) = LOWER(s.customer_clean)
                    AND a.active = TRUE
                WHERE s.month_start = TO_DATE(:period, 'YYYY-MM')
            ),
            advexp_pool AS (
                SELECT COALESCE(SUM(rack_count), 0) AS pool
                FROM aliased
                WHERE exclude = TRUE
            ),
            real_customers AS (
                SELECT customer, SUM(rack_count) AS rack_count
                FROM aliased
                WHERE COALESCE(exclude, FALSE) = FALSE
                  AND LOWER(customer) NOT LIKE '%nautical%'
                GROUP BY 1
                HAVING SUM(rack_count) > 0
            ),
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
                    AND dc.is_experiential = TRUE AND dc.active = TRUE
                WHERE p.contract_completion_date IS NOT NULL
                  AND TRIM(p.contract_completion_date::text) != ''
                  AND DATE_TRUNC('month', p.contract_completion_date::date) = TO_DATE(:period, 'YYYY-MM')
                  AND COALESCE(a.exclude, FALSE) = FALSE
                GROUP BY 1
            ),
            exp_total AS (
                SELECT SUM(revenue) AS total_exp_revenue FROM exp_revenue
            ),
            advexp_alloc AS (
                SELECT
                    e.customer_program AS customer,
                    ROUND(((e.revenue / NULLIF(t.total_exp_revenue, 0)) * ap.pool)::numeric, 2) AS advexp_racks
                FROM exp_revenue e
                CROSS JOIN exp_total t
                CROSS JOIN advexp_pool ap
                WHERE ap.pool > 0
            )
            SELECT
                r.customer,
                ROUND((r.rack_count + COALESCE(aa.advexp_racks, 0))::numeric, 2) AS units
            FROM real_customers r
            LEFT JOIN advexp_alloc aa ON LOWER(aa.customer) = LOWER(r.customer)
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )


# =====================================================
# Driver: demo kits, ogp bags, ow units
# =====================================================

def _get_demo_units(period: str, alias_map: dict) -> pd.DataFrame:
    df = pd.read_sql(
        text("""
            SELECT
                COALESCE(a.canonical_name, s.customer) AS customer,
                SUM(s.number_of_cases_completed)       AS units
            FROM stg_smartsheet_demo s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer) AND a.active = TRUE
            WHERE s.accrual_month = :period
              AND s.number_of_cases_completed > 0
              AND s.normalized_date IS NOT NULL
              AND TRIM(s.normalized_date) != ''
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
            HAVING SUM(s.number_of_cases_completed) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )
    return df


def _get_ogp_units(period: str, alias_map: dict) -> pd.DataFrame:
    df = pd.read_sql(
        text("""
            SELECT
                COALESCE(a.canonical_name, s.job_name) AS customer,
                SUM(s.daily_production_complete)        AS units
            FROM stg_smartsheet_ogp s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.job_name) AND a.active = TRUE
            WHERE s.accrual_month = :period
              AND s.daily_production_complete > 0
              AND s.date IS NOT NULL
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
            HAVING SUM(s.daily_production_complete) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )
    return df


def _get_ow_units(period: str, alias_map: dict) -> pd.DataFrame:
    df = pd.read_sql(
        text("""
            SELECT
                COALESCE(a.canonical_name, s.customer) AS customer,
                SUM(s.units_produced)                  AS units
            FROM stg_smartsheet_overwrap s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer) AND a.active = TRUE
            WHERE s.accrual_month = :period
              AND s.units_produced > 0
              AND s.date_finished IS NOT NULL
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
            HAVING SUM(s.units_produced) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )
    return df


# =====================================================
# Driver: e-comm elected programs revenue weight
# =====================================================

def _get_ecomm_revenue_weights(period: str, revenue_df: pd.DataFrame) -> pd.DataFrame:
    config = pd.read_sql(
        text("""
            SELECT customer_name
            FROM stg_labor_ecomm_period_config
            WHERE accrual_period = :period AND active = TRUE
        """),
        engine,
        params={"period": period},
    )
    if config.empty:
        return pd.DataFrame(columns=["customer", "units"])

    elected = set(config["customer_name"].tolist())
    sub = revenue_df[revenue_df["customer_program"].isin(elected)].copy()
    if sub.empty:
        return pd.DataFrame(columns=["customer", "units"])

    sub = sub.rename(columns={"customer_program": "customer", "revenue": "units"})
    return sub[["customer", "units"]]


# =====================================================
# Driver: office headcount split
# Returns (ops_pct, sga_pct) from approved labor for the period.
# =====================================================

def _get_office_headcount_split(period: str) -> tuple[float, float]:
    df = pd.read_sql(
        text("""
            SELECT
                COUNT(CASE WHEN UPPER(COALESCE(nmf_role, '')) = 'SG&A' THEN 1 END) AS sga_count,
                COUNT(CASE WHEN UPPER(COALESCE(nmf_role, '')) != 'SG&A' THEN 1 END) AS ops_count
            FROM stg_labor_direct_hire
            WHERE accrual_period = :period AND reviewed = TRUE
        """),
        engine,
        params={"period": period},
    )
    if df.empty:
        return (0.5, 0.5)

    ops = float(df["ops_count"].iloc[0] or 0)
    sga = float(df["sga_count"].iloc[0] or 0)
    total = ops + sga
    if total == 0:
        return (0.5, 0.5)
    return (round(ops / total, 6), round(sga / total, 6))


# =====================================================
# Driver: experiential-only inventory pallets
# Filters _get_inventory output to is_experiential customers.
# =====================================================

def _get_experiential_programs(period: str) -> set[str]:
    df = pd.read_sql(
        text("""
            SELECT customer_name
            FROM dim_customer
            WHERE active = TRUE
              AND is_revenue_customer = TRUE
              AND is_experiential = TRUE
              AND roll_up_for_cost = FALSE
        """),
        engine,
    )
    return set(df["customer_name"].str.lower().tolist())


def _get_ogp_programs(period: str) -> set[str]:
    """Programs with activity_subclass = OGP and is_experiential = TRUE."""
    df = pd.read_sql(
        text("""
            SELECT customer_name
            FROM dim_customer
            WHERE active = TRUE
              AND is_experiential = TRUE
              AND activity_subclass = 'OGP'
              AND roll_up_for_cost = FALSE
        """),
        engine,
    )
    return set(df["customer_name"].str.lower().tolist())


def _get_demo_programs(period: str) -> set[str]:
    """Programs with activity_subclass = Demo and is_experiential = TRUE."""
    df = pd.read_sql(
        text("""
            SELECT customer_name
            FROM dim_customer
            WHERE active = TRUE
              AND is_experiential = TRUE
              AND activity_subclass = 'Demo'
              AND roll_up_for_cost = FALSE
        """),
        engine,
    )
    return set(df["customer_name"].str.lower().tolist())


# =====================================================
# Helper: trailing-3-month period range
# =====================================================

def _three_periods_back(month_start: date) -> list[str]:
    """
    Returns [n-2, n-1, n] as YYYY-MM strings. Used for trailing 3-month
    average drivers (Overwrap Racking, Demo Racking, Walmart Bulk Area).
    """
    y, m = month_start.year, month_start.month
    out = []
    for offset in (-2, -1, 0):
        nm = m + offset
        ny = y
        while nm <= 0:
            nm += 12
            ny -= 1
        out.append(f"{ny:04d}-{nm:02d}")
    return out


# =====================================================
# Driver: 3-month avg movable units, pattern-matched customer
# Used for Overwrap Racking, Demo Racking, Walmart Bulk Area.
# =====================================================

def _get_inventory_3mo_avg(
    month_start: date,
    customer_patterns: list[str],
) -> pd.DataFrame:
    """
    Per-customer 3-month average of movable-unit count, restricted to
    customers whose raw customer_clean ILIKE any of the supplied substring
    patterns (case-insensitive, wrapped with % wildcards).

    Average method:
      1. For each (customer, accrual_period, snap_date), count distinct MUs
         (or fall back to sum_on_hand_qty if MU labels are missing).
      2. For each (customer, accrual_period), average across snapshots.
      3. Across the 3 trailing months, average those monthly averages.

    Pool customers (alias.exclude=TRUE) are excluded — these buckets bill
    matching customers directly with no experiential redistribution.
    """
    periods = _three_periods_back(month_start)
    if not customer_patterns:
        return pd.DataFrame(columns=["customer", "units"])

    pattern_params = {f"pat_{i}": f"%{p}%" for i, p in enumerate(customer_patterns)}
    pattern_clauses = " OR ".join(
        f"s.customer_clean ILIKE :pat_{i}" for i in range(len(customer_patterns))
    )

    sql = f"""
        WITH snapshots AS (
            SELECT
                s.accrual_period,
                CASE
                    WHEN s.customer_clean ILIKE '%LT-STG%'
                      OR s.customer_clean ILIKE '%LT STG%'
                      OR s.customer_clean ILIKE 'Life Time%'
                      OR s.customer_clean ILIKE 'LifeTime%'
                      OR s.customer_clean ILIKE 'FINISHED PALLETS LT%'
                        THEN 'Life Time'
                    ELSE COALESCE(a.canonical_name, s.customer_clean)
                END AS customer,
                COALESCE(a.exclude, FALSE) AS exclude,
                CAST(s.as_of_ts AS TIMESTAMP)::date AS snap_date,
                CASE
                    WHEN COUNT(DISTINCT NULLIF(TRIM(s.movable_unit_label_1), '')) > 0
                        THEN COUNT(DISTINCT NULLIF(TRIM(s.movable_unit_label_1), ''))::numeric
                    ELSE COALESCE(SUM(s.sum_on_hand_qty), 0)::numeric
                END AS mu_count
            FROM stg_extensiv_stock_status s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer_clean)
                AND a.active = TRUE
            WHERE s.accrual_period = ANY(:periods)
              AND NULLIF(TRIM(s.customer_clean), '') IS NOT NULL
              AND NULLIF(TRIM(s.as_of_ts), '') IS NOT NULL
              AND ({pattern_clauses})
            GROUP BY 1, 2, 3, 4
        ),
        per_customer_month AS (
            SELECT customer, accrual_period, AVG(mu_count) AS month_avg
            FROM snapshots
            WHERE exclude = FALSE
              AND LOWER(customer) NOT LIKE '%nautical%'
            GROUP BY 1, 2
            HAVING AVG(mu_count) > 0
        )
        SELECT
            customer,
            ROUND(AVG(month_avg)::numeric, 2) AS units
        FROM per_customer_month
        GROUP BY 1
        HAVING AVG(month_avg) > 0
        ORDER BY units DESC
    """

    return pd.read_sql(
        text(sql),
        engine,
        params={"periods": periods, **pattern_params},
    )


# =====================================================
# Driver: AMT/GP shipments — reference field includes 'v6'
# =====================================================

def _get_amt_gp_shipments(period: str) -> pd.DataFrame:
    """
    Shipments where the reference field contains 'v6' (case-insensitive).

    VERIFY: column name `reference_num` is a placeholder. Confirm the actual
    column on stg_extensiv_shipments and update the WHERE clause if needed.
    Common alternatives: reference_no, customer_reference, reference_id.
    """
    return pd.read_sql(
        text("""
            SELECT
                COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
                COUNT(DISTINCT s.transaction_id)                   AS units
            FROM stg_extensiv_shipments s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer_report_raw)
                AND a.active = TRUE
            WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
              AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
              AND NULLIF(TRIM(s.report_start_raw),    '') IS NOT NULL
              AND s.transaction_type_raw NOT ILIKE '%return%'
              AND s.reference_no ILIKE '%v6%'
              AND DATE_TRUNC('month', TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY'))
                  = TO_DATE(:period, 'YYYY-MM')
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
            HAVING COUNT(DISTINCT s.transaction_id) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )


# =====================================================
# Driver: filtered overwrap units (Gaylords)
# =====================================================

def _get_overwrap_units_filtered(
    period: str,
    customer_patterns: list[str],
) -> pd.DataFrame:
    """
    Sum stg_smartsheet_overwrap.units_produced filtered to customers whose
    raw customer field ILIKE any of the given substrings.
    """
    if not customer_patterns:
        return pd.DataFrame(columns=["customer", "units"])

    pattern_params = {f"pat_{i}": f"%{p}%" for i, p in enumerate(customer_patterns)}
    pattern_clauses = " OR ".join(
        f"s.customer ILIKE :pat_{i}" for i in range(len(customer_patterns))
    )

    sql = f"""
        SELECT
            COALESCE(a.canonical_name, s.customer) AS customer,
            SUM(s.units_produced)                  AS units
        FROM stg_smartsheet_overwrap s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer) AND a.active = TRUE
        WHERE s.accrual_month = :period
          AND s.units_produced > 0
          AND s.date_finished IS NOT NULL
          AND COALESCE(a.exclude, FALSE) = FALSE
          AND ({pattern_clauses})
        GROUP BY 1
        HAVING SUM(s.units_produced) > 0
        ORDER BY units DESC
    """

    return pd.read_sql(
        text(sql),
        engine,
        params={"period": period, **pattern_params},
    )


# =====================================================
# Driver: shipments for E-Comm elected customers
# =====================================================

def _get_ecomm_shipments(period: str) -> pd.DataFrame:
    """
    Shipments for customers present in stg_labor_ecomm_period_config
    (active=TRUE) for this period. Used for the E-Comm Dock Outbound bucket.

    Customer match is on the alias-normalized name vs. customer_name in
    the config table.
    """
    return pd.read_sql(
        text("""
            WITH ecomm_customers AS (
                SELECT LOWER(TRIM(customer_name)) AS customer_name_lower
                FROM stg_labor_ecomm_period_config
                WHERE accrual_period = :period AND active = TRUE
            )
            SELECT
                COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
                COUNT(DISTINCT s.transaction_id)                   AS units
            FROM stg_extensiv_shipments s
            LEFT JOIN dim_customer_alias a
                ON LOWER(a.alias) = LOWER(s.customer_report_raw)
                AND a.active = TRUE
            WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
              AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
              AND NULLIF(TRIM(s.report_start_raw),    '') IS NOT NULL
              AND s.transaction_type_raw NOT ILIKE '%return%'
              AND DATE_TRUNC('month', TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY'))
                  = TO_DATE(:period, 'YYYY-MM')
              AND COALESCE(a.exclude, FALSE) = FALSE
              AND LOWER(COALESCE(a.canonical_name, s.customer_report_raw)) IN (
                  SELECT customer_name_lower FROM ecomm_customers
              )
            GROUP BY 1
            HAVING COUNT(DISTINCT s.transaction_id) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )

# =====================================================
# Core: allocate a single unit-driven bucket
# =====================================================

def _allocate_units(
    driver_df: pd.DataFrame,
    bucket_cost: float,
    bucket_name: str,
    category: str,
    cost_type: str,
    driver_label: str,
    bucket_sqft: float,
    total_sqft: float,
    total_wh_cost: float,
    committed_by: str,
    committed_at: str,
    month_start: date,
) -> list[dict]:
    rows = []
    if driver_df.empty or driver_df["units"].sum() == 0:
        return rows

    driver_df = driver_df.copy()
    driver_df["units"] = pd.to_numeric(driver_df["units"], errors="coerce").fillna(0)
    driver_df = driver_df[driver_df["units"] > 0].copy()
    if driver_df.empty:
        return rows

    total_units = float(driver_df["units"].sum())

    for _, row in driver_df.iterrows():
        units = float(row["units"])
        pct   = round(units / total_units, 6) if total_units > 0 else 0.0
        amount = round(bucket_cost * pct, 2)
        rows.append({
            "month_start":       month_start,
            "customer_program":  str(row["customer"]),
            "program_bucket":    bucket_name,
            "category":          category,
            "cost_type":         cost_type,
            "driver_type":       driver_label,
            "driver_value":      units,
            "total_driver":      total_units,
            "allocation_pct":    pct,
            "bucket_sqft":       bucket_sqft,
            "total_sqft":        total_sqft,
            "sqft_pct":          round(bucket_sqft / total_sqft, 6) if total_sqft > 0 else 0.0,
            "total_wh_cost":     total_wh_cost,
            "allocation_amount": amount,
            "committed_by":      committed_by,
            "committed_at":      committed_at,
        })
    return rows


def _allocate_revenue(
    revenue_df: pd.DataFrame,
    bucket_cost: float,
    bucket_name: str,
    category: str,
    cost_type: str,
    bucket_sqft: float,
    total_sqft: float,
    total_wh_cost: float,
    committed_by: str,
    committed_at: str,
    month_start: date,
) -> list[dict]:
    rows = []
    if revenue_df.empty:
        return rows

    df = revenue_df.copy()
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0)
    df = df[df["revenue"] > 0].copy()
    if df.empty:
        return rows

    total_rev = float(df["revenue"].sum())

    for _, row in df.iterrows():
        rev    = float(row["revenue"])
        pct    = round(rev / total_rev, 6) if total_rev > 0 else 0.0
        amount = round(bucket_cost * pct, 2)
        rows.append({
            "month_start":       month_start,
            "customer_program":  str(row["customer_program"]),
            "program_bucket":    bucket_name,
            "category":          category,
            "cost_type":         cost_type,
            "driver_type":       "Revenue",
            "driver_value":      rev,
            "total_driver":      total_rev,
            "allocation_pct":    pct,
            "bucket_sqft":       bucket_sqft,
            "total_sqft":        total_sqft,
            "sqft_pct":          round(bucket_sqft / total_sqft, 6) if total_sqft > 0 else 0.0,
            "total_wh_cost":     total_wh_cost,
            "allocation_amount": amount,
            "committed_by":      committed_by,
            "committed_at":      committed_at,
        })
    return rows


# =====================================================
# Public: sqft reader (used by allocations.py for display)
# =====================================================

def get_sqft_inputs(month_start: date) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            SELECT id, month_start, program_bucket, category, total_sqft, updated_by, updated_at
            FROM alloc_warehouse_shared_sqft_monthly
            WHERE month_start = :m AND is_active = TRUE
            ORDER BY category, program_bucket
        """),
        engine,
        params={"m": month_start},
    )


def get_sqft_months() -> list[date]:
    df = pd.read_sql(
        text("""
            SELECT DISTINCT month_start
            FROM alloc_warehouse_shared_sqft_monthly
            ORDER BY month_start DESC
        """),
        engine,
    )
    return df["month_start"].tolist()


def save_sqft_inputs(month_start: date, rows: list[dict], updated_by: str) -> None:
    """
    Upsert sqft values for the month. Each row: {program_bucket, total_sqft}.
    Uses partial unique index ux_alloc_wh_sqft_active.
    """
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        for r in rows:
            conn.execute(
                text("""
                    UPDATE alloc_warehouse_shared_sqft_monthly
                    SET total_sqft  = :sqft,
                        updated_by  = :by,
                        updated_at  = :at
                    WHERE month_start   = :m
                      AND program_bucket = :bucket
                      AND is_active      = TRUE
                """),
                {"m": month_start, "bucket": r["program_bucket"], "sqft": float(r["total_sqft"]), "by": updated_by, "at": now},
            )
            conn.execute(
                text("""
                    INSERT INTO alloc_warehouse_shared_sqft_monthly
                        (month_start, program_bucket, category, total_sqft, is_active, updated_by, updated_at)
                    SELECT :m, :bucket, :category, :sqft, TRUE, :by, :at
                    WHERE NOT EXISTS (
                        SELECT 1 FROM alloc_warehouse_shared_sqft_monthly
                        WHERE month_start = :m AND program_bucket = :bucket AND is_active = TRUE
                    )
                """),
                {"m": month_start, "bucket": r["program_bucket"], "category": r.get("category", ""), "sqft": float(r["total_sqft"]), "by": updated_by, "at": now},
            )


def seed_sqft_month(month_start: date) -> None:
    """Initialize sqft grid for a new month with 0 values."""
    buckets = [
        # Storage
        ("Experiential ADV",         "Storage"),
        ("OGP ADV - Overwrap",       "Storage"),
        ("Shared Storage - A Racks", "Storage"),
        ("Overwrap Racking",         "Storage"),
        ("Demo Racking",             "Storage"),
        ("Walmart Bulk Area",        "Storage"),

        # Shared
        ("Office/Inventory",         "Shared"),
        ("Shared/Unassigned",        "Shared"),

        # Production
        ("OGP ADV",                  "Production"),
        ("Demo ADV",                 "Production"),
        ("E-Comm",                   "Production"),
        ("OverWrap",                 "Production"),
        ("Gaylords",                 "Production"),

        # Dock - Outbound
        ("AMT/GP Bulk and Outbound", "Dock - Outbound"),
        ("E-Comm Dock Outbound",     "Dock - Outbound"),
    ]
    with engine.begin() as conn:
        for bucket, category in buckets:
            conn.execute(
                text("""
                    INSERT INTO alloc_warehouse_shared_sqft_monthly
                        (month_start, program_bucket, category, total_sqft, is_active)
                    VALUES (:m, :bucket, :cat, 0, TRUE)
                    ON CONFLICT DO NOTHING
                """),
                {"m": month_start, "bucket": bucket, "cat": category},
            )


def copy_sqft_forward(prev_month: date, new_month: date) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE alloc_warehouse_shared_sqft_monthly
                SET is_active = FALSE, updated_at = NOW()
                WHERE month_start = :m AND is_active = TRUE
            """),
            {"m": new_month},
        )
        conn.execute(
            text("""
                INSERT INTO alloc_warehouse_shared_sqft_monthly
                    (month_start, program_bucket, category, total_sqft, is_active)
                SELECT :new_m, program_bucket, category, total_sqft, TRUE
                FROM alloc_warehouse_shared_sqft_monthly
                WHERE month_start = :prev_m 
                AND is_active = TRUE
                AND program_bucket = ANY(:active_buckets)
            """),
            {"prev_m": prev_month, "new_m": new_month, "active_buckets": ALL_BUCKETS},
        )


# =====================================================
# Public: committed allocation reader
# =====================================================

def get_committed_allocation(month_start: date) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            SELECT *
            FROM stg_warehouse_allocation
            WHERE month_start = :m
            ORDER BY category, program_bucket, allocation_amount DESC
        """),
        engine,
        params={"m": month_start},
    )


def is_committed(month_start: date) -> bool:
    df = pd.read_sql(
        text("SELECT 1 FROM stg_warehouse_allocation WHERE month_start = :m LIMIT 1"),
        engine,
        params={"m": month_start},
    )
    return not df.empty


def unlock_allocation(month_start: date) -> None:
    period = _period(month_start)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_warehouse_allocation WHERE month_start = :m"),
            {"m": month_start},
        )
        # Also clear any WIP applications written FOR this period (current period
        # consuming prior WIP). These were tied to this period's commit and
        # should not survive an unlock.
        conn.execute(
            text("DELETE FROM stg_warehouse_wip_applied WHERE accrual_period = :p"),
            {"p": period},
        )


def get_warehouse_wip(month_start: date) -> pd.DataFrame:
    """Programs with warehouse cost but no revenue in the period."""
    period = _period(month_start)
    return pd.read_sql(
        text("""
            SELECT
                wa.customer_program,
                wa.program_bucket,
                wa.category,
                wa.cost_type,
                SUM(wa.allocation_amount) AS warehouse_cost
            FROM stg_warehouse_allocation wa
            WHERE wa.month_start = :m
              AND NOT EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start = :m
                    AND mv.customer_program = wa.customer_program
              )
            GROUP BY 1, 2, 3, 4
            ORDER BY warehouse_cost DESC
        """),
        engine,
        params={"m": month_start},
    )


def get_prior_warehouse_wip_applicable(month_start: date) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            SELECT
                wa.month_start::text                          AS origin_period,
                wa.customer_program,
                wa.program_bucket,
                wa.category,
                wa.cost_type,
                SUM(wa.allocation_amount)                     AS warehouse_cost
            FROM stg_warehouse_allocation wa
            WHERE wa.month_start < :m
              AND NOT EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start = wa.month_start
                    AND mv.customer_program = wa.customer_program
              )
              AND EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start = :m
                    AND mv.customer_program = wa.customer_program
              )
              AND NOT EXISTS (
                  SELECT 1 FROM stg_warehouse_wip_applied wwa
                  WHERE wwa.accrual_period = TO_CHAR(:m, 'YYYY-MM')
                    AND wwa.origin_period    = wa.month_start::text
                    AND wwa.customer_program = wa.customer_program
                    AND wwa.program_bucket   = wa.program_bucket
              )
            GROUP BY wa.month_start::text, wa.customer_program, wa.program_bucket, wa.category, wa.cost_type
            ORDER BY wa.month_start::text, warehouse_cost DESC
        """),
        engine,
        params={"m": month_start},
    )


def get_warehouse_wip_all_periods(as_of_month: date | None = None) -> pd.DataFrame:
    """Outstanding warehouse WIP across all periods up to and including as_of_month.
    If as_of_month is None, returns all periods (legacy behavior)."""
    where_clause = ""
    params: dict = {}
    if as_of_month is not None:
        where_clause = "AND wa.month_start <= :as_of"
        params["as_of"] = as_of_month

    return pd.read_sql(
        text(f"""
            SELECT
                TO_CHAR(wa.month_start, 'YYYY-MM')            AS accrual_period,
                wa.customer_program,
                wa.program_bucket,
                wa.category,
                SUM(wa.allocation_amount)                      AS warehouse_cost
            FROM stg_warehouse_allocation wa
            WHERE NOT EXISTS (
                SELECT 1 FROM mv_program_profitability mv
                WHERE mv.month_start = wa.month_start
                  AND mv.customer_program = wa.customer_program
            )
            AND NOT EXISTS (
                SELECT 1 FROM stg_warehouse_wip_applied wwa
                WHERE wwa.origin_period    = wa.month_start::text
                  AND wwa.customer_program = wa.customer_program
                  AND wwa.program_bucket   = wa.program_bucket
            )
            {where_clause}
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, warehouse_cost DESC
        """),
        engine,
        params=params,
    )


def write_warehouse_wip_applied(
    month_start: date,
    rows: list[dict],
    locked_by: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    period = _period(month_start)
    with engine.begin() as conn:
        for r in rows:
            conn.execute(
                text("""
                    INSERT INTO stg_warehouse_wip_applied
                        (accrual_period, origin_period, customer_program,
                         program_bucket, category, cost_type, warehouse_cost,
                         locked, locked_by, locked_at)
                    VALUES
                        (:period, :origin, :program,
                         :bucket, :category, :cost_type, :cost,
                         TRUE, :by, :now)
                    ON CONFLICT (accrual_period, origin_period, customer_program, program_bucket)
                    DO UPDATE SET
                        warehouse_cost = EXCLUDED.warehouse_cost,
                        locked_by      = EXCLUDED.locked_by,
                        locked_at      = EXCLUDED.locked_at
                """),
                {
                    "period":    period,
                    "origin":    r["origin_period"],
                    "program":   r["customer_program"],
                    "bucket":    r["program_bucket"],
                    "category":  r["category"],
                    "cost_type": r["cost_type"],
                    "cost":      float(r["warehouse_cost"]),
                    "by":        locked_by,
                    "now":       now,
                },
            )

# =====================================================
# MAIN COMPUTE FUNCTION
# =====================================================

def compute_warehouse_allocation(
    month_start: date,
    committed_by: str,
    total_sqft_override: float | None = None,
) -> tuple[list[dict], dict]:
    """
    Computes the full warehouse allocation for a month.

    Returns:
        (rows, diagnostics)
        rows         — list of dicts ready to write to stg_warehouse_allocation
        diagnostics  — dict of per-bucket summaries for UI display
    """
    period       = _period(month_start)
    committed_at = datetime.now(timezone.utc).isoformat()

    # ---- inputs ----
    sqft_df = get_sqft_inputs(month_start)
    if sqft_df.empty:
        return [], {"error": "No sqft inputs found for this month. Initialize the sqft grid first."}

    wh_cost_df = pd.read_sql(
        text("SELECT allocated_warehouse_cost FROM alloc_warehouse_cost_monthly WHERE month_start = :m"),
        engine,
        params={"m": month_start},
    )
    if wh_cost_df.empty:
        return [], {"error": "No warehouse cost entry found for this month."}

    total_wh_cost = float(wh_cost_df["allocated_warehouse_cost"].iloc[0])
    shared_sqft = float(sqft_df["total_sqft"].sum())
    total_sqft  = total_sqft_override if total_sqft_override is not None else shared_sqft

    if total_sqft == 0:
        return [], {"error": "Total sqft is zero — enter sqft values before committing."}

    sqft_map = dict(zip(sqft_df["program_bucket"], sqft_df["total_sqft"].astype(float)))

    # ---- shared data ----
    # demo_programs / receipts_df / shipments_df were used by the deprecated
    # dock-inbound and dock-outbound buckets. The new dock buckets use
    # dedicated query helpers (_get_amt_gp_shipments, _get_ecomm_shipments)
    # so the broad shipments_df fetch is no longer needed.
    alias_map        = _get_alias_map()
    revenue_df       = _get_revenue(period)
    exp_programs     = _get_experiential_programs(period)
    ogp_programs     = _get_ogp_programs(period)
    inventory_df     = _get_inventory(period)
    demo_units_df    = _get_demo_units(period, alias_map)
    ogp_units_df     = _get_ogp_units(period, alias_map)
    ow_units_df      = _get_ow_units(period, alias_map)
    ops_pct, sga_pct = _get_office_headcount_split(period)

    all_rows    = []
    diagnostics = {}

    def _bucket_cost(bucket_name: str) -> float:
        sqft = sqft_map.get(bucket_name, 0.0)
        return round(total_wh_cost * (sqft / total_sqft), 2) if total_sqft > 0 else 0.0

    def _record(bucket, rows):
        diagnostics[bucket] = {
            "sqft":         sqft_map.get(bucket, 0.0),
            "bucket_cost":  _bucket_cost(bucket),
            "row_count":    len(rows),
            "total_allocated": sum(r["allocation_amount"] for r in rows),
        }
        all_rows.extend(rows)

    # ---- Experiential ADV (Storage) ----
    # Filter inventory to experiential customers only
    bucket = "Experiential ADV"
    exp_inv = inventory_df[inventory_df["customer"].str.lower().isin(exp_programs)].copy()
    _record(bucket, _allocate_units(
        exp_inv, _bucket_cost(bucket), bucket, "Storage", "cogs",
        "Pallets (Experiential)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- OGP ADV - Overwrap (Storage) ----
    bucket = "OGP ADV - Overwrap"
    ogp_inv = inventory_df[inventory_df["customer"].str.lower().isin(ogp_programs)].copy()
    _record(bucket, _allocate_units(
        ogp_inv, _bucket_cost(bucket), bucket, "Storage", "cogs",
        "Pallets (OGP ADV)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- Shared Storage - A Racks (Storage) ----
    bucket   = "Shared Storage - A Racks"
    aracks_df = _get_aracks(period)
    if not aracks_df.empty:
        aracks_df = aracks_df.rename(columns={"customer_clean": "customer"})
    _record(bucket, _allocate_units(
        aracks_df, _bucket_cost(bucket), bucket, "Storage", "cogs",
        "A-Rack Count", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- Office/Inventory (Shared) — mixed split ----
    bucket      = "Office/Inventory"
    office_cost = _bucket_cost(bucket)
    ops_cost    = round(office_cost * ops_pct, 2)
    sga_cost    = round(office_cost * sga_pct, 2)

    ops_rows = _allocate_revenue(
        revenue_df, ops_cost, bucket, "Shared", "cogs",
        sqft_map.get(bucket, 0), total_sqft, total_wh_cost,
        committed_by, committed_at, month_start,
    )
    for r in ops_rows:
        r["driver_type"] = f"Revenue (Ops {ops_pct:.1%} of headcount)"

    sga_rows = _allocate_revenue(
        revenue_df, sga_cost, bucket, "Shared", "sga",
        sqft_map.get(bucket, 0), total_sqft, total_wh_cost,
        committed_by, committed_at, month_start,
    )
    for r in sga_rows:
        r["driver_type"] = f"Revenue (SGA {sga_pct:.1%} of headcount)"

    _record(bucket, ops_rows + sga_rows)

    # ---- Shared/Unassigned (Shared) — SGA ----
    bucket = "Shared/Unassigned"
    _record(bucket, _allocate_revenue(
        revenue_df, _bucket_cost(bucket), bucket, "Shared", "sga",
        sqft_map.get(bucket, 0), total_sqft, total_wh_cost,
        committed_by, committed_at, month_start,
    ))

    # ---- OGP ADV (Production) ----
    bucket = "OGP ADV"
    _record(bucket, _allocate_units(
        ogp_units_df, _bucket_cost(bucket), bucket, "Production", "cogs",
        "OGP Bags", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- Demo ADV (Production) ----
    bucket = "Demo ADV"
    _record(bucket, _allocate_units(
        demo_units_df, _bucket_cost(bucket), bucket, "Production", "cogs",
        "Demo Kits", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- E-Comm (Production) ----
    bucket      = "E-Comm"
    ecomm_df    = _get_ecomm_revenue_weights(period, revenue_df)
    ecomm_alloc = []
    if not ecomm_df.empty:
        ecomm_rev = ecomm_df.rename(columns={"customer": "customer_program", "units": "revenue"})
        ecomm_alloc = _allocate_revenue(
            ecomm_rev, _bucket_cost(bucket), bucket, "Production", "cogs",
            sqft_map.get(bucket, 0), total_sqft, total_wh_cost,
            committed_by, committed_at, month_start,
        )
        for r in ecomm_alloc:
            r["driver_type"] = "Revenue (E-Comm elected)"
    _record(bucket, ecomm_alloc)

    # ---- OverWrap (Production) ----
    bucket = "OverWrap"
    _record(bucket, _allocate_units(
        ow_units_df, _bucket_cost(bucket), bucket, "Production", "cogs",
        "OW Units", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # =================================================
    # NEW STORAGE BUCKETS — 3-month avg MU, pattern-matched
    # =================================================

    # ---- Overwrap Racking (Storage) ----
    # Customer raw name contains 'recess' or 'arrived'
    bucket = "Overwrap Racking"
    ow_rack_df = _get_inventory_3mo_avg(month_start, ["recess", "arrived"])
    _record(bucket, _allocate_units(
        ow_rack_df, _bucket_cost(bucket), bucket, "Storage", "cogs",
        "MU 3mo Avg (Recess/Arrived)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- Demo Racking (Storage) ----
    # Customer raw name contains 'demo'
    bucket = "Demo Racking"
    demo_rack_df = _get_inventory_3mo_avg(month_start, ["demo"])
    _record(bucket, _allocate_units(
        demo_rack_df, _bucket_cost(bucket), bucket, "Storage", "cogs",
        "MU 3mo Avg (Demo)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- Walmart Bulk Area (Storage) ----
    # Customer raw name contains 'walmart'
    bucket = "Walmart Bulk Area"
    wm_bulk_df = _get_inventory_3mo_avg(month_start, ["walmart"])
    _record(bucket, _allocate_units(
        wm_bulk_df, _bucket_cost(bucket), bucket, "Storage", "cogs",
        "MU 3mo Avg (Walmart)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # =================================================
    # NEW PRODUCTION BUCKET — Gaylords
    # =================================================

    # ---- Gaylords (Production) ----
    # Overwrap units_produced from stg_smartsheet_overwrap where the
    # customer field literally contains 'OGP' or 'other'. The smartsheet
    # buckets non-OGP retailer flow under the literal value 'other', so
    # filtering by retailer names individually would miss them.
    bucket = "Gaylords"
    gaylords_df = _get_overwrap_units_filtered(period, ["ogp", "other"])
    _record(bucket, _allocate_units(
        gaylords_df, _bucket_cost(bucket), bucket, "Production", "cogs",
        "OW Units (OGP/Other)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # =================================================
    # NEW DOCK - OUTBOUND BUCKETS
    # =================================================

    # ---- AMT/GP Bulk and Outbound (Dock - Outbound) ----
    # Shipments where the reference field includes 'v6'
    bucket = "AMT/GP Bulk and Outbound"
    amt_gp_df = _get_amt_gp_shipments(period)
    _record(bucket, _allocate_units(
        amt_gp_df, _bucket_cost(bucket), bucket, "Dock - Outbound", "cogs",
        "Shipments (Reference contains v6)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    # ---- E-Comm Dock Outbound (Dock - Outbound) ----
    # Shipments for customers present in stg_labor_ecomm_period_config
    bucket = "E-Comm Dock Outbound"
    ecomm_ship_df = _get_ecomm_shipments(period)
    _record(bucket, _allocate_units(
        ecomm_ship_df, _bucket_cost(bucket), bucket, "Dock - Outbound", "cogs",
        "Shipments (E-Comm elected)", sqft_map.get(bucket, 0), total_sqft,
        total_wh_cost, committed_by, committed_at, month_start,
    ))

    return all_rows, diagnostics


# =====================================================
# COMMIT
# =====================================================

def commit_warehouse_allocation(month_start: date, committed_by: str) -> tuple[int, dict]:
    """
    Runs the compute and writes results to stg_warehouse_allocation.
    Returns (rows_written, diagnostics).
    """
    rows, diagnostics = compute_warehouse_allocation(month_start, committed_by)

    if "error" in diagnostics:
        return 0, diagnostics

    committed_at = datetime.now(timezone.utc).isoformat()

    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_warehouse_allocation WHERE month_start = :m"),
            {"m": month_start},
        )
        for r in rows:
            conn.execute(
                text("""
                    INSERT INTO stg_warehouse_allocation (
                        month_start, customer_program, program_bucket, category,
                        cost_type, driver_type, driver_value, total_driver,
                        allocation_pct, bucket_sqft, total_sqft, sqft_pct,
                        total_wh_cost, allocation_amount,
                        committed_by, committed_at
                    ) VALUES (
                        :month_start, :customer_program, :program_bucket, :category,
                        :cost_type, :driver_type, :driver_value, :total_driver,
                        :allocation_pct, :bucket_sqft, :total_sqft, :sqft_pct,
                        :total_wh_cost, :allocation_amount,
                        :committed_by, :committed_at
                    )
                """),
                {**r, "committed_at": committed_at},
            )

    return len(rows), diagnostics