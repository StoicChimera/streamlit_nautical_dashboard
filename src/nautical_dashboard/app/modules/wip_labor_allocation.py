"""
wip_labor_allocation.py
=======================

Data layer for the new per-employee allocation model.

Read + write helpers for three tables:
    dim_nmf_role
    dim_cost_center
    stg_labor_employee_allocation

Also provides ancillary readers used by the review UI:
    get_prior_period_allocation   — carry-forward support
    list_employees_for_review     — joined view for the review tab
    get_all_revenue_programs      — dropdown feeder for direct_program lines

No UI. No compute. UI lives in wip_labor_review.py; compute wiring
happens in Phase 1b.3.

Line contract (used by save_employee_allocation):
    {
        'line_type':            'direct_program' | 'cost_center',
        'target_program':       str | None,
        'cost_center_name':     str | None,
        'allocation_pct':       float (0 < pct <= 1),
        'program_restrictions': list[str] | None,
    }
"""

import os
import pandas as pd
import streamlit as st
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

engine = create_engine(SUPABASE_CONN)


# -------------------------------------------------------------
# Dim catalog readers
# -------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def get_available_roles() -> pd.DataFrame:
    sql = text("""
        SELECT role_id,
               role_name,
               cost_type,
               default_cost_center,
               is_direct_assignment,
               direct_assignment_program,
               notes
        FROM dim_nmf_role
        WHERE active = TRUE
        ORDER BY role_name
    """)
    return pd.read_sql(sql, engine)


@st.cache_data(ttl=300, show_spinner=False)
def get_available_cost_centers() -> pd.DataFrame:
    sql = text("""
        SELECT cost_center_id,
               cost_center_name,
               driver_type,
               driver_key,
               driver_source_description,
               notes
        FROM dim_cost_center
        WHERE active = TRUE
        ORDER BY cost_center_name
    """)
    return pd.read_sql(sql, engine)


@st.cache_data(ttl=300, show_spinner=False)
def get_programs_for_cost_center(cost_center_name: str, period: str) -> list[str]:
    """Customers eligible for program_restrictions multi-select under the given cost center."""
    name = (cost_center_name or "").strip()

    all_billable_sql = """
        SELECT customer_name FROM dim_customer
        WHERE active = TRUE AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
        ORDER BY customer_name
    """

    subclass_map = {'Demo': 'Demo', 'OGP': 'OGP', 'Overwrap': 'Overwrap'}

    fulfillment_class_centers = {
        'Shipping LTL', 'Shipping Parcel', 'Inventory',
        'Receiving Parcel', 'Receiving Pallet',
    }

    revenue_all_centers = {
        'IT', 'Marketing', 'Finance', 'Executive', 'Facilities', 'Sales',
    }

    if name in subclass_map:
        sql = text("""
            SELECT customer_name FROM dim_customer
            WHERE active = TRUE AND is_revenue_customer = TRUE
              AND roll_up_for_cost = FALSE
              AND activity_subclass = :subclass
            ORDER BY customer_name
        """)
        df = pd.read_sql(sql, engine, params={'subclass': subclass_map[name]})
    elif name == 'E-Commerce Picking':
        sql = text("""
            SELECT customer_name FROM stg_labor_ecomm_period_config
            WHERE accrual_period = :period AND active = TRUE
            ORDER BY customer_name
        """)
        df = pd.read_sql(sql, engine, params={'period': period})
    elif name == 'Container Unload':
        # Only customers with containers actually received in the period
        sql = text("""
            SELECT DISTINCT c.customer_name
            FROM stg_labor_container_unload u
            JOIN dim_customer c
              ON c.canonical_key = u.customer_canonical_key
             AND c.active = TRUE
             AND c.is_revenue_customer = TRUE
             AND c.roll_up_for_cost = FALSE
            WHERE u.accrual_period = :period
            ORDER BY c.customer_name
        """)
        df = pd.read_sql(sql, engine, params={'period': period})
    elif name == 'Returns':
        # Only customers with return entries for the period
        sql = text("""
            SELECT DISTINCT customer_name
            FROM stg_labor_receiving_returns
            WHERE accrual_period = :period
              AND return_count > 0
            ORDER BY customer_name
        """)
        df = pd.read_sql(sql, engine, params={'period': period})
    elif name in fulfillment_class_centers:
        sql = text("""
            SELECT customer_name FROM dim_customer
            WHERE active = TRUE AND is_revenue_customer = TRUE
              AND roll_up_for_cost = FALSE
              AND activity_class = 'Fulfillment'
            ORDER BY customer_name
        """)
        df = pd.read_sql(sql, engine)
    elif name == 'Purchasing':
        sql = text("""
            SELECT customer_name FROM dim_customer
            WHERE active = TRUE AND is_revenue_customer = TRUE
              AND roll_up_for_cost = FALSE
              AND is_purchasing_program = TRUE
            ORDER BY customer_name
        """)
        df = pd.read_sql(sql, engine)
    elif name in revenue_all_centers:
        df = pd.read_sql(text(all_billable_sql), engine)
    else:
        raise ValueError(f"Unknown cost center: {cost_center_name!r}")

    return df['customer_name'].tolist()


