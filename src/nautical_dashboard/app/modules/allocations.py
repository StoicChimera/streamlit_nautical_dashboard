"""
allocations.py
==============
Warehouse Allocation page — Streamlit module.

Architecture:
  - alloc_warehouse_cost_monthly         manual total $ input
  - alloc_warehouse_shared_sqft_monthly  manual sqft for shared buckets
  - alloc_footprint_monthly_input        manual sqft for direct (non-shared) programs
  - stg_warehouse_allocation             committed output (written on Commit)
  - mv_warehouse_by_program              view over committed output → profitability MV

Direct footprints and shared buckets together form the total sqft denominator.
total_wh_cost is divided by total_sqft to get a $/sqft rate. Direct programs
receive (their sqft × $/sqft). Shared buckets receive (bucket sqft × $/sqft)
then further split by activity driver.
"""

import os
from datetime import date, datetime, timezone
from typing import Optional

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from nautical_dashboard.app.modules import auth

from nautical_dashboard.app.modules.allocation_engine import (
    ALL_BUCKETS,
    _consolidate_program,
    copy_sqft_forward,
    get_committed_allocation,
    get_prior_warehouse_wip_applicable,
    get_sqft_inputs,
    get_warehouse_wip,
    get_warehouse_wip_all_periods,
    is_committed,
    save_sqft_inputs,
    seed_sqft_month,
    unlock_allocation,
    write_warehouse_wip_applied,
    _get_office_headcount_split,
    _period,
)

# =====================================================
# Setup
# =====================================================
load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    st.error("Missing SUPABASE_CONN environment variable.")
    st.stop()

engine = create_engine(SUPABASE_CONN, pool_pre_ping=True)

# =====================================================
# Bucket display order and category grouping
# =====================================================
BUCKET_ROWS = [
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

CATEGORY_OPTIONS_DIRECT = ["Storage", "Production", "Dock - Inbound", "Dock - Outbound"]

SECTION_COLOR = "#2E86C1"

# =====================================================
# State helpers
# =====================================================
def _init_state() -> None:
    st.session_state.setdefault("wh_month_calendar", None)
    st.session_state.setdefault("wh_controls", {})
    st.session_state.setdefault("wh_cache_bust", 0)


def _bust_cache_and_rerun() -> None:
    st.session_state["wh_cache_bust"] = int(st.session_state.get("wh_cache_bust", 0)) + 1
    st.cache_data.clear()
    st.rerun()


def _cb() -> int:
    return int(st.session_state.get("wh_cache_bust", 0))


def month_floor(d: date) -> date:
    return d.replace(day=1)


def _dollar(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v)


def _pct(v) -> str:
    try:
        return f"{float(v):.2%}"
    except Exception:
        return str(v)


# =====================================================
# Data access — warehouse cost
# =====================================================
@st.cache_data(ttl=300)
def get_months(cache_bust: int) -> list[date]:
    df = pd.read_sql(
        "SELECT DISTINCT month_start FROM alloc_warehouse_cost_monthly ORDER BY month_start DESC",
        engine,
    )
    return df["month_start"].tolist()


def get_allocated_cost(month_start: date) -> float:
    df = pd.read_sql(
        text("SELECT allocated_warehouse_cost FROM alloc_warehouse_cost_monthly WHERE month_start = :m"),
        engine, params={"m": month_start},
    )
    return float(df["allocated_warehouse_cost"].iloc[0]) if not df.empty else 0.0


def upsert_allocated_cost(month_start: date, cost: float) -> None:
    reviewer = st.session_state.get("wh_reviewer", "")
    now = datetime.now(timezone.utc).isoformat()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO alloc_warehouse_cost_monthly
                    (month_start, allocated_warehouse_cost, updated_by, updated_at)
                VALUES (:m, :c, :by, :at)
                ON CONFLICT (month_start)
                DO UPDATE SET
                    allocated_warehouse_cost = EXCLUDED.allocated_warehouse_cost,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = EXCLUDED.updated_at
            """),
            {"m": month_start, "c": float(cost), "by": reviewer, "at": now},
        )


# =====================================================
# Data access — programs
# =====================================================
@st.cache_data(ttl=300)
def get_programs(cache_bust: int) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            SELECT canonical_key AS program_id, customer_name AS program_name
            FROM dim_customer
            WHERE active = TRUE
              AND is_revenue_customer = TRUE
              AND roll_up_for_cost = FALSE
            ORDER BY customer_name
        """),
        engine,
    )


@st.cache_data(ttl=300)
def get_all_active_groups(cache_bust: int) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT group_id, group_name FROM public.dim_program_group WHERE is_active = true ORDER BY group_name",
        engine,
    )


@st.cache_data(ttl=300)
def get_group_parents(cache_bust: int) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT group_id, group_name FROM public.dim_program_group WHERE is_active = true AND parent_group_id IS NULL ORDER BY group_name",
        engine,
    )


@st.cache_data(ttl=300)
def get_group_children(parent_group_id: int, cache_bust: int) -> pd.DataFrame:
    return pd.read_sql(
        text("SELECT group_id, group_name FROM public.dim_program_group WHERE is_active = true AND parent_group_id = :pid ORDER BY group_name"),
        engine, params={"pid": int(parent_group_id)},
    )


