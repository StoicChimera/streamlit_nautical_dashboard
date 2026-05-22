"""
wip_labor_compute.py
====================

Phase 1b.3 — Per-employee-line compute engine.

Replaces the old heuristic allocation engine. Reads explicit per-employee
allocation lines from stg_labor_employee_allocation and fans them out
to program-level rows, dispatching each cost_center line through the
driver specified in dim_cost_center.

Key design points:
  - Weekly drivers (demo, ogp, ow, shipments, receipts, container unload)
    fan out per ISO week using get_employee_weekly_cost.
  - Period drivers (inventory, sqft, revenue) collapse weekly cost first,
    then dispatch once.
  - direct_program lines emit a single row straight through.
  - Altria 10% cap applied on revenue_all driver only.

Output schema mirrors the old build_employee_heuristic_allocations for
downstream compatibility (write_labor_incurred, write_production_layers,
build_program_reconciliation, render_allocation_tab), with two additions:
  - cost_type: 'COGS' or 'SGA' (from dim_cost_center)
  - iso_week:  int or None (None for period-level drivers)
"""

import os
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

engine = create_engine(SUPABASE_CONN)


# Drivers that need ISO-week fanout. Only production drivers qualify, because
# they feed stg_wip_production_layers which keys off iso_week for FIFO matching.
# All other drivers dispatch at period level — weekly fanout would just create
# duplicate rows that get summed back in stg_labor_incurred anyway.
_WEEKLY_DRIVERS = {
    "units_demo", "units_ogp", "units_ow",
}

_ALTRIA_CAP = 0.10


# =============================================================
# Period-level driver readers
# =============================================================

def _get_shipments_by_iso_week(period: str, include_v6: bool) -> pd.DataFrame:
    """stg_extensiv_shipments split by v6 reference. Columns: iso_week, customer, units."""
    op = "ILIKE" if include_v6 else "NOT ILIKE"
    ref_filter = f"reference_no {op} '%v6%'" if include_v6 else (
        "(reference_no IS NULL OR reference_no NOT ILIKE '%v6%')"
    )
    sql = text(f"""
        SELECT
            EXTRACT(WEEK FROM TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY'))::int AS iso_week,
            COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
            COUNT(DISTINCT s.transaction_id)                   AS units
        FROM stg_extensiv_shipments s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer_report_raw)
            AND a.active = TRUE
        WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
          AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
          AND s.transaction_type_raw NOT ILIKE '%return%'
          AND {ref_filter}
          AND s.accrual_period = :period
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1, 2
        HAVING COUNT(DISTINCT s.transaction_id) > 0
    """)
    return pd.read_sql(sql, engine, params={"period": period})


def _get_receipts_by_iso_week(period: str, include_v6: bool) -> pd.DataFrame:
    """stg_extensiv_receipts split by v6 reference. Columns: iso_week, customer, units."""
    op = "ILIKE" if include_v6 else "NOT ILIKE"
    ref_filter = f"reference_no {op} '%v6%'" if include_v6 else (
        "(reference_no IS NULL OR reference_no NOT ILIKE '%v6%')"
    )
    sql = text(f"""
        SELECT
            EXTRACT(WEEK FROM TO_DATE(NULLIF(TRIM(s.report_start_raw), ''), 'MM/DD/YYYY'))::int AS iso_week,
            COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
            COUNT(DISTINCT s.transaction_id)                   AS units
        FROM stg_extensiv_receipts s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer_report_raw)
            AND a.active = TRUE
        WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
          AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
          AND {ref_filter}
          AND s.accrual_period = :period
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1, 2
        HAVING COUNT(DISTINCT s.transaction_id) > 0
    """)
    return pd.read_sql(sql, engine, params={"period": period})