@st.cache_data(ttl=300, show_spinner=False)
def get_all_revenue_programs() -> list[str]:
    """Direct-assignment dropdown universe for direct_program allocation lines.

    Only customers explicitly marked as valid direct-hire targets. Rollup
    nodes (Advantage - Demo, Advantage - OGP, etc.) must be excluded here —
    labor charged to a rollup terminates there instead of fanning out to
    actual customer programs via produced units, which is the bug this
    filter prevents.
    """
    df = pd.read_sql(text("""
        SELECT customer_name
        FROM dim_customer
        WHERE active = TRUE
          AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
          AND allow_direct_hire = TRUE
        ORDER BY customer_name
    """), engine)
    return df['customer_name'].tolist()


# -------------------------------------------------------------
# Employee allocation reader
# -------------------------------------------------------------

def _parse_restrictions(val):
    if val is None:
        return None
    return list(val) if len(val) > 0 else None


def get_employee_allocation(period: str, employee: str, labor_source: str) -> dict:
    """Full allocation record for one (period, employee, labor_source).
    Returns empty dict if no record exists."""
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")

    sql = text("""
        SELECT role_name, reviewed, reviewed_by, reviewed_at,
               line_order, line_type, target_program, cost_center_name,
               allocation_pct, program_restrictions
        FROM stg_labor_employee_allocation
        WHERE accrual_period = :period
          AND employee_name  = :employee
          AND labor_source   = :labor_source
        ORDER BY line_order
    """)
    df = pd.read_sql(sql, engine, params={
        'period': period, 'employee': employee, 'labor_source': labor_source,
    })
    if df.empty:
        return {}

    header = df.iloc[0]
    lines = [
        {
            'line_order':           int(r['line_order']),
            'line_type':            str(r['line_type']),
            'target_program':       r['target_program'] if pd.notna(r['target_program']) else None,
            'cost_center_name':     r['cost_center_name'] if pd.notna(r['cost_center_name']) else None,
            'allocation_pct':       float(r['allocation_pct']),
            'program_restrictions': _parse_restrictions(r['program_restrictions']),
        }
        for _, r in df.iterrows()
    ]
    return {
        'role_name':   str(header['role_name']),
        'reviewed':    bool(header['reviewed']),
        'reviewed_by': str(header['reviewed_by']) if pd.notna(header['reviewed_by']) else None,
        'reviewed_at': str(header['reviewed_at']) if pd.notna(header['reviewed_at']) else None,
        'lines':       lines,
    }