@st.cache_data(ttl=300)
def get_primary_groups_all(cache_bust: int) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT p.program_id, p.program_name, g.group_name AS primary_group_name
        FROM public.dim_program p
        LEFT JOIN public.map_program_group mpg ON mpg.program_id = p.program_id AND mpg.active = true AND mpg.is_primary = true
        LEFT JOIN public.dim_program_group g ON g.group_id = mpg.group_id
        WHERE p.active = true ORDER BY p.program_name
        """,
        engine,
    )


def set_program_primary_group(program_id: str, group_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE public.map_program_group SET active = false WHERE program_id = :pid AND is_primary = true AND active = true"), {"pid": str(program_id)})
        conn.execute(text("INSERT INTO public.map_program_group (program_id, group_id, is_primary, active) VALUES (:pid, :gid, true, true) ON CONFLICT (program_id, group_id) DO UPDATE SET is_primary = true, active = true"), {"pid": str(program_id), "gid": int(group_id)})


def clear_program_primary_group(program_id: str) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE public.map_program_group SET active = false WHERE program_id = :pid AND is_primary = true AND active = true"), {"pid": str(program_id)})


@st.cache_data(ttl=300)
def get_orphan_programs(month_start: date, cache_bust: int) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            WITH mapped AS (
                SELECT DISTINCT revenue_customer_rollup FROM dim_program
                WHERE revenue_customer_rollup IS NOT NULL AND active = true
            )
            SELECT r.customer_rollup, r.revenue
            FROM v_revenue_monthly_by_customer_rollup r
            LEFT JOIN mapped m ON m.revenue_customer_rollup = r.customer_rollup
            WHERE r.month_start = :m AND m.revenue_customer_rollup IS NULL AND r.revenue <> 0
            ORDER BY r.revenue DESC
        """),
        engine, params={"m": month_start},
    )


def ensure_dim_program_rollup_unique_index() -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_program_revenue_rollup ON public.dim_program (revenue_customer_rollup) WHERE revenue_customer_rollup IS NOT NULL"))


def upsert_programs_return_ids(df_new: pd.DataFrame) -> pd.DataFrame:
    ensure_dim_program_rollup_unique_index()
    df = df_new.copy()
    df = df[df["revenue_customer_rollup"].notna()].copy()
    df["program_name"] = df["program_name"].fillna("").astype(str).str.strip()
    df["revenue_customer_rollup"] = df["revenue_customer_rollup"].astype(str).str.strip()
    df["active"] = df.get("active", True).fillna(True).astype(bool)
    df = df[(df["program_name"] != "") & (df["revenue_customer_rollup"] != "")]
    df = df.drop_duplicates(subset=["revenue_customer_rollup"])
    if df.empty:
        return pd.DataFrame(columns=["program_id", "revenue_customer_rollup"])
    params = {}
    parts = []
    for i, r in enumerate(df.to_dict("records")):
        params[f"name_{i}"] = r["program_name"]
        params[f"rollup_{i}"] = r["revenue_customer_rollup"]
        params[f"active_{i}"] = bool(r["active"])
        parts.append(f"(:name_{i}, :rollup_{i}, :active_{i})")
    stmt = text(f"""
        WITH incoming(program_name, revenue_customer_rollup, active) AS (VALUES {",".join(parts)})
        INSERT INTO public.dim_program (program_name, revenue_customer_rollup, active)
        SELECT program_name, revenue_customer_rollup, active FROM incoming
        ON CONFLICT (revenue_customer_rollup)
        DO UPDATE SET program_name = EXCLUDED.program_name, active = EXCLUDED.active, updated_at = now()
        RETURNING program_id, revenue_customer_rollup
    """)
    with engine.begin() as conn:
        res = conn.execute(stmt, params)
        return pd.DataFrame(res.mappings().all())


# =====================================================
# Data access — direct footprints
# =====================================================
@st.cache_data(ttl=300)
def get_footprint_rows(month_start: date, cache_bust: int) -> pd.DataFrame:
    df = pd.read_sql(
        text("""
            SELECT
                a.row_id,
                COALESCE(c.customer_name, a.customer_canonical_key) AS program_name,
                a.customer_canonical_key AS program_id,
                CASE a.category::text
                    WHEN 'storage'       THEN 'Storage'
                    WHEN 'production'    THEN 'Production' 
                    WHEN 'dock_inbound'  THEN 'Dock - Inbound'
                    WHEN 'dock_outbound' THEN 'Dock - Outbound'
                    ELSE a.category::text
                END AS category,
                a.sqft
            FROM alloc_footprint_monthly_input a
            LEFT JOIN dim_customer c
                ON c.canonical_key = a.customer_canonical_key
            WHERE a.month_start = :m
              AND a.is_active = true
            ORDER BY program_name, a.category
        """),
        engine,
        params={"m": month_start},
    )
    return df