def _get_container_unload_by_iso_week(period: str) -> pd.DataFrame:
    """stg_labor_container_unload pallet_count rolled up. Columns: iso_week, customer, units."""
    sql = text("""
        SELECT
            u.iso_week,
            c.customer_name AS customer,
            SUM(u.pallet_count)::numeric AS units
        FROM stg_labor_container_unload u
        JOIN dim_customer c
          ON c.canonical_key = u.customer_canonical_key
         AND c.active = TRUE
        WHERE u.accrual_period = :period
        GROUP BY 1, 2
        HAVING SUM(u.pallet_count) > 0
    """)
    return pd.read_sql(sql, engine, params={"period": period})


def _get_shipments_period(period: str, include_v6: bool) -> pd.DataFrame:
    """Period-aggregate of shipments. Columns: customer, units."""
    df = _get_shipments_by_iso_week(period, include_v6)
    if df.empty:
        return df
    return df.groupby("customer", as_index=False)["units"].sum()


def _get_receipts_period(period: str, include_v6: bool) -> pd.DataFrame:
    """Period-aggregate of receipts. Columns: customer, units.

    Applies ADVEXP redistribution at the period level: receipts from the bare
    "ADVEXP" customer (holding customer with no direct revenue program) are
    summed into a pool, then distributed across experiential customers by
    revenue weight. Mirrors the inventory pallet redistribution pattern.
    """
    # Pull all receipts including ADVEXP holding rows. include_v6 toggle is
    # not needed at receipt level — receipts use raw customer_report_raw, no
    # v6 reference filtering. Keeping the param for signature compatibility.
    df = pd.read_sql(
        text("""
            WITH aliased AS (
                SELECT
                    COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
                    COALESCE(a.exclude, FALSE)                         AS exclude,
                    COUNT(DISTINCT s.transaction_id)                   AS units
                FROM stg_extensiv_receipts s
                LEFT JOIN dim_customer_alias a
                    ON LOWER(a.alias) = LOWER(s.customer_report_raw)
                   AND a.active = TRUE
               WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
                AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
                AND s.accrual_period = :period
               GROUP BY 1, 2
            ),
            advexp_pool AS (
                SELECT COALESCE(SUM(units), 0) AS pool
                FROM aliased
                WHERE exclude = TRUE
                  AND LOWER(customer) NOT LIKE '%nautical%'
            ),
            real_customers AS (
                SELECT customer, SUM(units) AS units
                FROM aliased
                WHERE exclude = FALSE
                GROUP BY 1
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
                    )
                   AND a.active = TRUE
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
                   AND dc.roll_up_for_cost = FALSE
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
                    ROUND(((e.revenue / NULLIF(t.total_exp_revenue, 0)) * ap.pool)::numeric, 2)
                        AS advexp_units
                FROM exp_revenue e
                CROSS JOIN exp_total t
                CROSS JOIN advexp_pool ap
                WHERE ap.pool > 0
            )
            SELECT
                COALESCE(r.customer, aa.customer) AS customer,
                ROUND((COALESCE(r.units, 0) + COALESCE(aa.advexp_units, 0))::numeric, 2)
                    AS units
            FROM real_customers r
            FULL OUTER JOIN advexp_alloc aa
                ON LOWER(aa.customer) = LOWER(r.customer)
            WHERE COALESCE(r.units, 0) + COALESCE(aa.advexp_units, 0) > 0
            ORDER BY units DESC
        """),
        engine,
        params={"period": period},
    )
    return df


def _get_container_unload_period(period: str) -> pd.DataFrame:
    """Period-aggregate of container unload. Columns: customer, units."""
    df = _get_container_unload_by_iso_week(period)
    if df.empty:
        return df
    return df.groupby("customer", as_index=False)["units"].sum()