def get_prior_period_allocation(period: str, employee: str, labor_source: str) -> dict:
    """Most recent reviewed allocation for this employee before the given period.
    Returns empty dict if none exists.

    Extra key in return dict: 'source_period' — which period we pulled from.
    reviewed/reviewed_by/reviewed_at are NOT returned (carry-forward resets review state).
    """
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")

    sql = text("""
        WITH latest AS (
            SELECT MAX(accrual_period) AS source_period
            FROM stg_labor_employee_allocation
            WHERE employee_name = :employee
              AND labor_source  = :labor_source
              AND reviewed      = TRUE
              AND accrual_period < :period
        )
        SELECT sa.accrual_period, sa.role_name,
               sa.line_order, sa.line_type,
               sa.target_program, sa.cost_center_name,
               sa.allocation_pct, sa.program_restrictions
        FROM stg_labor_employee_allocation sa
        JOIN latest l ON sa.accrual_period = l.source_period
        WHERE sa.employee_name = :employee
          AND sa.labor_source  = :labor_source
        ORDER BY sa.line_order
    """)
    df = pd.read_sql(sql, engine, params={
        'period': period, 'employee': employee, 'labor_source': labor_source,
    })
    if df.empty:
        return {}

    lines = [
        {
            'line_order':           int(r['line_order']),
            'line_type':            str(r['line_type']),
            'target_program':       r['target_program'] if pd.notna(r['target_program']) else None,
            'cost_center_name':     r['cost_center_name'] if pd.notna(r['cost_center_name']) else None,
            'allocation_pct':       float(r['allocation_pct']),
            'program_restrictions': _parse_restrictions(r['program_restrictions']),
        }
        for _, r in df.iterrows()
    ]
    return {
        'source_period': str(df.iloc[0]['accrual_period']),
        'role_name':     str(df.iloc[0]['role_name']),
        'lines':         lines,
    }


# -------------------------------------------------------------
# Employee list for review tab
# -------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def list_employees_for_review(period: str, labor_source: str) -> pd.DataFrame:
    """
    Joined view of UKG employees with their allocation state for review.

    Returns columns:
        employee_name, total_labor_cost, ukg_program, ukg_role,
        role_name, reviewed, reviewed_by, reviewed_at,
        sum_pct, line_count, is_new_employee
    """
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")

    if labor_source == 'direct':
        ukg_cte = """
            SELECT employee_name,
                   SUM(total_labor_cost)  AS total_labor_cost,
                   MAX(nmf_program)       AS ukg_program,
                   MAX(nmf_role)          AS ukg_role,
                   bool_and(NOT EXISTS (
                       SELECT 1 FROM stg_labor_direct_hire prior
                       WHERE prior.employee_id    = stg_labor_direct_hire.employee_id
                         AND prior.accrual_period < :period
                   )) AS is_new_employee
            FROM stg_labor_direct_hire
            WHERE accrual_period = :period
            GROUP BY employee_name
        """
    else:
        ukg_cte = """
            SELECT employee_name,
                   SUM(total_labor_cost)  AS total_labor_cost,
                   MAX(sbs2_raw)          AS ukg_program,
                   MAX(sbs3)              AS ukg_role,
                   bool_and(NOT EXISTS (
                       SELECT 1 FROM stg_labor_temp prior
                       WHERE prior.employee_name  = stg_labor_temp.employee_name
                         AND prior.accrual_period < :period
                   )) AS is_new_employee
            FROM stg_labor_temp
            WHERE accrual_period = :period
              AND COALESCE(sbs3, '') != 'Not Nautical'
              AND NULLIF(TRIM(COALESCE(sbs2_raw, '')), '') IS NOT NULL
              AND (
                  sbs1     ILIKE '%nautical%'
                  OR sbs2_raw ILIKE '%nautical%'
                  OR sbs2_raw ILIKE '%altria%'
                  OR sbs2_raw ILIKE '%lifetime%'
                  OR sbs2_raw ILIKE '%life time%'
              )
            GROUP BY employee_name
        """

    sql = text(f"""
        WITH ukg AS ({ukg_cte}),
        alloc AS (
            SELECT employee_name,
                   MAX(role_name)                  AS role_name,
                   bool_or(reviewed)               AS reviewed,
                   MAX(reviewed_by)                AS reviewed_by,
                   MAX(reviewed_at::text)          AS reviewed_at,
                   COALESCE(SUM(allocation_pct),0) AS sum_pct,
                   COUNT(*)                        AS line_count
            FROM stg_labor_employee_allocation
            WHERE accrual_period = :period
              AND labor_source   = :labor_source
            GROUP BY employee_name
        )
        SELECT u.employee_name,
               u.total_labor_cost,
               u.ukg_program,
               u.ukg_role,
               u.is_new_employee,
               a.role_name,
               a.reviewed,
               a.reviewed_by,
               a.reviewed_at,
               COALESCE(a.sum_pct, 0)    AS sum_pct,
               COALESCE(a.line_count, 0) AS line_count
        FROM ukg u
        LEFT JOIN alloc a USING (employee_name)
        ORDER BY u.employee_name
    """)
    return pd.read_sql(sql, engine, params={
        'period': period, 'labor_source': labor_source,
    })