def save_footprint_changes(month_start: date, df: pd.DataFrame) -> None:
    d = df.copy()
    d = d[d["program_id"].notna()].copy()
    d["sqft"] = pd.to_numeric(d.get("sqft", 0), errors="coerce").fillna(0.0)
    d["category"] = d.get("category", "Storage").fillna("Storage").astype(str).str.strip()
    d = d[d["category"].isin(CATEGORY_OPTIONS_DIRECT)].copy()
    rows = [
        {"key": r["program_id"], "cat": str(r["category"]), "sqft": float(r["sqft"])}
        for _, r in d.iterrows()
    ]
    with engine.begin() as conn:
        if not rows:
            conn.execute(
                text("UPDATE public.alloc_footprint_monthly_input SET is_active = false, updated_at = now() WHERE month_start = :m AND is_active = true"),
                {"m": month_start},
            )
            return
        params = {"m": month_start}
        values = []
        for i, r in enumerate(rows):
            params[f"key_{i}"] = r["key"]
            params[f"cat_{i}"] = r["cat"]
            params[f"sqft_{i}"] = r["sqft"]
            values.append(f"(:key_{i}, :cat_{i}, :sqft_{i})")
        stmt = text(f"""
            WITH incoming(customer_canonical_key, category, sqft) AS (VALUES {",".join(values)}),
            deactivated AS (
                UPDATE public.alloc_footprint_monthly_input a
                SET is_active = false, updated_at = now()
                WHERE a.month_start = :m AND a.is_active = true
                  AND (a.customer_canonical_key, a.category::text) NOT IN (
                      SELECT customer_canonical_key, category FROM incoming
                  )
                RETURNING 1
            )
            INSERT INTO public.alloc_footprint_monthly_input
                (month_start, customer_canonical_key, category, shared_space, sqft, is_active)
            SELECT :m, customer_canonical_key, 
                CASE category
                    WHEN 'Storage'       THEN 'storage'
                    WHEN 'Production'    THEN 'production'
                    WHEN 'Dock - Inbound'  THEN 'dock_inbound'
                    WHEN 'Dock - Outbound' THEN 'dock_outbound'
                END::warehouse_alloc_category,
                false, sqft, true
            FROM incoming
            ON CONFLICT (month_start, customer_canonical_key, category)
            WHERE is_active = true
            DO UPDATE SET sqft = EXCLUDED.sqft, updated_at = now(), is_active = true
        """)
        conn.execute(stmt, params)