def _get_ecomm_orders_period(period: str) -> pd.DataFrame:
    """Non-v6 parcel orders scoped to elected E-Commerce programs only."""
    sql = text("""
        SELECT
            COALESCE(a.canonical_name, s.customer_report_raw) AS customer,
            COUNT(DISTINCT s.transaction_id)                   AS units
        FROM stg_extensiv_shipments s
        LEFT JOIN dim_customer_alias a
            ON LOWER(a.alias) = LOWER(s.customer_report_raw)
           AND a.active = TRUE
        INNER JOIN stg_labor_ecomm_period_config e
            ON LOWER(e.customer_name) = LOWER(COALESCE(a.canonical_name, s.customer_report_raw))
           AND e.accrual_period = :period
           AND e.active = TRUE
        WHERE NULLIF(TRIM(s.customer_report_raw), '') IS NOT NULL
          AND NULLIF(TRIM(s.transaction_id),      '') IS NOT NULL
          AND s.transaction_type_raw NOT ILIKE '%return%'
          AND (s.reference_no IS NULL OR s.reference_no NOT ILIKE '%v6%')
          AND s.accrual_period = :period
          AND COALESCE(a.exclude, FALSE) = FALSE
        GROUP BY 1
        HAVING COUNT(DISTINCT s.transaction_id) > 0
        ORDER BY units DESC
    """)
    return pd.read_sql(sql, engine, params={"period": period})


def _get_footprint(period: str) -> pd.DataFrame:
    """alloc_footprint_monthly_input is the final sqft allocation output — one row
    per (program, category) with shared_space already distributed upstream. We just
    sum sqft per program and use the result as the weight distribution.
    month_start = first of the period month (e.g. 2026-02-01 for '2026-02')."""
    sql = text("""
        SELECT
            c.customer_name AS customer,
            SUM(f.sqft)::numeric AS units
        FROM alloc_footprint_monthly_input f
        JOIN dim_customer c
          ON c.canonical_key = f.customer_canonical_key
         AND c.active = TRUE
        WHERE f.month_start = TO_DATE(:period, 'YYYY-MM')
        GROUP BY 1
        HAVING SUM(f.sqft) > 0
    """)
    return pd.read_sql(sql, engine, params={"period": period})


def _get_returns(period: str) -> pd.DataFrame:
    """stg_labor_receiving_returns return_count. Columns: customer, units."""
    sql = text("""
        SELECT customer_name AS customer,
               return_count::numeric AS units
        FROM stg_labor_receiving_returns
        WHERE accrual_period = :period
          AND return_count > 0
    """)
    return pd.read_sql(sql, engine, params={"period": period})


def _get_purchasing_program_names() -> set[str]:
    df = pd.read_sql(text("""
        SELECT customer_name FROM dim_customer
        WHERE active = TRUE
          AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
          AND is_purchasing_program = TRUE
    """), engine)
    return set(df["customer_name"].tolist())


# =============================================================
# Distribution helpers
# =============================================================

def _distribute_by_units(
    act_df: pd.DataFrame,
    cost: float,
    restrictions: list[str] | None,
    driver_label: str,
) -> list[dict]:
    """
    Splits `cost` across customers in `act_df` weighted by `units`.
    act_df must have columns: customer, units.
    Vectorized — to_dict('records') is ~100x faster than iterrows for this shape.
    """
    if act_df.empty:
        return []

    df = act_df
    if restrictions:
        df = df[df["customer"].isin(restrictions)]
        if df.empty:
            return []

    units = df["units"].astype(float)
    total = float(units.sum())
    if total <= 0:
        return []

    out = pd.DataFrame({
        "target_program": df["customer"].astype(str).values,
        "weight":         (units / total).values,
        "activity_value": units.values,
        "allocated_cost": (cost * units / total).values,
        "driver_label":   driver_label,
    })
    return out.to_dict("records")