# -------------------------------------------------------------
# Weekly cost reader
# -------------------------------------------------------------

def get_employee_weekly_cost(period: str, employee: str, labor_source: str) -> pd.DataFrame:
    """Direct: bi-weekly split across ISO weeks. Temp: pass-through."""
    if labor_source == 'direct':
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
    elif labor_source == 'temp':
        sql = text("""
            SELECT iso_week,
                   ROUND(SUM(total_labor_cost)::numeric, 2) AS total_labor_cost
            FROM stg_labor_temp
            WHERE accrual_period = :period
              AND employee_name  = :employee
            GROUP BY iso_week
            ORDER BY iso_week
        """)
    else:
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")

    return pd.read_sql(sql, engine, params={'period': period, 'employee': employee})


# -------------------------------------------------------------
# Writers
# -------------------------------------------------------------

def _validate_lines(lines: list[dict]) -> None:
    if not lines:
        raise ValueError("At least one allocation line required.")

    total_pct = sum(float(ln.get('allocation_pct', 0)) for ln in lines)
    if abs(total_pct - 1.0) > 1e-6:
        raise ValueError(f"Allocation pct must sum to 1.00, got {total_pct:.6f}")

    for i, ln in enumerate(lines, start=1):
        lt  = ln.get('line_type')
        pct = float(ln.get('allocation_pct', 0))
        tp  = ln.get('target_program')
        cc  = ln.get('cost_center_name')
        pr  = ln.get('program_restrictions')

        if pct <= 0 or pct > 1:
            raise ValueError(f"Line {i}: allocation_pct must be > 0 and <= 1, got {pct}")
        if lt == 'direct_program':
            if not tp:
                raise ValueError(f"Line {i}: direct_program requires target_program")
            if cc:
                raise ValueError(f"Line {i}: direct_program cannot have cost_center_name")
            if pr:
                raise ValueError(f"Line {i}: direct_program cannot have program_restrictions")
        elif lt == 'cost_center':
            if not cc:
                raise ValueError(f"Line {i}: cost_center requires cost_center_name")
            if tp:
                raise ValueError(f"Line {i}: cost_center cannot have target_program")
        else:
            raise ValueError(f"Line {i}: invalid line_type: {lt!r}")