def copy_footprint_forward(prev_month: date, new_month: date) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO public.alloc_footprint_monthly_input
                (month_start, customer_canonical_key, category, shared_space, sqft, is_active)
            SELECT :new_month, customer_canonical_key, category, shared_space, sqft, true
            FROM public.alloc_footprint_monthly_input
            WHERE month_start = :prev_month AND is_active = true
            ON CONFLICT (month_start, customer_canonical_key, category)
            WHERE is_active = true
            DO UPDATE SET sqft = EXCLUDED.sqft, updated_at = now(), is_active = true
        """), {"prev_month": prev_month, "new_month": new_month})


def clear_footprint_rows(month_start: date) -> None:
    with engine.begin() as conn:
        conn.execute(text("UPDATE public.alloc_footprint_monthly_input SET is_active = false, updated_at = now() WHERE month_start = :m AND is_active = true"), {"m": month_start})


# =====================================================
# Direct footprint commit helper
# Computes allocation rows for direct (non-shared) programs.
# =====================================================
def _compute_direct_footprint_rows(
    month_start: date,
    total_sqft: float,
    total_wh_cost: float,
    committed_by: str,
    committed_at: str,
) -> list[dict]:
    fp = get_footprint_rows(month_start, cache_bust=_cb())
    if fp.empty or total_sqft == 0:
        return []
    rows = []
    for _, r in fp.iterrows():
        sqft   = float(r["sqft"])
        pct    = round(sqft / total_sqft, 6) if total_sqft > 0 else 0.0
        amount = round(total_wh_cost * pct, 2)
        rows.append({
            "month_start":       month_start,
            "customer_program":  _consolidate_program(str(r["program_name"])),
            "program_bucket":    f"Direct - {r['category']}",
            "category":          str(r["category"]),
            "cost_type":         "cogs",
            "driver_type":       "Direct Sqft",
            "driver_value":      sqft,
            "total_driver":      total_sqft,
            "allocation_pct":    pct,
            "bucket_sqft":       sqft,
            "total_sqft":        total_sqft,
            "sqft_pct":          pct,
            "total_wh_cost":     total_wh_cost,
            "allocation_amount": amount,
            "committed_by":      committed_by,
            "committed_at":      committed_at,
        })
    return rows


# =====================================================
# Section renderers
# =====================================================

def _render_warehouse_cost(month_start: date) -> float:
    st.markdown(f'<h3 style="color:{SECTION_COLOR};">Warehouse Cost</h3>', unsafe_allow_html=True)
    current = get_allocated_cost(month_start)
    new_cost = st.number_input(
        "Total allocated warehouse cost ($)",
        min_value=0.0, value=float(current), step=1000.0, format="%.2f",
        key="wh_cost_input",
    )
    col_a, col_b = st.columns([1, 4])
    with col_a:
        if st.button("Save cost", key="btn_save_wh_cost"):
            upsert_allocated_cost(month_start, new_cost)
            st.success("Saved.")
            _bust_cache_and_rerun()
    with col_b:
        st.caption("This total is divided across all sqft (shared buckets + direct footprints) to produce a $/sqft rate.")

    # Last saved metadata
    meta = pd.read_sql(
        text("SELECT updated_by, updated_at FROM alloc_warehouse_cost_monthly WHERE month_start = :m"),
        engine, params={"m": month_start},
    )
    if not meta.empty and pd.notna(meta["updated_by"].iloc[0]):
        st.caption(f"Last saved by {meta['updated_by'].iloc[0]} at {meta['updated_at'].iloc[0]}")

    return float(new_cost)


def _render_shared_sqft(month_start: date, months: list[date]) -> None:
    st.markdown(f'<h3 style="color:{SECTION_COLOR};">Shared Space Sqft Inputs</h3>', unsafe_allow_html=True)
    st.caption(
        "Enter the total sqft for each shared space bucket. "
        "The compute engine will allocate each bucket's cost to programs using its activity driver. "
        "Use Initialize to create a blank grid for a new month."
    )

    col_init, col_copy, col_spacer = st.columns([1, 2, 4])
    with col_init:
        if st.button("Initialize grid", key="btn_init_sqft"):
            seed_sqft_month(month_start)
            st.success("Grid initialized.")
            _bust_cache_and_rerun()
    with col_copy:
        prior = [m for m in months if m < month_start]
        if prior:
            prev = st.selectbox("Copy from", prior, key="sqft_copy_from")
            if st.button("Copy forward", key="btn_copy_sqft"):
                copy_sqft_forward(prev, month_start)
                st.success(f"Copied from {prev}.")
                _bust_cache_and_rerun()

    sqft_df = get_sqft_inputs(month_start)

    if sqft_df.empty:
        st.info("No sqft grid for this month. Click Initialize grid above.")
        return

    # Build display df with category grouping
    display = sqft_df[["program_bucket", "category", "total_sqft"]].copy()
    display = display.sort_values(["category", "program_bucket"])

    edited = st.data_editor(
        display,
        num_rows="fixed",
        use_container_width=True,
        hide_index=True,
        column_config={
            "program_bucket": st.column_config.TextColumn("Bucket", disabled=True),
            "category":       st.column_config.TextColumn("Category", disabled=True),
            "total_sqft":     st.column_config.NumberColumn("Sq Ft", min_value=0.0, step=10.0),
        },
        key="sqft_editor",
    )

    # Subtotals by category
    subt = edited.groupby("category", as_index=False)["total_sqft"].sum().sort_values("total_sqft", ascending=False)
    total = float(edited["total_sqft"].sum())
    col1, col2, col3, col4, col5 = st.columns(5)
    metrics = subt.set_index("category")["total_sqft"].to_dict()
    col1.metric("Storage",        f"{metrics.get('Storage', 0):,.0f} sqft")
    col2.metric("Production",     f"{metrics.get('Production', 0):,.0f} sqft")
    col3.metric("Dock - Inbound", f"{metrics.get('Dock - Inbound', 0):,.0f} sqft")
    col4.metric("Dock - Outbound",f"{metrics.get('Dock - Outbound', 0):,.0f} sqft")
    col5.metric("Shared",         f"{metrics.get('Shared', 0):,.0f} sqft")
    st.caption(f"Shared total: {total:,.0f} sqft")

    if not sqft_df.empty and sqft_df["updated_by"].notna().any():
        last_row = sqft_df.dropna(subset=["updated_by"]).sort_values("updated_at", ascending=False).iloc[0]
        st.caption(f"Last saved by {last_row['updated_by']} at {last_row['updated_at']}")

    reviewer = st.session_state.get("wh_reviewer", "")
    if st.button("Save sqft", key="btn_save_sqft"):
        if not reviewer:
            st.warning("Enter your name in the Reviewer field above before saving.")
        else:
            rows = [{"program_bucket": r["program_bucket"], "category": r["category"], "total_sqft": float(r["total_sqft"])} for _, r in edited.iterrows()]
            save_sqft_inputs(month_start, rows, reviewer)
            st.success("Sqft saved.")
            _bust_cache_and_rerun()


def _render_direct_footprints(month_start: date, months: list[date]) -> None:
    st.markdown(f'<h3 style="color:{SECTION_COLOR};">Direct Footprints (Non-Shared)</h3>', unsafe_allow_html=True)
    st.caption(
        "Programs with dedicated, non-shared warehouse space. "
        "Each program receives (its sqft / total sqft) of total warehouse cost directly. "
        "Reviewed quarterly, confirmed monthly."
    )

    programs_df  = get_programs(cache_bust=_cb())
    footprint_df = get_footprint_rows(month_start, cache_bust=_cb())

    if footprint_df.empty:
        footprint_df = pd.DataFrame(columns=["program_name", "program_id", "category", "sqft"])

    edited = st.data_editor(
        footprint_df[["program_name", "program_id", "category", "sqft"]],
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
        column_config={
            "program_name": st.column_config.SelectboxColumn("Program", options=programs_df["program_name"].tolist(), required=True),
            "program_id":   None,
            "category":     st.column_config.SelectboxColumn("Category", options=CATEGORY_OPTIONS_DIRECT, required=True),
            "sqft":         st.column_config.NumberColumn("Sq Ft", min_value=0.0, step=10.0),
        },
        key="direct_footprint_editor",
    )

    name_to_id = dict(zip(programs_df["program_name"], programs_df["program_id"]))
    edited["program_id"] = edited["program_name"].map(name_to_id)

    direct_total = float(edited["sqft"].fillna(0).sum())
    st.caption(f"Direct footprint total: {direct_total:,.0f} sqft")

    col_save, col_clear, col_copy = st.columns([1, 1, 2])
    reviewer = st.session_state.get("wh_reviewer", "")
    with col_save:
        if st.button("Save footprints", key="btn_save_footprints"):
            if not reviewer:
                st.warning("Enter your name above.")
            elif edited["program_id"].isna().any():
                st.error("All rows must have a valid program selected.")
            else:
                save_footprint_changes(month_start, edited)
                st.success("Saved.")
                _bust_cache_and_rerun()
    with col_clear:
        if st.button("Clear month", key="btn_clear_footprints"):
            clear_footprint_rows(month_start)
            st.success("Cleared.")
            _bust_cache_and_rerun()
    with col_copy:
        prior = [m for m in months if m < month_start]
        if prior:
            prev = st.selectbox("Copy from month", prior, key="fp_copy_from")
            if st.button("Copy forward", key="btn_copy_fp"):
                copy_footprint_forward(prev, month_start)
                st.success(f"Copied from {prev}.")
                _bust_cache_and_rerun()


def _render_office_headcount(month_start: date) -> tuple[float, float]:
    """Displays headcount split and returns (ops_pct, sga_pct)."""
    period = _period(month_start)
    ops_pct, sga_pct = _get_office_headcount_split(period)

    st.markdown(f'<h4 style="color:{SECTION_COLOR};">Office/Inventory Headcount Split</h4>', unsafe_allow_html=True)
    st.caption(
        "Office/Inventory shared space cost is split between Ops (COGS) and SGA based on "
        "headcount from approved labor for the period. If labor has not been committed yet, defaults to 50/50."
    )

    col1, col2, col3 = st.columns(3)
    col1.metric("Ops share (COGS)", f"{ops_pct:.1%}")
    col2.metric("SGA share (SGA)",  f"{sga_pct:.1%}")
    col3.caption("Source: stg_labor_direct_hire reviewed rows")
    return ops_pct, sga_pct


def _render_commit_section(month_start: date, total_wh_cost: float, reviewer: str) -> None:
    st.markdown(f'<h3 style="color:{SECTION_COLOR};">Commit Allocation</h3>', unsafe_allow_html=True)

    # Sqft summary before commit
    sqft_df      = get_sqft_inputs(month_start)
    footprint_df = get_footprint_rows(month_start, cache_bust=_cb())
    shared_sqft  = float(sqft_df["total_sqft"].sum()) if not sqft_df.empty else 0.0
    direct_sqft  = float(footprint_df["sqft"].sum()) if not footprint_df.empty else 0.0
    total_sqft   = shared_sqft + direct_sqft

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total WH Cost",   _dollar(total_wh_cost))
    col2.metric("Shared Sqft",     f"{shared_sqft:,.0f}")
    col3.metric("Direct Sqft",     f"{direct_sqft:,.0f}")
    col4.metric("Total Sqft",      f"{total_sqft:,.0f}")

    if total_sqft > 0 and total_wh_cost > 0:
        st.caption(f"Implied rate: {_dollar(total_wh_cost / total_sqft)}/sqft")

    # Guards
    if total_wh_cost == 0:
        st.warning("Warehouse cost is zero. Enter a cost above before committing.")
        return
    if total_sqft == 0:
        st.warning("Total sqft is zero. Enter sqft values before committing.")
        return
    if shared_sqft == 0:
        st.warning("No shared space sqft entered. Initialize and fill the shared space grid.")
        return

    _render_office_headcount(month_start)

    st.markdown("")
    can_commit = auth.has_role("admin", "controller")
    if not can_commit:
        st.info(
            "Committing warehouse allocation requires admin or controller role. "
            f"Your current role is `{auth.current_user()['role']}`."
        )

    st.markdown("")
    commit_col, _ = st.columns([2, 6])
    with commit_col:
        if st.button(
            "Commit Warehouse Allocation",
            key="btn_commit_wh",
            type="primary",
            use_container_width=True,
            disabled=not can_commit,
        ):
            if not reviewer:
                st.warning("Enter your name in the Reviewer field above before committing.")
                return

            committed_at = datetime.now(timezone.utc).isoformat()

            with st.spinner("Computing allocation..."):
                from app.modules.allocation_engine import compute_warehouse_allocation as _compute
                shared_rows, diagnostics = _compute(month_start, reviewer, total_sqft_override=total_sqft)

                if "error" in diagnostics:
                    st.error(diagnostics["error"])
                    return

                # Direct footprint rows
                direct_rows = _compute_direct_footprint_rows(
                    month_start, total_sqft, total_wh_cost, reviewer, committed_at
                )

                all_rows = shared_rows + direct_rows

                # Write to stg_warehouse_allocation
                with engine.begin() as conn:
                    conn.execute(text("DELETE FROM stg_warehouse_allocation WHERE month_start = :m"), {"m": month_start})
                    for r in all_rows:
                        conn.execute(text("""
                            INSERT INTO stg_warehouse_allocation (
                                month_start, customer_program, program_bucket, category,
                                cost_type, driver_type, driver_value, total_driver,
                                allocation_pct, bucket_sqft, total_sqft, sqft_pct,
                                total_wh_cost, allocation_amount, committed_by, committed_at
                            ) VALUES (
                                :month_start, :customer_program, :program_bucket, :category,
                                :cost_type, :driver_type, :driver_value, :total_driver,
                                :allocation_pct, :bucket_sqft, :total_sqft, :sqft_pct,
                                :total_wh_cost, :allocation_amount, :committed_by, :committed_at
                            )
                        """), {**r, "committed_at": committed_at})

            st.success(f"Committed {len(all_rows)} rows.")
            _bust_cache_and_rerun()


def _render_committed_results(month_start: date) -> None:
    st.markdown(f'<h3 style="color:{SECTION_COLOR};">Committed Results</h3>', unsafe_allow_html=True)

    df = get_committed_allocation(month_start)
    if df.empty:
        st.info("No committed allocation for this month.")
        return

    # Header metrics
    total_cogs = float(df[df["cost_type"] == "cogs"]["allocation_amount"].sum())
    total_sga  = float(df[df["cost_type"] == "sga"]["allocation_amount"].sum())
    total      = total_cogs + total_sga
    committed_by = str(df["committed_by"].iloc[0]) if "committed_by" in df.columns else ""
    committed_at = str(df["committed_at"].iloc[0]) if "committed_at" in df.columns else ""

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Allocated", _dollar(total))
    col2.metric("COGS (applied_wh)", _dollar(total_cogs))
    col3.metric("SGA (applied_wh_sga)", _dollar(total_sga))
    st.caption(f"Committed by {committed_by} at {committed_at}")

    # Category subtotals
    st.markdown("#### By Category")
    cat_sub = df.groupby(["category", "cost_type"], as_index=False)["allocation_amount"].sum()
    cat_sub["allocation_amount"] = cat_sub["allocation_amount"].map(_dollar)
    st.dataframe(
        cat_sub.rename(columns={"category": "Category", "cost_type": "Cost Type", "allocation_amount": "Allocated"}),
        use_container_width=True, hide_index=True,
    )

    # Bucket subtotals
    st.markdown("#### By Bucket")
    bucket_sub = df.groupby(["program_bucket", "category", "cost_type"], as_index=False).agg(
        programs=("customer_program", "nunique"),
        allocation_amount=("allocation_amount", "sum"),
    )
    bucket_sub["allocation_amount"] = bucket_sub["allocation_amount"].map(_dollar)
    st.dataframe(
        bucket_sub.rename(columns={
            "program_bucket": "Bucket", "category": "Category",
            "cost_type": "Cost Type", "programs": "Programs",
            "allocation_amount": "Allocated",
        }),
        use_container_width=True, hide_index=True,
    )

    # Program detail
    with st.expander("Program detail", expanded=False):
        detail = df.copy()
        detail["allocation_amount"] = detail["allocation_amount"].map(_dollar)
        detail["allocation_pct"]    = detail["allocation_pct"].map(_pct)
        detail["driver_value"]      = detail["driver_value"].map(lambda x: f"{float(x):,.2f}" if pd.notna(x) else "")
        st.dataframe(
            detail[[
                "customer_program", "program_bucket", "category", "cost_type",
                "driver_type", "driver_value", "allocation_pct", "allocation_amount",
            ]].rename(columns={
                "customer_program": "Program",
                "program_bucket":   "Bucket",
                "category":         "Category",
                "cost_type":        "Cost Type",
                "driver_type":      "Driver",
                "driver_value":     "Driver Value",
                "allocation_pct":   "Alloc %",
                "allocation_amount":"Allocated",
            }),
            use_container_width=True, hide_index=True,
        )

    # SGA callout
    sga_rows = df[df["cost_type"] == "sga"].copy()
    if not sga_rows.empty:
        with st.expander("SGA warehouse cost detail (flows below GP line)", expanded=False):
            st.caption(
                "These amounts are included in applied_wh_sga in mv_warehouse_by_program "
                "and fold into applied_sga in mv_program_profitability. "
                "They reduce net profit but not gross profit."
            )
            sga_display = sga_rows.groupby(["program_bucket", "customer_program"], as_index=False)["allocation_amount"].sum()
            sga_display["allocation_amount"] = sga_display["allocation_amount"].map(_dollar)
            st.dataframe(
                sga_display.rename(columns={"program_bucket": "Bucket", "customer_program": "Program", "allocation_amount": "SGA Amount"}),
                use_container_width=True, hide_index=True,
            )

    # Unlock
    period_str = month_start.strftime("%Y-%m")
    applied_count = pd.read_sql(
        text("SELECT COUNT(*) AS n FROM stg_warehouse_wip_applied WHERE accrual_period = :p"),
        engine, params={"p": period_str},
    )["n"].iloc[0]

    unlock_warning = (
        f"This will clear the committed allocation AND {int(applied_count)} "
        f"prior-period WIP application(s) that were applied to {period_str}."
        if applied_count > 0
        else "This will clear the committed allocation."
    )

    with st.expander("Unlock and Recommit", expanded=False):
        st.warning(unlock_warning)
        if st.button("Confirm Unlock", key="btn_unlock_wh_confirm", type="secondary"):
            unlock_allocation(month_start)
            _bust_cache_and_rerun()


def _render_warehouse_wip_tab(month_start: date, reviewer: str) -> None:
    st.markdown(
        f'<h3 style="color:{SECTION_COLOR};">Warehouse WIP</h3>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Warehouse cost allocated to programs with no revenue recognized in the period. "
        "These amounts sit on the balance sheet until revenue is invoiced."
    )

    # =========================================================================
    # PRIOR PERIOD WIP APPLICABLE
    # =========================================================================
    prior_df = get_prior_warehouse_wip_applicable(month_start)

    if not prior_df.empty:
        total_prior = float(prior_df["warehouse_cost"].sum())
        st.warning(
            f"{prior_df['customer_program'].nunique()} program(s) have prior period warehouse WIP "
            f"totaling {_dollar(total_prior)} that now have revenue in "
            f"{month_start:%Y-%m}. Review and apply below."
        )

        with st.expander("Apply Prior Period Warehouse WIP", expanded=True):
            check_key = f"wh_wip_checks_{month_start}"
            if check_key not in st.session_state:
                st.session_state[check_key] = {
                    i: True for i in range(len(prior_df))
                }

            h_chk, h_period, h_program, h_bucket, h_cat, h_cost = st.columns(
                [0.5, 1.5, 2.5, 2, 1.5, 1.5]
            )
            h_chk.markdown("**Apply**")
            h_period.markdown("**Origin Period**")
            h_program.markdown("**Program**")
            h_bucket.markdown("**Bucket**")
            h_cat.markdown("**Category**")
            h_cost.markdown("**Cost**")
            st.markdown("---")

            for i, row in prior_df.iterrows():
                c_chk, c_period, c_program, c_bucket, c_cat, c_cost = st.columns(
                    [0.5, 1.5, 2.5, 2, 1.5, 1.5]
                )
                with c_chk:
                    checked = st.checkbox(
                        label="apply",
                        value=st.session_state[check_key].get(i, True),
                        key=f"wh_wip_chk_{month_start}_{i}",
                        label_visibility="collapsed",
                    )
                    st.session_state[check_key][i] = checked
                c_period.markdown(row["origin_period"])
                c_program.markdown(row["customer_program"])
                c_bucket.markdown(row["program_bucket"])
                c_cat.markdown(row["category"])
                c_cost.markdown(_dollar(row["warehouse_cost"]))

            selected_indices = [
                i for i, checked in st.session_state[check_key].items() if checked
            ]
            selected_total = prior_df.loc[
                prior_df.index.isin(selected_indices), "warehouse_cost"
            ].sum()

            st.markdown("")
            col_apply, col_info = st.columns([2, 5])
            with col_apply:
                if st.button(
                    f"Apply Selected ({len(selected_indices)} rows)",
                    key=f"btn_apply_wh_wip_{month_start}",
                    type="primary",
                    use_container_width=True,
                    disabled=len(selected_indices) == 0,
                ):
                    if not reviewer:
                        st.warning("Enter your name in the Reviewer field above before applying.")
                    else:
                        rows_to_write = prior_df.loc[
                            prior_df.index.isin(selected_indices)
                        ].to_dict("records")
                        write_warehouse_wip_applied(month_start, rows_to_write, reviewer)
                        st.success(f"Applied {_dollar(selected_total)} of prior warehouse WIP to {month_start:%Y-%m}.")
                        st.rerun()
            with col_info:
                st.caption(
                    f"Selected total: {_dollar(selected_total)}  "
                    f"·  Writes to stg_warehouse_wip_applied  "
                    f"·  MV will reflect on next refresh."
                )
    else:
        # Check if there's outstanding WIP at all
        any_outstanding = get_warehouse_wip_all_periods(as_of_month=month_start)
        if not any_outstanding.empty:
            outstanding_total = float(any_outstanding["warehouse_cost"].sum())
            st.info(
                f"Total outstanding warehouse WIP across all periods: {_dollar(outstanding_total)}. "
                f"None of these programs have revenue in {month_start:%Y-%m}, so nothing is "
                "applicable right now. WIP will surface here in future periods when the "
                "corresponding programs invoice."
            )

    st.markdown("---")

    # =========================================================================
    # CURRENT PERIOD WIP
    # =========================================================================
    st.markdown(
    f'<h4 style="color:{SECTION_COLOR};">New WIP Generated — {month_start:%Y-%m}</h4>',
    unsafe_allow_html=True,
    )
    st.caption(
        "Warehouse cost from this period's commit that landed on programs with no "
        "revenue this period. These amounts will sit on the balance sheet until "
        "those programs invoice in a future period."
    )

    if not is_committed(month_start):
        st.info("Warehouse allocation has not been committed for this period. Commit first to see WIP.")
        return

    wip_df = get_warehouse_wip(month_start)

    if wip_df.empty:
        st.success("No warehouse WIP for this period — all allocated programs have matching revenue.")
    else:
        total_wip = float(wip_df["warehouse_cost"].sum())
        w1, w2, w3 = st.columns(3)
        w1.metric("Total Warehouse WIP", _dollar(total_wip))
        w2.metric("Programs", wip_df["customer_program"].nunique())
        w3.metric("Buckets", wip_df["program_bucket"].nunique())

        display = wip_df.copy()
        display["warehouse_cost"] = display["warehouse_cost"].map(_dollar)
        st.dataframe(
            display.rename(columns={
                "customer_program": "Program",
                "program_bucket":   "Bucket",
                "category":         "Category",
                "cost_type":        "Cost Type",
                "warehouse_cost":   "WIP Cost",
            }),
            use_container_width=True,
            hide_index=True,
        )

    # =========================================================================
    # ALL PERIODS OUTSTANDING
    # =========================================================================
    st.markdown(
    f'<h4 style="color:{SECTION_COLOR};">Outstanding Warehouse WIP Balance — All Periods</h4>',
    unsafe_allow_html=True,
    )
    st.caption(
        "Total unapplied warehouse WIP across all periods. "
        "A row stays here until its program has revenue in some future period, "
        "at which point it surfaces above as applicable to that period."
    )

    all_wip = get_warehouse_wip_all_periods(as_of_month=month_start)
    if all_wip.empty:
        st.info("No outstanding warehouse WIP across any period.")
        return

    total_all = float(all_wip["warehouse_cost"].sum())
    st.metric("Total Outstanding Warehouse WIP", _dollar(total_all))
    st.markdown("")

    display_all = all_wip.copy()
    display_all["warehouse_cost"] = display_all["warehouse_cost"].map(_dollar)
    st.dataframe(
        display_all.rename(columns={
            "accrual_period":   "Period",
            "customer_program": "Program",
            "program_bucket":   "Bucket",
            "category":         "Category",
            "warehouse_cost":   "WIP Cost",
        }),
        use_container_width=True,
        hide_index=True,
    )

# =====================================================
# Main render
# =====================================================
def render():
    _init_state()
    st.markdown("## Warehouse Allocation")

    months = get_months(cache_bust=_cb())
    default_month = months[0] if months else month_floor(date.today())

    if st.session_state["wh_month_calendar"] is None:
        st.session_state["wh_month_calendar"] = default_month

    user = auth.current_user()
    st.session_state["wh_reviewer"] = user["name"]   # back-compat for nested functions

    col_month, col_reviewer, col_spacer = st.columns([2, 2, 4])
    with col_month:
        picked = st.date_input("Allocation month", value=st.session_state["wh_month_calendar"], key="wh_month_picker")
        month_start = month_floor(picked)
        st.session_state["wh_month_calendar"] = month_start
        st.session_state["wh_controls"]["month_start"] = month_start
    with col_reviewer:
        st.markdown("**Reviewer**")
        st.markdown(f"{user['name']}  ·  `{user['role']}`")

    reviewer = user["name"]

    st.markdown("---")

    # Committed state banner
    committed = is_committed(month_start)
    if committed:
        st.success(f"Allocation committed for {month_start:%Y-%m}. Scroll to Committed Results to review or unlock.")
    else:
        st.warning(f"Allocation not yet committed for {month_start:%Y-%m}.")

    # Warehouse cost
    st.markdown("---")
    total_wh_cost = _render_warehouse_cost(month_start)

    st.markdown("---")

    tab_setup, tab_results, tab_wip = st.tabs([
        "Setup",
        "Committed Results",
        "Warehouse WIP",
    ])

    with tab_setup:
        if not committed:
            _render_shared_sqft(month_start, months)
            st.markdown("---")
            _render_direct_footprints(month_start, months)
            st.markdown("---")
            _render_commit_section(month_start, total_wh_cost, reviewer)
        else:
            st.info(
                f"Allocation committed for {month_start:%Y-%m}. "
                "Go to Committed Results to review or unlock."
            )

    with tab_results:
        if committed:
            _render_committed_results(month_start)
        else:
            st.info("No committed allocation for this period yet.")

    with tab_wip:
        _render_warehouse_wip_tab(month_start, reviewer)


if __name__ == "__main__":
    try:
        render()
    except SQLAlchemyError as e:
        st.error(f"Database error: {e}")