def _distribute_by_revenue(
    revenue_df: pd.DataFrame,
    cost: float,
    restrictions: list[str] | None,
    driver_label: str,
    purchasing_only: bool = False,
    altria_cap: float | None = None,
) -> list[dict]:
    """
    Splits `cost` across customers weighted by revenue. Supports purchasing-only
    filter and an Altria cap (redistributes excess proportionally to non-Altria).
    """
    if revenue_df.empty:
        return []

    df = revenue_df.copy()
    if "customer_program" not in df.columns or "revenue" not in df.columns:
        return []

    if purchasing_only:
        purchasing = _get_purchasing_program_names()
        df = df[df["customer_program"].isin(purchasing)]

    if restrictions:
        df = df[df["customer_program"].isin(restrictions)]

    if df.empty:
        return []

    total = float(df["revenue"].sum())
    if total <= 0:
        return []

    df = df.copy()
    df["weight"] = df["revenue"] / total

    if altria_cap is not None:
        altria_mask = df["customer_program"].str.lower().str.contains("altria", na=False)
        altria_w = float(df.loc[altria_mask, "weight"].sum())
        if altria_w > altria_cap:
            excess = altria_w - altria_cap
            df.loc[altria_mask, "weight"] = altria_cap
            non_altria_w = float(df.loc[~altria_mask, "weight"].sum())
            if non_altria_w > 0:
                df.loc[~altria_mask, "weight"] += (
                    df.loc[~altria_mask, "weight"] / non_altria_w * excess
                )

    out = pd.DataFrame({
        "target_program": df["customer_program"].astype(str).values,
        "weight":         df["weight"].astype(float).values,
        "activity_value": df["revenue"].astype(float).values,
        "allocated_cost": (cost * df["weight"].astype(float)).values,
        "driver_label":   driver_label,
    })
    return out.to_dict("records")


# =============================================================
# Driver dispatch
# =============================================================

def _distribute_weekly(
    driver_key: str,
    cost: float,
    iso_week: int,
    activity: dict[str, pd.DataFrame],
    weekly_drivers: dict[str, pd.DataFrame],
    restrictions: list[str] | None,
) -> list[dict]:
    """Dispatch for drivers that have ISO-week resolution."""
    if driver_key == "units_demo":
        act = activity.get("demo", pd.DataFrame())
        sub = act[act["iso_week"] == iso_week] if not act.empty else act
        return _distribute_by_units(sub, cost, restrictions, "Demo Kits")

    if driver_key == "units_ogp":
        act = activity.get("ogp", pd.DataFrame())
        sub = act[act["iso_week"] == iso_week] if not act.empty else act
        return _distribute_by_units(sub, cost, restrictions, "OGP Bags")

    if driver_key == "units_ow":
        act = activity.get("ow", pd.DataFrame())
        sub = act[act["iso_week"] == iso_week] if not act.empty else act
        return _distribute_by_units(sub, cost, restrictions, "OW Units")

    if driver_key == "ltl_orders_v6":
        df = weekly_drivers.get("ltl_orders_v6", pd.DataFrame())
        sub = df[df["iso_week"] == iso_week] if not df.empty else df
        return _distribute_by_units(sub, cost, restrictions, "LTL Orders (v6)")

    if driver_key == "parcel_orders_non_v6":
        df = weekly_drivers.get("parcel_orders_non_v6", pd.DataFrame())
        sub = df[df["iso_week"] == iso_week] if not df.empty else df
        return _distribute_by_units(sub, cost, restrictions, "Parcel Orders")

    if driver_key == "received_pallets_v6":
        df = weekly_drivers.get("received_pallets_v6", pd.DataFrame())
        sub = df[df["iso_week"] == iso_week] if not df.empty else df
        return _distribute_by_units(sub, cost, restrictions, "Received Pallets (v6)")

    if driver_key == "received_parcel_non_v6":
        df = weekly_drivers.get("received_parcel_non_v6", pd.DataFrame())
        sub = df[df["iso_week"] == iso_week] if not df.empty else df
        return _distribute_by_units(sub, cost, restrictions, "Received Parcels")

    if driver_key == "orders_non_v6":
        df = weekly_drivers.get("parcel_orders_non_v6", pd.DataFrame())
        sub = df[df["iso_week"] == iso_week] if not df.empty else df
        return _distribute_by_units(sub, cost, restrictions, "E-Comm Orders (non-v6)")

    if driver_key == "unload_pallets":
        df = weekly_drivers.get("unload_pallets", pd.DataFrame())
        sub = df[df["iso_week"] == iso_week] if not df.empty else df
        return _distribute_by_units(sub, cost, restrictions, "Container Pallets")

    return [{
        "target_program": f"UNKNOWN DRIVER: {driver_key}",
        "weight": 1.0,
        "activity_value": 0,
        "allocated_cost": cost,
        "driver_label": "Unknown",
    }]