def save_employee_allocation(
    period: str, employee: str, labor_source: str,
    role_name: str, lines: list[dict], set_by: str,
) -> None:
    """Replaces existing lines with provided ones in one transaction.
    Always resets reviewed=FALSE on save. Raises ValueError on validation failure."""
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")
    if not role_name or not role_name.strip():
        raise ValueError("role_name is required.")
    if not set_by or not set_by.strip():
        raise ValueError("set_by is required.")

    _validate_lines(lines)

    now = datetime.now(timezone.utc).isoformat()

    with engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM stg_labor_employee_allocation
            WHERE accrual_period = :period
              AND employee_name  = :employee
              AND labor_source   = :labor_source
        """), {'period': period, 'employee': employee, 'labor_source': labor_source})

        for i, ln in enumerate(lines, start=1):
            conn.execute(text("""
                INSERT INTO stg_labor_employee_allocation
                    (accrual_period, employee_name, labor_source,
                     role_name, line_order, line_type,
                     target_program, cost_center_name, allocation_pct,
                     program_restrictions,
                     reviewed, reviewed_by, reviewed_at,
                     set_by, set_at)
                VALUES
                    (:period, :employee, :labor_source,
                     :role_name, :line_order, :line_type,
                     :target_program, :cost_center_name, :allocation_pct,
                     :program_restrictions,
                     FALSE, NULL, NULL,
                     :set_by, :set_at)
            """), {
                'period':               period,
                'employee':             employee,
                'labor_source':         labor_source,
                'role_name':            role_name.strip(),
                'line_order':           i,
                'line_type':            ln['line_type'],
                'target_program':       ln.get('target_program'),
                'cost_center_name':     ln.get('cost_center_name'),
                'allocation_pct':       float(ln['allocation_pct']),
                'program_restrictions': ln.get('program_restrictions'),
                'set_by':               set_by.strip(),
                'set_at':               now,
            })

    # Bust caches so the review tab reflects the save
    list_employees_for_review.clear()


def mark_employee_reviewed(
    period: str, employee: str, labor_source: str, reviewed_by: str,
) -> None:
    """Rejects if no lines or if sum != 1.00. Denormalized across all line rows."""
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")
    if not reviewed_by or not reviewed_by.strip():
        raise ValueError("reviewed_by is required.")

    check_sql = text("""
        SELECT COUNT(*)                         AS cnt,
               COALESCE(SUM(allocation_pct), 0) AS total_pct
        FROM stg_labor_employee_allocation
        WHERE accrual_period = :period
          AND employee_name  = :employee
          AND labor_source   = :labor_source
    """)

    with engine.begin() as conn:
        row = conn.execute(check_sql, {
            'period': period, 'employee': employee, 'labor_source': labor_source,
        }).mappings().one()

        if int(row['cnt']) == 0:
            raise ValueError(f"No allocation lines for {employee!r} / {period} / {labor_source}")

        total_pct = float(row['total_pct'])
        if abs(total_pct - 1.0) > 1e-6:
            raise ValueError(
                f"Cannot mark reviewed — allocation sums to {total_pct:.6f}, must equal 1.00"
            )

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(text("""
            UPDATE stg_labor_employee_allocation
            SET reviewed    = TRUE,
                reviewed_by = :reviewed_by,
                reviewed_at = :reviewed_at
            WHERE accrual_period = :period
              AND employee_name  = :employee
              AND labor_source   = :labor_source
        """), {
            'period': period, 'employee': employee, 'labor_source': labor_source,
            'reviewed_by': reviewed_by.strip(), 'reviewed_at': now,
        })

    list_employees_for_review.clear()


def unmark_employee_reviewed(period: str, employee: str, labor_source: str) -> None:
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")

    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE stg_labor_employee_allocation
            SET reviewed = FALSE, reviewed_by = NULL, reviewed_at = NULL
            WHERE accrual_period = :period
              AND employee_name  = :employee
              AND labor_source   = :labor_source
        """), {'period': period, 'employee': employee, 'labor_source': labor_source})

    list_employees_for_review.clear()