def _distribute_period(
    driver_key: str,
    cost: float,
    activity: dict[str, pd.DataFrame],
    period_drivers: dict[str, pd.DataFrame],
    revenue_df: pd.DataFrame,
    restrictions: list[str] | None,
) -> list[dict]:
    """Dispatch for drivers that are period-level (no weekly resolution)."""
    if driver_key == "movable_units":
        act = activity.get("inventory", pd.DataFrame())
        return _distribute_by_units(act, cost, restrictions, "Pallets (3mo Avg)")

    if driver_key == "sqft":
        df = period_drivers.get("sqft", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "Square Feet")

    if driver_key == "return_count":
        df = period_drivers.get("return_count", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "Return Count")

    if driver_key == "ltl_orders_v6":
        df = period_drivers.get("ltl_orders_v6", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "LTL Orders (v6)")

    if driver_key == "parcel_orders_non_v6":
        df = period_drivers.get("parcel_orders_non_v6", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "Parcel Orders")

    if driver_key == "received_pallets_v6":
        df = period_drivers.get("received_pallets_v6", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "Received Pallets (v6)")

    if driver_key == "received_parcel_non_v6":
        df = period_drivers.get("received_parcel_non_v6", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "Received Parcels")

    if driver_key == "orders_non_v6":
        df = period_drivers.get("orders_non_v6", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "E-Comm Orders")

    if driver_key == "unload_pallets":
        df = period_drivers.get("unload_pallets", pd.DataFrame())
        return _distribute_by_units(df, cost, restrictions, "Container Pallets")

    if driver_key == "revenue_all":
        return _distribute_by_revenue(
            revenue_df, cost, restrictions, "Revenue",
            purchasing_only=False, altria_cap=_ALTRIA_CAP,
        )

    if driver_key == "revenue_purchasing":
        return _distribute_by_revenue(
            revenue_df, cost, restrictions, "Revenue (Purchasing)",
            purchasing_only=True, altria_cap=None,
        )

    return [{
        "target_program": f"UNKNOWN DRIVER: {driver_key}",
        "weight": 1.0,
        "activity_value": 0,
        "allocated_cost": cost,
        "driver_label": "Unknown",
    }]


# =============================================================
# Weekly cost readers (copied from wip_labor_allocation for independence)
# =============================================================

def _get_employee_weekly_cost_direct(period: str, employee: str) -> pd.DataFrame:
    sql = text("""
        WITH expanded AS (
            SELECT d.total_labor_cost,
                   d.pay_period_start,
                   d.pay_period_end,
                   gs.week_start,
                   EXTRACT(WEEK FROM gs.week_start)::int AS iso_week,
                   CEIL((d.pay_period_end - d.pay_period_start + 1)::numeric / 7) AS total_weeks
            FROM stg_labor_direct_hire d
            CROSS JOIN LATERAL generate_series(
                DATE_TRUNC('week', d.pay_period_start),
                DATE_TRUNC('week', d.pay_period_end),
                INTERVAL '1 week'
            ) AS gs(week_start)
            WHERE d.accrual_period = :period
              AND d.employee_name  = :employee
        )
        SELECT iso_week,
               ROUND(SUM(total_labor_cost / NULLIF(total_weeks, 0))::numeric, 2) AS total_labor_cost
        FROM expanded
        GROUP BY iso_week
        ORDER BY iso_week
    """)
    return pd.read_sql(sql, engine, params={"period": period, "employee": employee})


def _get_employee_weekly_cost_temp(period: str, employee: str) -> pd.DataFrame:
    sql = text("""
        SELECT iso_week,
               ROUND(SUM(total_labor_cost)::numeric, 2) AS total_labor_cost
        FROM stg_labor_temp
        WHERE accrual_period = :period
          AND employee_name  = :employee
        GROUP BY iso_week
        ORDER BY iso_week
    """)
    return pd.read_sql(sql, engine, params={"period": period, "employee": employee})


# =============================================================
# Main entry point
# =============================================================

def build_employee_allocations(
    period: str,
    activity: dict[str, pd.DataFrame],
    revenue_df: pd.DataFrame,
    return_warnings: bool = False,
):
    """
    Fans out approved per-employee-line allocations into program-level rows.

    Returns columns:
        target_program, employee_name, labor_source, source_bucket,
        source_assignment, role_detail, cost_type, weight, activity_driver,
        activity_value, allocated_cost, iso_week

    Weekly fanout ONLY for production drivers (Demo/OGP/OW) since they feed
    stg_wip_production_layers for FIFO matching. All other drivers dispatch
    at period level — one row per (employee, target_program, cost_center).

    Lines with no driver data do NOT get placeholder rows in the output.
    They're tracked in the warnings list and surfaced to the reviewer before commit.

    labor_source is 'Direct COGS', 'Direct SG&A', or 'Temp' for display compat.
    cost_type is 'COGS' or 'SGA'.
    iso_week is int for production drivers, None for everything else.

    If return_warnings=True, returns (rows_df, warnings_list). Otherwise just rows_df.
    Each warning dict: {employee_name, role_name, cost_center, driver_key, cost}.
    """
    lines_df = pd.read_sql(text("""
        SELECT ea.employee_name, ea.labor_source, ea.role_name,
               ea.line_order, ea.line_type,
               ea.target_program, ea.cost_center_name,
               ea.allocation_pct, ea.program_restrictions,
               cc.driver_key, cc.cost_type
        FROM stg_labor_employee_allocation ea
        LEFT JOIN dim_cost_center cc
          ON cc.cost_center_name = ea.cost_center_name
         AND cc.active = TRUE
        WHERE ea.accrual_period = :period
          AND ea.reviewed = TRUE
        ORDER BY ea.employee_name, ea.line_order
    """), engine, params={"period": period})

    if lines_df.empty:
        empty = pd.DataFrame()
        return (empty, []) if return_warnings else empty

    # For direct_program lines, cost_type comes from the role (since there's no cost center)
    roles_df = pd.read_sql(text("""
        SELECT role_name, cost_type AS role_cost_type
        FROM dim_nmf_role
        WHERE active = TRUE
    """), engine)
    lines_df = lines_df.merge(roles_df, on="role_name", how="left")

    # Collapse cost_type: prefer cost center's if present, else role's, else default COGS
    def _resolve_cost_type(r):
        if pd.notna(r["cost_type"]):
            return str(r["cost_type"])
        if pd.notna(r.get("role_cost_type")):
            return str(r["role_cost_type"])
        return "COGS"

    lines_df["effective_cost_type"] = lines_df.apply(_resolve_cost_type, axis=1)

    # Pre-load weekly costs in TWO queries (one per labor_source), not N+1.
    # Previously this loop made ~150 sequential SQL round-trips at ~150ms each
    # for network latency, which dominated total compute time.
    weekly_costs: dict[tuple[str, str], dict[int, float]] = {}

    direct_wc = pd.read_sql(text("""
        WITH expanded AS (
            SELECT d.employee_name,
                   d.total_labor_cost,
                   d.pay_period_start,
                   d.pay_period_end,
                   gs.week_start,
                   EXTRACT(WEEK FROM gs.week_start)::int AS iso_week,
                   CEIL((d.pay_period_end - d.pay_period_start + 1)::numeric / 7) AS total_weeks
            FROM stg_labor_direct_hire d
            CROSS JOIN LATERAL generate_series(
                DATE_TRUNC('week', d.pay_period_start),
                DATE_TRUNC('week', d.pay_period_end),
                INTERVAL '1 week'
            ) AS gs(week_start)
            WHERE d.accrual_period = :period
        )
        SELECT employee_name,
               iso_week,
               ROUND(SUM(total_labor_cost / NULLIF(total_weeks, 0))::numeric, 2) AS total_labor_cost
        FROM expanded
        GROUP BY employee_name, iso_week
    """), engine, params={"period": period})

    temp_wc = pd.read_sql(text("""
        SELECT employee_name,
               iso_week,
               ROUND(SUM(total_labor_cost)::numeric, 2) AS total_labor_cost
        FROM stg_labor_temp
        WHERE accrual_period = :period
        GROUP BY employee_name, iso_week
    """), engine, params={"period": period})

    for emp_name, grp in direct_wc.groupby("employee_name"):
        weekly_costs[(str(emp_name), "direct")] = dict(
            zip(grp["iso_week"].astype(int), grp["total_labor_cost"].astype(float))
        )
    for emp_name, grp in temp_wc.groupby("employee_name"):
        weekly_costs[(str(emp_name), "temp")] = dict(
            zip(grp["iso_week"].astype(int), grp["total_labor_cost"].astype(float))
        )

    # Pre-load weekly drivers (production only — fanned out per ISO week)
    weekly_drivers = {}  # Demo/OGP/OW come from the `activity` dict passed in

    # Pre-load period drivers (dispatched once per line at period level)
    period_drivers = {
        "sqft":                   _get_footprint(period),
        "return_count":           _get_returns(period),
        "ltl_orders_v6":          _get_shipments_period(period, include_v6=True),
        "parcel_orders_non_v6":   _get_shipments_period(period, include_v6=False),
        "orders_non_v6":          _get_ecomm_orders_period(period),
        "received_pallets_v6":    _get_receipts_period(period, include_v6=True),
        "received_parcel_non_v6": _get_receipts_period(period, include_v6=False),
        "unload_pallets":         _get_container_unload_period(period),
    }

    rows = []
    warnings: list[dict] = []

    for _, line in lines_df.iterrows():
        emp          = str(line["employee_name"])
        src          = str(line["labor_source"])
        role_name    = str(line["role_name"])
        pct          = float(line["allocation_pct"])
        cost_type    = str(line["effective_cost_type"])
        restrictions = list(line["program_restrictions"]) if line["program_restrictions"] else None

        labor_source_label = _labor_source_label(src, cost_type)
        employee_weeks = weekly_costs.get((emp, src), {})
        total_line_cost = sum(employee_weeks.values()) * pct

        if total_line_cost <= 0:
            continue

        # ------------------------------------------------------------
        # direct_program: emit one row (period-level, no iso_week)
        # ------------------------------------------------------------
        if line["line_type"] == "direct_program":
            rows.append({
                "target_program":    str(line["target_program"]),
                "employee_name":     emp,
                "labor_source":      labor_source_label,
                "source_bucket":     str(line["target_program"]),
                "source_assignment": "",
                "role_detail":       role_name,
                "cost_type":         cost_type,
                "weight":            1.0,
                "activity_driver":   "Direct Assignment",
                "activity_value":    total_line_cost,
                "allocated_cost":    round(total_line_cost, 2),
                "iso_week":          None,
            })
            continue

        # ------------------------------------------------------------
        # cost_center: dispatch by driver_key
        # ------------------------------------------------------------
        cc_name    = str(line["cost_center_name"])
        driver_key = str(line["driver_key"]) if pd.notna(line["driver_key"]) else ""

        if not driver_key:
            warnings.append({
                "employee_name": emp,
                "role_name":     role_name,
                "cost_center":   cc_name,
                "driver_key":    "(missing)",
                "cost":          round(total_line_cost, 2),
                "reason":        "Cost center has no driver_key configured",
            })
            continue

        # Weekly drivers — production only, fan out per ISO week
        if driver_key in _WEEKLY_DRIVERS:
            any_week_had_activity = False
            for iso_week, weekly_cost in employee_weeks.items():
                week_line_cost = weekly_cost * pct
                if week_line_cost <= 0:
                    continue
                distributed = _distribute_weekly(
                    driver_key, week_line_cost, iso_week,
                    activity, weekly_drivers, restrictions,
                )
                if not distributed:
                    continue
                any_week_had_activity = True
                for d in distributed:
                    rows.append({
                        "target_program":    d["target_program"],
                        "employee_name":     emp,
                        "labor_source":      labor_source_label,
                        "source_bucket":     cc_name,
                        "source_assignment": "",
                        "role_detail":       role_name,
                        "cost_type":         cost_type,
                        "weight":            d["weight"],
                        "activity_driver":   d["driver_label"],
                        "activity_value":    d["activity_value"],
                        "allocated_cost":    round(d["allocated_cost"], 2),
                        "iso_week":          iso_week,
                    })
            if not any_week_had_activity:
                warnings.append({
                    "employee_name": emp,
                    "role_name":     role_name,
                    "cost_center":   cc_name,
                    "driver_key":    driver_key,
                    "cost":          round(total_line_cost, 2),
                    "reason":        "No activity data for any ISO week in this period",
                })
        else:
            # Period driver — dispatch once with total cost
            distributed = _distribute_period(
                driver_key, total_line_cost,
                activity, period_drivers, revenue_df, restrictions,
            )
            if not distributed:
                warnings.append({
                    "employee_name": emp,
                    "role_name":     role_name,
                    "cost_center":   cc_name,
                    "driver_key":    driver_key,
                    "cost":          round(total_line_cost, 2),
                    "reason":        "No driver data for this period",
                })
                continue
            for d in distributed:
                rows.append({
                    "target_program":    d["target_program"],
                    "employee_name":     emp,
                    "labor_source":      labor_source_label,
                    "source_bucket":     cc_name,
                    "source_assignment": "",
                    "role_detail":       role_name,
                    "cost_type":         cost_type,
                    "weight":            d["weight"],
                    "activity_driver":   d["driver_label"],
                    "activity_value":    d["activity_value"],
                    "allocated_cost":    round(d["allocated_cost"], 2),
                    "iso_week":          None,
                })

    result = pd.DataFrame(rows) if rows else pd.DataFrame()
    return (result, warnings) if return_warnings else result


def _labor_source_label(labor_source: str, cost_type: str) -> str:
    """Map (labor_source, cost_type) to the display label used by existing tabs."""
    if labor_source == "direct":
        return "Direct SG&A" if cost_type == "SGA" else "Direct COGS"
    return "Temp"


def _driver_label(driver_key: str) -> str:
    return {
        "units_demo":             "Demo Kits",
        "units_ogp":              "OGP Bags",
        "units_ow":               "OW Units",
        "ltl_orders_v6":          "LTL Orders (v6)",
        "parcel_orders_non_v6":   "Parcel Orders",
        "received_pallets_v6":    "Received Pallets (v6)",
        "received_parcel_non_v6": "Received Parcels",
        "orders_non_v6":          "E-Comm Orders",
        "unload_pallets":         "Container Pallets",
        "movable_units":          "Pallets (3mo Avg)",
        "sqft":                   "Square Feet",
        "return_count":           "Return Count",
        "revenue_all":            "Revenue",
        "revenue_purchasing":     "Revenue (Purchasing)",
    }.get(driver_key, driver_key)