def bulk_approve_carried_forward(
    period: str, labor_source: str, reviewer_name: str,
) -> dict:
    """
    For every employee in the period with no current allocation:
      - Pull their most recent prior reviewed allocation
      - Validate role + every program/cost_center still exists and is active
      - Validate lines sum to 1.00
      - On pass: write lines + mark reviewed in one transaction
      - On fail: skip with a reason

    Skips silently any employee who already has a current allocation
    (their state is whatever the user left it).

    Returns:
      {
        'approved':  int,
        'skipped':   list[{employee, reason}],
        'no_prior':  int,
        'current':   int,
      }
    """
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")
    if not reviewer_name or not reviewer_name.strip():
        raise ValueError("reviewer_name is required.")

    employees_df = list_employees_for_review(period, labor_source)
    if employees_df.empty:
        return {'approved': 0, 'skipped': [], 'no_prior': 0, 'current': 0}

    # Validation lookups (active dim values + current period programs)
    roles_df = pd.read_sql(text("""
        SELECT role_name FROM dim_nmf_role WHERE active = TRUE
    """), engine)
    valid_roles = set(roles_df['role_name'].tolist())

    cc_df = pd.read_sql(text("""
        SELECT cost_center_name FROM dim_cost_center WHERE active = TRUE
    """), engine)
    valid_cost_centers = set(cc_df['cost_center_name'].tolist())

    customers_df = pd.read_sql(text("""
        SELECT customer_name FROM dim_customer
        WHERE active = TRUE AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
    """), engine)
    valid_programs = set(customers_df['customer_name'].tolist())

    approved = 0
    skipped  = []
    no_prior = 0
    current  = 0
    now = datetime.now(timezone.utc).isoformat()

    with engine.begin() as conn:
        for _, emp_row in employees_df.iterrows():
            employee = str(emp_row['employee_name'])

            # Skip employees who already have a current allocation
            if int(emp_row['line_count']) > 0:
                current += 1
                continue

            prior = get_prior_period_allocation(period, employee, labor_source)
            if not prior:
                no_prior += 1
                continue

            # Validate role
            role = prior['role_name']
            if role not in valid_roles:
                skipped.append({
                    'employee': employee,
                    'reason': f"Role '{role}' no longer active",
                })
                continue

            # Validate every line
            lines = prior['lines']
            total_pct = sum(float(ln.get('allocation_pct', 0)) for ln in lines)
            if abs(total_pct - 1.0) > 1e-6:
                skipped.append({
                    'employee': employee,
                    'reason': f"Lines sum to {total_pct:.4f}, not 1.00",
                })
                continue

            line_failure = None
            for i, ln in enumerate(lines, start=1):
                lt = ln.get('line_type')
                if lt == 'direct_program':
                    tp = ln.get('target_program')
                    if not tp or tp not in valid_programs:
                        line_failure = f"Line {i}: program '{tp}' no longer active"
                        break
                elif lt == 'cost_center':
                    cc = ln.get('cost_center_name')
                    if not cc or cc not in valid_cost_centers:
                        line_failure = f"Line {i}: cost center '{cc}' no longer active"
                        break
                    pr = ln.get('program_restrictions') or []
                    if pr:
                        try:
                            eligible = set(get_programs_for_cost_center(cc, period))
                        except ValueError:
                            line_failure = f"Line {i}: cost center '{cc}' rejected"
                            break
                        missing = [p for p in pr if p not in eligible]
                        if missing:
                            line_failure = (
                                f"Line {i}: restrictions no longer eligible: "
                                f"{', '.join(missing)}"
                            )
                            break
                else:
                    line_failure = f"Line {i}: invalid line_type {lt!r}"
                    break

            if line_failure:
                skipped.append({'employee': employee, 'reason': line_failure})
                continue

            # All checks passed — write lines + mark reviewed
            for i, ln in enumerate(lines, start=1):
                conn.execute(text("""
                    INSERT INTO stg_labor_employee_allocation
                        (accrual_period, employee_name, labor_source,
                         role_name, line_order, line_type,
                         target_program, cost_center_name, allocation_pct,
                         program_restrictions,
                         reviewed, reviewed_by, reviewed_at,
                         set_by, set_at)
                    VALUES
                        (:period, :employee, :labor_source,
                         :role_name, :line_order, :line_type,
                         :target_program, :cost_center_name, :allocation_pct,
                         :program_restrictions,
                         TRUE, :reviewer, :now,
                         :reviewer, :now)
                """), {
                    'period':               period,
                    'employee':             employee,
                    'labor_source':         labor_source,
                    'role_name':            role,
                    'line_order':           i,
                    'line_type':            ln['line_type'],
                    'target_program':       ln.get('target_program'),
                    'cost_center_name':     ln.get('cost_center_name'),
                    'allocation_pct':       float(ln['allocation_pct']),
                    'program_restrictions': ln.get('program_restrictions'),
                    'reviewer':             reviewer_name.strip(),
                    'now':                  now,
                })
            approved += 1

    list_employees_for_review.clear()

    return {
        'approved': approved,
        'skipped':  skipped,
        'no_prior': no_prior,
        'current':  current,
    }


def bulk_apply_allocation(
    period: str, labor_source: str, employees: list[str],
    role_name: str, lines: list[dict], reviewer_name: str,
) -> dict:
    """
    Apply the same role + lines to multiple employees. One outer transaction
    wraps the whole batch; each employee runs inside a savepoint so a single
    bad employee rolls back independently without poisoning the rest. Existing
    lines for each employee are replaced.

    Returns:
      {
        'applied':  int,   # count actually committed to DB
        'skipped':  list[{employee, reason}],
        'total':    int,
      }
    """
    if labor_source not in ('direct', 'temp'):
        raise ValueError(f"labor_source must be 'direct' or 'temp', got: {labor_source!r}")
    if not role_name or not role_name.strip():
        raise ValueError("role_name is required.")
    if not reviewer_name or not reviewer_name.strip():
        raise ValueError("reviewer_name is required.")
    if not employees:
        return {'applied': 0, 'skipped': [], 'total': 0}

    _validate_lines(lines)

    # Validate role + targets against active dim values
    roles_df = pd.read_sql(
        text("SELECT role_name FROM dim_nmf_role WHERE active = TRUE"), engine,
    )
    if role_name not in set(roles_df['role_name'].tolist()):
        raise ValueError(f"Role '{role_name}' is not active.")

    customers_df = pd.read_sql(text("""
        SELECT customer_name FROM dim_customer
        WHERE active = TRUE AND is_revenue_customer = TRUE AND roll_up_for_cost = FALSE
    """), engine)
    valid_programs = set(customers_df['customer_name'].tolist())

    cc_df = pd.read_sql(
        text("SELECT cost_center_name FROM dim_cost_center WHERE active = TRUE"), engine,
    )
    valid_cost_centers = set(cc_df['cost_center_name'].tolist())

    for i, ln in enumerate(lines, start=1):
        if ln['line_type'] == 'direct_program':
            if ln.get('target_program') not in valid_programs:
                raise ValueError(
                    f"Line {i}: program '{ln.get('target_program')}' is not active."
                )
        elif ln['line_type'] == 'cost_center':
            if ln.get('cost_center_name') not in valid_cost_centers:
                raise ValueError(
                    f"Line {i}: cost center '{ln.get('cost_center_name')}' is not active."
                )

    now = datetime.now(timezone.utc).isoformat()
    applied = 0
    skipped = []

    with engine.begin() as conn:
        for raw_emp in employees:
            employee = str(raw_emp).strip()
            if not employee:
                continue
            try:
                # SAVEPOINT — isolates this employee. A failure inside this
                # block rolls back only this employee's writes (DELETE + any
                # INSERTs that already ran) and leaves the outer transaction
                # healthy, so the next iteration of the loop can proceed
                # normally instead of inheriting a poisoned txn state.
                with conn.begin_nested():
                    conn.execute(text("""
                        DELETE FROM stg_labor_employee_allocation
                        WHERE accrual_period = :period
                          AND employee_name  = :employee
                          AND labor_source   = :labor_source
                    """), {'period': period, 'employee': employee, 'labor_source': labor_source})

                    for i, ln in enumerate(lines, start=1):
                        conn.execute(text("""
                            INSERT INTO stg_labor_employee_allocation
                                (accrual_period, employee_name, labor_source,
                                 role_name, line_order, line_type,
                                 target_program, cost_center_name, allocation_pct,
                                 program_restrictions,
                                 reviewed, reviewed_by, reviewed_at,
                                 set_by, set_at)
                            VALUES
                                (:period, :employee, :labor_source,
                                 :role_name, :line_order, :line_type,
                                 :target_program, :cost_center_name, :allocation_pct,
                                 :program_restrictions,
                                 TRUE, :reviewer, :now,
                                 :reviewer, :now)
                        """), {
                            'period':               period,
                            'employee':             employee,
                            'labor_source':         labor_source,
                            'role_name':            role_name.strip(),
                            'line_order':           i,
                            'line_type':            ln['line_type'],
                            'target_program':       ln.get('target_program'),
                            'cost_center_name':     ln.get('cost_center_name'),
                            'allocation_pct':       float(ln['allocation_pct']),
                            'program_restrictions': ln.get('program_restrictions'),
                            'reviewer':             reviewer_name.strip(),
                            'now':                  now,
                        })
                applied += 1
            except Exception as e:
                skipped.append({'employee': employee, 'reason': str(e)})

    list_employees_for_review.clear()

    return {'applied': applied, 'skipped': skipped, 'total': len(employees)}

# -------------------------------------------------------------
# Commit-state guard
# -------------------------------------------------------------

@st.cache_data(ttl=30, show_spinner=False)
def is_period_committed(period: str) -> bool:
    """Returns True if the period has a row in stg_labor_allocation.

    Used to gate edits in the review, e-comm config, and container unload
    tabs. When committed, mutating the underlying allocation lines or
    config tables would silently invalidate the locked snapshot in
    stg_labor_incurred / stg_labor_applied / stg_wip_* and the
    profitability MV would drift. Editors should be disabled until the
    user explicitly unlocks via the Allocation tab.
    """
    df = pd.read_sql(
        text("""
            SELECT 1
            FROM stg_labor_allocation
            WHERE accrual_period = :period
            LIMIT 1
        """),
        engine,
        params={'period': period},
    )
    return not df.empty