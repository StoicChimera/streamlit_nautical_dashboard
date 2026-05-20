from __future__ import annotations

import os
from typing import Optional

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
CONN_STRING = os.getenv("SUPABASE_CONN")

LBS_TO_KG = 0.453592


@st.cache_resource
def get_engine():
    return create_engine(CONN_STRING)


# =============================================================
# Helpers
# =============================================================

def _interpolate_weight(size: float, specs_df: pd.DataFrame) -> Optional[float]:
    known = specs_df[specs_df['roll_weight_lbs'].notna()].sort_values('size_numeric')
    if known.empty:
        return None

    left = known[known['size_numeric'] < size]
    right = known[known['size_numeric'] > size]
    if left.empty or right.empty:
        return None

    l = left.iloc[-1]
    r = right.iloc[0]
    l_size, l_wt = float(l['size_numeric']), float(l['roll_weight_lbs'])
    r_size, r_wt = float(r['size_numeric']), float(r['roll_weight_lbs'])
    return l_wt + (r_wt - l_wt) * (size - l_size) / (r_size - l_size)


def _resolve_weight(size: float, specs_df: pd.DataFrame) -> tuple[Optional[float], bool]:
    row = specs_df[specs_df['size_numeric'] == size]
    if not row.empty and pd.notna(row.iloc[0]['roll_weight_lbs']):
        return float(row.iloc[0]['roll_weight_lbs']), False
    interp = _interpolate_weight(size, specs_df)
    if interp is None:
        return None, False
    return interp, True


def _cost_per_roll(weight_lbs: float, cost_per_kg: float) -> float:
    return weight_lbs * LBS_TO_KG * cost_per_kg


# =============================================================
# Data access
# =============================================================

def load_raw_goods(engine, start_date: str, end_date: str) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            WITH revenue_by_invoice AS (
                SELECT
                    customer_full_name                  AS customer,
                    invoice_num,
                    MIN(contract_completion_date::date) AS contract_completion_date,
                    SUM(amount)                         AS total_revenue
                FROM stg_product_service_detail
                WHERE contract_completion_date::date >= :start_date
                  AND contract_completion_date::date <  :end_date
                GROUP BY customer_full_name, invoice_num
            ),
            raw_goods_costs AS (
                SELECT
                    num                          AS invoice_num,
                    SUM(CAST(amount AS NUMERIC)) AS raw_goods_cost
                FROM clean_qbo_transaction_splits_flat
                WHERE LOWER(account_name) LIKE '%raw goods%'
                   OR LOWER(account_name) LIKE '%cost of goods%'
                GROUP BY num
            ),
            combined AS (
                SELECT
                    r.customer,
                    r.invoice_num,
                    r.contract_completion_date,
                    r.total_revenue,
                    COALESCE(c.raw_goods_cost, 0) AS raw_goods_cost
                FROM revenue_by_invoice r
                LEFT JOIN raw_goods_costs c ON r.invoice_num = c.invoice_num
            )
            SELECT *, 0 AS sort_order FROM combined
            UNION ALL
            SELECT
                'TOTAL' AS customer, NULL, NULL,
                SUM(total_revenue), SUM(raw_goods_cost), 1
            FROM combined
            ORDER BY sort_order, customer, invoice_num
        """),
        engine,
        params={"start_date": start_date, "end_date": end_date},
    )


def load_specs(engine) -> pd.DataFrame:
    return pd.read_sql(
        "SELECT size_numeric, roll_weight_lbs, notes FROM dim_bopp_film_specs ORDER BY size_numeric",
        engine,
    )


def upsert_spec(engine, size: float, weight_lbs: Optional[float], notes: str):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO dim_bopp_film_specs (size_numeric, roll_weight_lbs, notes, updated_at)
                VALUES (:size, :wt, :notes, NOW())
                ON CONFLICT (size_numeric) DO UPDATE SET
                    roll_weight_lbs = EXCLUDED.roll_weight_lbs,
                    notes = EXCLUDED.notes,
                    updated_at = NOW()
            """),
            {"size": size, "wt": weight_lbs, "notes": notes or None}
        )


def delete_spec(engine, size: float):
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM dim_bopp_film_specs WHERE size_numeric = :size"),
            {"size": size}
        )


def load_cost(engine) -> float:
    df = pd.read_sql("SELECT cost_per_kg FROM dim_bopp_cost WHERE id = 1", engine)
    return float(df.iloc[0]['cost_per_kg']) if not df.empty else 0.0


def set_cost(engine, cost_per_kg: float, updated_by: str = ''):
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE dim_bopp_cost
                SET cost_per_kg = :cost,
                    updated_at = NOW(),
                    updated_by = NULLIF(:by, '')
                WHERE id = 1
            """),
            {"cost": cost_per_kg, "by": updated_by}
        )


def load_consumption(engine, period: Optional[str] = None) -> pd.DataFrame:
    if period:
        return pd.read_sql(
            text("""
                SELECT * FROM stg_raw_material_consumption
                WHERE period = :period
                ORDER BY created_at DESC
            """),
            engine,
            params={"period": period}
        )
    return pd.read_sql(
        "SELECT * FROM stg_raw_material_consumption ORDER BY period DESC, created_at DESC",
        engine,
    )


def load_wip(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT * FROM stg_raw_material_consumption
        WHERE allocation_type = 'wip'
        ORDER BY (wip_released_period IS NOT NULL),
                 period DESC,
                 customer_program
        """,
        engine,
    )


def load_period_programs(engine, period: str) -> list[str]:
    start = f"{period}-01"
    end = (pd.to_datetime(start) + pd.DateOffset(months=1)).strftime("%Y-%m-%d")
    df = pd.read_sql(
        text("""
            SELECT DISTINCT customer_full_name AS customer_program
            FROM stg_product_service_detail
            WHERE contract_completion_date::date >= :start
              AND contract_completion_date::date <  :end
            ORDER BY customer_full_name
        """),
        engine,
        params={"start": start, "end": end},
    )
    return df['customer_program'].tolist()


def load_all_programs(engine) -> list[str]:
    df = pd.read_sql(
        "SELECT DISTINCT customer_program FROM mv_program_profitability ORDER BY customer_program",
        engine,
    )
    return df['customer_program'].tolist()


def load_parent_map(engine) -> dict[str, str]:
    df = pd.read_sql(
        "SELECT DISTINCT customer_program, customer_parent FROM mv_program_profitability",
        engine,
    )
    return dict(zip(df['customer_program'], df['customer_parent']))


def classify(program: str, period_programs: set[str], program_source: str) -> str:
    if program_source == 'free_text':
        return 'wip'
    if program in period_programs:
        return 'period'
    return 'wip'


def add_consumption(
    engine,
    period: str,
    program: str,
    parent: str,
    size: float,
    rolls: float,
    weight_lbs_snap: float,
    cost_per_kg_snap: float,
    program_source: str,
    period_programs: set[str],
    notes: str = '',
) -> int:
    cost_per_roll_snap = _cost_per_roll(weight_lbs_snap, cost_per_kg_snap)
    total_cost = rolls * cost_per_roll_snap
    allocation_type = classify(program, period_programs, program_source)
    review_status = 'pending_review' if program_source == 'free_text' else 'auto'

    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO stg_raw_material_consumption (
                    period, customer_program, customer_parent,
                    size_numeric, rolls_used,
                    weight_lbs_per_roll_snap, cost_per_kg_snap, cost_per_roll_snap,
                    total_cost, allocation_type, program_source,
                    review_status, notes
                ) VALUES (
                    :period, :program, :parent,
                    :size, :rolls,
                    :wt, :cpk, :cpr,
                    :total, :alloc, :src,
                    :rev, :notes
                )
                RETURNING id
            """),
            {
                "period": period, "program": program, "parent": parent or None,
                "size": size, "rolls": rolls,
                "wt": weight_lbs_snap, "cpk": cost_per_kg_snap, "cpr": cost_per_roll_snap,
                "total": total_cost, "alloc": allocation_type, "src": program_source,
                "rev": review_status, "notes": notes or None
            }
        )
        return result.scalar()


def update_consumption(engine, row_id: int, **fields):
    allowed = {
        'period', 'customer_program', 'customer_parent', 'allocation_type',
        'wip_released_period', 'review_status', 'reviewer_name', 'notes'
    }
    set_clauses = []
    params: dict = {"id": row_id}
    for k, v in fields.items():
        if k not in allowed:
            continue
        set_clauses.append(f"{k} = :{k}")
        params[k] = v
    if not set_clauses:
        return
    set_clauses.append("updated_at = NOW()")
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE stg_raw_material_consumption SET {', '.join(set_clauses)} WHERE id = :id"),
            params
        )


def delete_consumption(engine, row_id: int):
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_raw_material_consumption WHERE id = :id"),
            {"id": row_id}
        )


def find_wip_release_candidates(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        WITH wip_open AS (
            SELECT id, period, customer_program
            FROM stg_raw_material_consumption
            WHERE allocation_type = 'wip'
              AND wip_released_period IS NULL
        )
        SELECT
            wo.id,
            wo.period AS consumption_period,
            wo.customer_program,
            TO_CHAR(MIN(psd.contract_completion_date::date), 'YYYY-MM') AS candidate_release_period
        FROM wip_open wo
        JOIN stg_product_service_detail psd
          ON psd.customer_full_name = wo.customer_program
         AND psd.contract_completion_date::date >= (wo.period || '-01')::date
        GROUP BY wo.id, wo.period, wo.customer_program
        """,
        engine,
    )


def release_wip(engine, row_id: int, release_period: str, reviewer: str = ''):
    with engine.begin() as conn:
        conn.execute(
            text("""
                UPDATE stg_raw_material_consumption
                SET wip_released_period = :rp,
                    review_status = 'reviewed',
                    reviewer_name = COALESCE(NULLIF(:rev, ''), reviewer_name),
                    updated_at = NOW()
                WHERE id = :id
                  AND allocation_type = 'wip'
                  AND wip_released_period IS NULL
            """),
            {"rp": release_period, "rev": reviewer, "id": row_id}
        )


def refresh_mvs(engine):
    with engine.begin() as conn:
        conn.execute(text("REFRESH MATERIALIZED VIEW mv_raw_materials_by_program"))
        conn.execute(text("REFRESH MATERIALIZED VIEW mv_program_profitability"))


def _load_available_periods(engine) -> list[str]:
    """Periods that have any raw goods activity (invoice-based or manual)."""
    df = pd.read_sql(
        text("""
            SELECT DISTINCT period FROM (
                SELECT TO_CHAR(psd.contract_completion_date::date, 'YYYY-MM') AS period
                FROM stg_product_service_detail psd
                JOIN clean_qbo_transaction_splits_flat tx ON tx.num = psd.invoice_num
                WHERE psd.contract_completion_date IS NOT NULL
                  AND (LOWER(tx.account_name) LIKE '%raw goods%'
                       OR LOWER(tx.account_name) LIKE '%cost of goods%')
                UNION
                SELECT period FROM stg_raw_material_consumption
                WHERE period IS NOT NULL
            ) p
            ORDER BY period DESC
        """),
        engine,
    )
    return df["period"].tolist()


# =============================================================
# Tab renderers
# =============================================================

def _render_invoice_detail(engine):
    col_start, col_end = st.columns(2)
    with col_start:
        start_date = st.date_input(
            "Accrual period start",
            value=pd.Timestamp.today().replace(day=1),
            key="raw_start",
        )
    with col_end:
        default_end = pd.Timestamp.today().replace(day=1) + pd.DateOffset(months=1)
        end_date = st.date_input(
            "Accrual period end (1st of NEXT month — exclusive)",
            value=default_end,
            key="raw_end",
        )

    if not st.button("Run", key="btn_raw_run"):
        return

    with st.spinner("Querying..."):
        df = load_raw_goods(engine, str(start_date), str(end_date))

    if df.empty:
        st.warning("No data found for that period.")
        return

    detail = df[df["sort_order"] == 0].drop(columns=["sort_order"])
    total = df[df["sort_order"] == 1].drop(columns=["sort_order", "invoice_num", "contract_completion_date"])

    detail["margin_pct"] = (
        (detail["total_revenue"] - detail["raw_goods_cost"]) / detail["total_revenue"] * 100
    )

    if not total.empty:
        t = total.iloc[0]
        c1, c2, c3 = st.columns(3)
        c1.metric("Total Revenue", f"${t['total_revenue']:,.2f}")
        c2.metric("Total Raw Goods Cost", f"${t['raw_goods_cost']:,.2f}")
        c3.metric("Gross Margin", f"${t['total_revenue'] - t['raw_goods_cost']:,.2f}")

    st.divider()
    st.dataframe(
        detail[["customer", "invoice_num", "contract_completion_date",
                "total_revenue", "raw_goods_cost", "margin_pct"]],
        use_container_width=True,
        hide_index=True,
        column_config={
            "customer":                 st.column_config.TextColumn("Customer"),
            "invoice_num":              st.column_config.TextColumn("Invoice #"),
            "contract_completion_date": st.column_config.DateColumn("Completion Date", format="YYYY-MM-DD"),
            "total_revenue":            st.column_config.NumberColumn("Revenue",        format="$%.2f"),
            "raw_goods_cost":           st.column_config.NumberColumn("Raw Goods Cost", format="$%.2f"),
            "margin_pct":               st.column_config.NumberColumn("Margin %",       format="%.1f%%"),
        },
    )


def _render_film_specs(engine):
    st.subheader("BOPP cost per kg")

    current_cost = load_cost(engine)

    col_cost, col_save_cost = st.columns([3, 1])
    with col_cost:
        new_cost = st.number_input(
            "Cost per kg (USD)",
            value=float(current_cost),
            min_value=0.0,
            step=0.01,
            format="%.4f",
            key="bopp_cost_input",
        )
    with col_save_cost:
        st.markdown("&nbsp;", unsafe_allow_html=True)
        if st.button("Save cost", key="save_bopp_cost", use_container_width=True):
            set_cost(engine, float(new_cost))
            st.success(f"Saved: ${new_cost:.4f}/kg")
            st.rerun()

    st.caption(
        "Snapshotted into each consumption entry at entry time. Updates here "
        "do not retroactively change past consumption."
    )

    st.divider()
    st.subheader("Size to roll weight")

    specs = load_specs(engine)
    cost_for_display = float(new_cost)

    rows = []
    for _, row in specs.iterrows():
        size = float(row['size_numeric'])
        if pd.notna(row['roll_weight_lbs']):
            weight = float(row['roll_weight_lbs'])
            source = 'set'
        else:
            interp = _interpolate_weight(size, specs)
            if interp is None:
                rows.append({
                    'Size': size,
                    'Weight (lbs)': None,
                    'Source': 'NEEDS WEIGHT',
                    'Weight (kg)': None,
                    'Cost/roll': None,
                    'Notes': row['notes'] or '',
                })
                continue
            weight = interp
            source = 'interpolated'

        weight_kg = weight * LBS_TO_KG
        rows.append({
            'Size': size,
            'Weight (lbs)': round(weight, 2),
            'Source': source,
            'Weight (kg)': round(weight_kg, 3),
            'Cost/roll': round(weight_kg * cost_for_display, 4),
            'Notes': row['notes'] or '',
        })

    display_df = pd.DataFrame(rows)
    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            'Size':         st.column_config.NumberColumn('Size', format="%.1f"),
            'Weight (lbs)': st.column_config.NumberColumn('Weight (lbs)', format="%.2f"),
            'Source':       st.column_config.TextColumn('Source', width='small'),
            'Weight (kg)':  st.column_config.NumberColumn('Weight (kg)', format="%.3f"),
            'Cost/roll':    st.column_config.NumberColumn('Cost/roll', format="$%.4f"),
            'Notes':        st.column_config.TextColumn('Notes'),
        },
    )

    needs = display_df[display_df['Source'] == 'NEEDS WEIGHT']
    if not needs.empty:
        st.warning(
            f"{len(needs)} size(s) have no weight and no neighbors to "
            "interpolate from. Add weights for surrounding sizes or enter "
            "a weight for the affected sizes directly below."
        )

    st.divider()
    with st.expander("Add or edit a size", expanded=False):
        with st.form("form_spec_upsert"):
            c1, c2 = st.columns(2)
            with c1:
                spec_size = st.number_input("Size", min_value=0.0, step=0.5, format="%.1f")
            with c2:
                spec_weight = st.number_input(
                    "Weight (lbs)",
                    min_value=0.0,
                    step=0.1,
                    format="%.2f",
                    help="Enter 0 to store NULL (interpolate at use time).",
                )
            spec_notes = st.text_input("Notes (optional)")

            c_save, c_del = st.columns(2)
            saved = c_save.form_submit_button("Save")
            deleted = c_del.form_submit_button("Delete this size", type="secondary")

            if saved:
                wt_to_store = spec_weight if spec_weight > 0 else None
                upsert_spec(engine, spec_size, wt_to_store, spec_notes)
                st.success(
                    f"Saved: size {spec_size} -> "
                    f"{wt_to_store if wt_to_store else 'NULL (interpolate)'}"
                )
                st.rerun()
            if deleted:
                delete_spec(engine, spec_size)
                st.warning(f"Deleted size {spec_size}.")
                st.rerun()


def _render_consumption(engine):
    st.subheader("Consumption period")

    available_periods = _load_available_periods(engine)
    if not available_periods:
        st.warning("No raw goods data found.")
        return

    current_month = pd.Timestamp.today().strftime("%Y-%m")
    default_idx = available_periods.index(current_month) if current_month in available_periods else 0

    period = st.selectbox(
        "Period",
        options=available_periods,
        index=default_idx,
        key="cons_period",
    )

    specs = load_specs(engine)
    cost_per_kg = load_cost(engine)
    period_programs = load_period_programs(engine, period)
    all_programs = load_all_programs(engine)
    parent_map = load_parent_map(engine)

    period_set = set(period_programs)
    wip_candidates = [p for p in all_programs if p not in period_set]

    st.caption(
        f"Cost/kg: ${cost_per_kg:.4f}  ·  "
        f"In-period programs: {len(period_set)}  ·  "
        f"Other known programs: {len(wip_candidates)}"
    )

    st.divider()
    st.subheader("Add entry")

    program_meta: dict = {}
    for p in period_programs:
        program_meta[p] = ('product_service_detail', parent_map.get(p, ''))
    for p in wip_candidates:
        if p not in program_meta:
            program_meta[p] = ('mv_program_profitability', parent_map.get(p, ''))

    OTHER_OPT = "Other..."
    program_options = sorted(program_meta.keys()) + [OTHER_OPT]

    picked = st.selectbox(
        "Program",
        options=program_options,
        key="prog_picker",
        help=(
            "Pick a program from the list. Choose 'Other...' to type "
            "a program name not yet in the system — the entry will "
            "land in WIP for review."
        ),
    )

    other_name = ""
    if picked == OTHER_OPT:
        other_name = st.text_input(
            "Program name",
            placeholder="Type the program name",
            key="other_program_name",
        )

    with st.form("form_consumption_add", clear_on_submit=True):
        c1, c2 = st.columns([1, 2])
        with c1:
            size_options = sorted(specs['size_numeric'].astype(float).tolist())
            picked_size = st.selectbox(
                "Size",
                options=size_options,
                format_func=lambda x: f"{x:.1f}",
            )
        with c2:
            rolls = st.number_input("Rolls used", min_value=0.0, step=0.5, format="%.2f")

        weight_lbs_snap, is_interp = _resolve_weight(picked_size, specs)
        if weight_lbs_snap is None:
            st.error(
                f"Size {picked_size} has no weight and cannot be interpolated. "
                "Add a weight in Film Specs first."
            )
            cost_per_roll_preview = 0.0
            total_preview = 0.0
        else:
            cost_per_roll_preview = _cost_per_roll(weight_lbs_snap, cost_per_kg)
            total_preview = rolls * cost_per_roll_preview
            tag = " (interpolated)" if is_interp else ""
            st.caption(
                f"Weight: {weight_lbs_snap:.2f} lbs/roll{tag}  ·  "
                f"Cost: ${cost_per_roll_preview:.4f}/roll  ·  "
                f"Total: ${total_preview:.2f}"
            )

        notes = st.text_input("Notes (optional)")

        submitted = st.form_submit_button("Save consumption", type="primary")

    if submitted:
        if weight_lbs_snap is None:
            st.error("Cannot save without a resolvable weight.")
        elif rolls <= 0:
            st.error("Enter a positive rolls used value.")
        else:
            if picked == OTHER_OPT:
                if not other_name.strip():
                    st.error("Enter a program name in the 'Program name' field above.")
                    return
                program = other_name.strip()
                parent = ''
                program_source = 'free_text'
            else:
                program = picked
                program_source, parent = program_meta[picked]

            new_id = add_consumption(
                engine, period, program, parent,
                float(picked_size), float(rolls),
                float(weight_lbs_snap), float(cost_per_kg),
                program_source, period_set, notes,
            )
            classification = "period" if program in period_set else "WIP"
            st.success(
                f"Saved [{new_id}]: {rolls:.2f} rolls of size {picked_size:.1f} "
                f"-> {program}  ·  ${total_preview:.2f}  ·  {classification}"
            )
            st.rerun()

    st.divider()
    st.subheader(f"Entries for {period}")

    consumption_df = load_consumption(engine, period)

    if consumption_df.empty:
        st.info("No consumption entries for this period yet.")
    else:
        c1, c2, c3 = st.columns(3)
        c1.metric("Entries", len(consumption_df))
        c2.metric("Total cost", f"${consumption_df['total_cost'].sum():,.2f}")
        c3.metric(
            "Period / WIP",
            f"{(consumption_df['allocation_type'] == 'period').sum()} / "
            f"{(consumption_df['allocation_type'] == 'wip').sum()}",
        )

        display_cols = consumption_df[[
            'id', 'customer_program', 'size_numeric', 'rolls_used',
            'cost_per_roll_snap', 'total_cost', 'allocation_type',
            'program_source', 'review_status', 'notes'
        ]].copy()
        display_cols.columns = [
            'ID', 'Program', 'Size', 'Rolls', 'Cost/roll', 'Total',
            'Type', 'Source', 'Review', 'Notes'
        ]
        st.dataframe(
            display_cols,
            use_container_width=True,
            hide_index=True,
            column_config={
                'ID':        st.column_config.NumberColumn('ID', width='small'),
                'Size':      st.column_config.NumberColumn('Size', format="%.1f"),
                'Rolls':     st.column_config.NumberColumn('Rolls', format="%.2f"),
                'Cost/roll': st.column_config.NumberColumn('Cost/roll', format="$%.4f"),
                'Total':     st.column_config.NumberColumn('Total', format="$%.2f"),
            },
        )

        with st.expander("Edit or delete an entry", expanded=False):
            row_labels = consumption_df.apply(
                lambda r: f"[{r['id']}] {r['customer_program']} | size {r['size_numeric']} | "
                          f"{r['rolls_used']} rolls | ${r['total_cost']:,.2f}",
                axis=1,
            ).tolist()
            sel_label = st.selectbox("Select", row_labels, key="sel_cons_edit")
            sel_idx = row_labels.index(sel_label)
            sel_row = consumption_df.iloc[sel_idx]

            edit_program = st.text_input("Program", value=sel_row['customer_program'])
            edit_notes = st.text_input("Notes", value=sel_row['notes'] or '')

            c_save, c_del = st.columns(2)
            with c_save:
                if st.button("Update", key="btn_cons_update", use_container_width=True):
                    update_consumption(
                        engine, int(sel_row['id']),
                        customer_program=edit_program,
                        notes=edit_notes,
                    )
                    st.success("Updated.")
                    st.rerun()
            with c_del:
                if st.button("Delete", key="btn_cons_delete", type="secondary", use_container_width=True):
                    delete_consumption(engine, int(sel_row['id']))
                    st.warning(f"Deleted [{sel_row['id']}].")
                    st.rerun()

    st.divider()
    if st.button("Refresh MVs", key="refresh_cons_mvs"):
        with st.spinner("Refreshing..."):
            refresh_mvs(engine)
        st.success("MVs refreshed.")
        st.rerun()


def _render_by_program(engine):
    st.subheader("Raw Goods by Program")

    available_periods = _load_available_periods(engine)
    if not available_periods:
        st.warning("No raw goods data found.")
        return

    current_month = pd.Timestamp.today().strftime("%Y-%m")
    default_idx = available_periods.index(current_month) if current_month in available_periods else 0

    period = st.selectbox(
        "Period",
        options=available_periods,
        index=default_idx,
        key="by_program_period",
    )
    period_start = pd.to_datetime(period + "-01")
    period_end = (period_start + pd.DateOffset(months=1)).strftime("%Y-%m-%d")
    period_start_str = period_start.strftime("%Y-%m-%d")

    # Invoice-based: dedupe PSD to one row per (program, invoice) BEFORE joining costs
    invoice_df = pd.read_sql(
        text("""
            WITH invoice_costs AS (
                SELECT
                    num AS invoice_num,
                    description,
                    account_name,
                    SUM(CAST(amount AS NUMERIC)) AS cost
                FROM clean_qbo_transaction_splits_flat
                WHERE LOWER(account_name) LIKE '%raw goods%'
                   OR LOWER(account_name) LIKE '%cost of goods%'
                GROUP BY num, description, account_name
            ),
            invoice_programs AS (
                SELECT
                    COALESCE(
                        a.canonical_name,
                        CASE
                            WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3)) != ''
                                THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3))
                            WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2)) != ''
                                THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2))
                            ELSE TRIM(psd.customer_full_name)
                        END
                    ) AS customer_program,
                    psd.invoice_num,
                    MIN(psd.contract_completion_date::date) AS completion_date
                FROM stg_product_service_detail psd
                LEFT JOIN (
                    SELECT DISTINCT ON (LOWER(alias))
                        LOWER(alias) AS alias_lower,
                        canonical_name,
                        exclude
                    FROM dim_customer_alias
                    WHERE active = TRUE
                    ORDER BY LOWER(alias), canonical_name
                ) a ON a.alias_lower = LOWER(
                    CASE
                        WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3)) != ''
                            THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3))
                        WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2)) != ''
                            THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2))
                        ELSE TRIM(psd.customer_full_name)
                    END
                )
                WHERE psd.contract_completion_date::date >= :start
                  AND psd.contract_completion_date::date <  :end
                  AND COALESCE(a.exclude, FALSE) = FALSE
                GROUP BY 1, 2
            )
            SELECT
                ip.customer_program,
                ip.invoice_num,
                ip.completion_date,
                ic.account_name,
                ic.description,
                ic.cost
            FROM invoice_programs ip
            JOIN invoice_costs ic ON ic.invoice_num = ip.invoice_num
        """),
        engine,
        params={"start": period_start_str, "end": period_end},
    )

    manual_df = pd.read_sql(
        text("""
            SELECT
                customer_program,
                size_numeric,
                rolls_used,
                total_cost,
                allocation_type,
                notes,
                created_at
            FROM stg_raw_material_consumption
            WHERE (
                (allocation_type = 'period' AND period = :period)
                OR (allocation_type = 'wip' AND wip_released_period = :period)
            )
        """),
        engine,
        params={"period": period},
    )

    # Revenue per program for the period — same alias/split logic as invoice_programs
    revenue_df = pd.read_sql(
        text("""
            SELECT
                COALESCE(
                    a.canonical_name,
                    CASE
                        WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3)) != ''
                            THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3))
                        WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2)) != ''
                            THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2))
                        ELSE TRIM(psd.customer_full_name)
                    END
                ) AS customer_program,
                SUM(psd.amount) AS revenue
            FROM stg_product_service_detail psd
            LEFT JOIN (
                SELECT DISTINCT ON (LOWER(alias))
                    LOWER(alias) AS alias_lower,
                    canonical_name,
                    exclude
                FROM dim_customer_alias
                WHERE active = TRUE
                ORDER BY LOWER(alias), canonical_name
            ) a ON a.alias_lower = LOWER(
                CASE
                    WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3)) != ''
                        THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3))
                    WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2)) != ''
                        THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2))
                    ELSE TRIM(psd.customer_full_name)
                END
            )
            WHERE psd.contract_completion_date::date >= :start
              AND psd.contract_completion_date::date <  :end
              AND COALESCE(a.exclude, FALSE) = FALSE
            GROUP BY 1
        """),
        engine,
        params={"start": period_start_str, "end": period_end},
    )

    # PSD line detail per invoice for the period — used in expander
    psd_lines_df = pd.read_sql(
        text("""
            SELECT
                COALESCE(
                    a.canonical_name,
                    CASE
                        WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3)) != ''
                            THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3))
                        WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2)) != ''
                            THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2))
                        ELSE TRIM(psd.customer_full_name)
                    END
                ) AS customer_program,
                psd.invoice_num,
                psd.contract_completion_date::date AS completion_date,
                psd.product_service,
                psd.line_description AS description,
                psd.qty,
                psd.amount AS revenue
            FROM stg_product_service_detail psd
            LEFT JOIN (
                SELECT DISTINCT ON (LOWER(alias))
                    LOWER(alias) AS alias_lower,
                    canonical_name,
                    exclude
                FROM dim_customer_alias
                WHERE active = TRUE
                ORDER BY LOWER(alias), canonical_name
            ) a ON a.alias_lower = LOWER(
                CASE
                    WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3)) != ''
                        THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 3))
                    WHEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2)) != ''
                        THEN TRIM(SPLIT_PART(psd.customer_full_name, ':', 2))
                    ELSE TRIM(psd.customer_full_name)
                END
            )
            WHERE psd.contract_completion_date::date >= :start
              AND psd.contract_completion_date::date <  :end
              AND COALESCE(a.exclude, FALSE) = FALSE
            ORDER BY psd.invoice_num
        """),
        engine,
        params={"start": period_start_str, "end": period_end},
    )

    revenue_map = dict(zip(revenue_df["customer_program"], revenue_df["revenue"])) if not revenue_df.empty else {}
    invoice_total = float(invoice_df["cost"].sum()) if not invoice_df.empty else 0.0
    manual_total  = float(manual_df["total_cost"].sum()) if not manual_df.empty else 0.0
    total         = invoice_total + manual_total

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Raw Goods",     f"${total:,.2f}")
    c2.metric("Invoice-Based (QBO)", f"${invoice_total:,.2f}")
    c3.metric("Manual Consumption", f"${manual_total:,.2f}")
    c4.metric(
        "Programs",
        len(
            set(invoice_df["customer_program"].tolist() if not invoice_df.empty else [])
            | set(manual_df["customer_program"].tolist() if not manual_df.empty else [])
        ),
    )

    if total == 0:
        st.info(f"No raw goods cost found for {period}.")
        return

    by_program = []
    all_programs = set()
    if not invoice_df.empty:
        all_programs.update(invoice_df["customer_program"].dropna().tolist())
    if not manual_df.empty:
        all_programs.update(manual_df["customer_program"].dropna().tolist())

    for prog in all_programs:
        inv_sub = invoice_df[invoice_df["customer_program"] == prog] if not invoice_df.empty else pd.DataFrame()
        man_sub = manual_df[manual_df["customer_program"] == prog] if not manual_df.empty else pd.DataFrame()
        inv_amt = float(inv_sub["cost"].sum()) if not inv_sub.empty else 0.0
        man_amt = float(man_sub["total_cost"].sum()) if not man_sub.empty else 0.0
        cost_total = inv_amt + man_amt
        revenue = float(revenue_map.get(prog, 0.0))
        margin_dollars = revenue - cost_total
        margin_pct = (margin_dollars / revenue * 100) if revenue > 0 else None
        by_program.append({
            "Program":      prog,
            "Revenue":      revenue,
            "Invoice":      inv_amt,
            "Manual":       man_amt,
            "Cost":         cost_total,
            "Margin $":     margin_dollars,
            "Margin %":     margin_pct,
            "Invoices":     inv_sub["invoice_num"].nunique() if not inv_sub.empty else 0,
            "Manual Lines": len(man_sub),
        })

    summary = pd.DataFrame(by_program).sort_values("Cost", ascending=False)

    st.markdown("---")
    st.markdown("#### Summary")
    display = summary.copy()
    for col in ["Revenue", "Invoice", "Manual", "Cost", "Margin $"]:
        display[col] = display[col].map(lambda x: f"${x:,.2f}")
    display["Margin %"] = display["Margin %"].map(
        lambda x: f"{x:.1f}%" if x is not None and pd.notna(x) else "—"
    )
    st.dataframe(display, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("#### Program Detail")

    for _, row in summary.iterrows():
        prog = row["Program"]
        margin_label = (
            f"{row['Margin %']:.1f}%" 
            if row["Margin %"] is not None and pd.notna(row["Margin %"]) 
            else "no rev"
        )
        with st.expander(
            f"**{prog}** — Revenue \\${row['Revenue']:,.2f} · Cost \\${row['Cost']:,.2f} · Margin {margin_label}",
            expanded=False,
        ):
            inv_sub = invoice_df[invoice_df["customer_program"] == prog] if not invoice_df.empty else pd.DataFrame()
            man_sub = manual_df[manual_df["customer_program"] == prog] if not manual_df.empty else pd.DataFrame()
            psd_sub = psd_lines_df[psd_lines_df["customer_program"] == prog] if not psd_lines_df.empty else pd.DataFrame()

            if not psd_sub.empty:
                st.markdown("**Revenue Lines (PSD)**")
                psd_display = psd_sub.copy()
                psd_display["revenue"] = psd_display["revenue"].map(lambda x: f"${float(x):,.2f}")
                psd_display = psd_display.rename(columns={
                    "invoice_num":      "Invoice",
                    "completion_date":  "Completion",
                    "product_service":  "Item",
                    "description":      "Description",
                    "qty":              "Qty",
                    "revenue":          "Revenue",
                })[["Invoice", "Completion", "Item", "Description", "Qty", "Revenue"]]
                st.dataframe(psd_display, use_container_width=True, hide_index=True)

            if not inv_sub.empty:
                st.markdown("**Cost Lines (QBO)**")
                inv_display = inv_sub.copy()
                inv_display["cost"] = inv_display["cost"].map(lambda x: f"${float(x):,.2f}")
                inv_display = inv_display.rename(columns={
                    "invoice_num": "Invoice",
                    "completion_date": "Completion",
                    "account_name": "Account",
                    "description": "Description",
                    "cost": "Cost",
                })[["Invoice", "Completion", "Account", "Description", "Cost"]]
                st.dataframe(inv_display, use_container_width=True, hide_index=True)

            if not man_sub.empty:
                st.markdown("**Manual Consumption (BOPP)**")
                man_display = man_sub.copy()
                man_display["total_cost"] = man_display["total_cost"].map(lambda x: f"${float(x):,.2f}")
                man_display = man_display.rename(columns={
                    "size_numeric": "Size",
                    "rolls_used":   "Rolls",
                    "total_cost":   "Cost",
                    "notes":        "Notes",
                })[["Size", "Rolls", "Cost", "Notes"]]
                st.dataframe(man_display, use_container_width=True, hide_index=True)


def _render_wip(engine):
    st.subheader("WIP Raw Material — Open and Released")
    st.caption(
        "WIP entries are auto-created from the Consumption tab when a BOPP entry "
        "is for a program with no revenue in the period. "
        "Invoice-based raw goods won't appear here as they tie to revenue automatically."
    )

    wip_df = load_wip(engine)
    if wip_df.empty:
        st.info("No WIP raw material entries.")
        return

    open_mask = wip_df['wip_released_period'].isna()
    open_df = wip_df[open_mask].copy()
    released_df = wip_df[~open_mask].copy()

    c1, c2, c3 = st.columns(3)
    c1.metric("Open WIP entries", len(open_df))
    c2.metric("Open WIP $", f"${open_df['total_cost'].sum():,.2f}")
    c3.metric("Released entries", len(released_df))

    st.divider()
    st.markdown("### Open WIP")

    if open_df.empty:
        st.caption("No open WIP entries.")
    else:
        display_open = open_df[[
            'id', 'period', 'customer_program', 'size_numeric',
            'rolls_used', 'total_cost', 'review_status', 'program_source', 'notes'
        ]].copy()
        display_open.columns = [
            'ID', 'Consumed in', 'Program', 'Size', 'Rolls',
            'Total', 'Review', 'Source', 'Notes'
        ]
        st.dataframe(
            display_open,
            use_container_width=True,
            hide_index=True,
            column_config={
                'Size':  st.column_config.NumberColumn('Size', format="%.1f"),
                'Rolls': st.column_config.NumberColumn('Rolls', format="%.2f"),
                'Total': st.column_config.NumberColumn('Total', format="$%.2f"),
            },
        )

        st.markdown("### Release candidates")
        st.caption(
            "Open WIP entries whose program has since appeared in "
            "product_service_detail with a contract_completion_date on or after "
            "the consumption period. The earliest such month is the candidate "
            "release period."
        )

        candidates = find_wip_release_candidates(engine)
        if candidates.empty:
            st.info("No release candidates — no open WIP programs have invoiced yet.")
        else:
            merged = open_df[['id', 'period', 'customer_program', 'total_cost']].merge(
                candidates[['id', 'candidate_release_period']],
                on='id',
                how='inner',
            )
            display_cand = merged.rename(columns={
                'id': 'ID',
                'period': 'Consumed in',
                'customer_program': 'Program',
                'total_cost': 'Total',
                'candidate_release_period': 'Release to',
            })
            st.dataframe(
                display_cand,
                use_container_width=True,
                hide_index=True,
                column_config={
                    'Total': st.column_config.NumberColumn('Total', format="$%.2f"),
                },
            )

            if st.button(
                f"Release all {len(merged)} candidates",
                key="btn_release_all_wip",
                type="primary",
                use_container_width=True,
            ):
                for _, r in merged.iterrows():
                    release_wip(engine, int(r['id']), str(r['candidate_release_period']))
                st.success(f"Released {len(merged)} entries.")
                st.rerun()

        with st.expander("Manual override for a specific entry", expanded=False):
            row_labels = open_df.apply(
                lambda r: f"[{r['id']}] {r['period']} | {r['customer_program']} | ${r['total_cost']:,.2f}",
                axis=1,
            ).tolist()
            sel_label = st.selectbox("Entry", row_labels, key="sel_wip_manual")
            sel_idx = row_labels.index(sel_label)
            sel_row = open_df.iloc[sel_idx]

            override_program = st.text_input(
                "Reclassify program (leave empty to keep current)",
                value="",
                key="wip_override_program",
            )
            override_release = st.text_input(
                "Release to period (YYYY-MM, empty = stay open)",
                value="",
                key="wip_override_release",
            )

            c_apply, c_to_period = st.columns(2)
            with c_apply:
                if st.button("Apply overrides", key="btn_wip_apply", use_container_width=True):
                    updates: dict = {}
                    if override_program.strip():
                        updates['customer_program'] = override_program.strip()
                    if override_release.strip():
                        try:
                            pd.to_datetime(override_release.strip() + "-01")
                            updates['wip_released_period'] = override_release.strip()
                            updates['review_status'] = 'reviewed'
                        except Exception:
                            st.error("Invalid release period.")
                            return
                    if updates:
                        update_consumption(engine, int(sel_row['id']), **updates)
                        st.success("Updated.")
                        st.rerun()
            with c_to_period:
                if st.button(
                    "Reclassify as period cost",
                    key="btn_wip_to_period",
                    use_container_width=True,
                    help="Treats this WIP entry as period cost in the consumption period. "
                         "Use when WIP classification was wrong at entry."
                ):
                    update_consumption(
                        engine, int(sel_row['id']),
                        allocation_type='period',
                        review_status='reviewed',
                    )
                    st.success("Reclassified to period.")
                    st.rerun()

    if not released_df.empty:
        st.divider()
        st.markdown("### Released WIP")
        display_rel = released_df[[
            'id', 'period', 'wip_released_period', 'customer_program',
            'total_cost', 'reviewer_name', 'notes'
        ]].copy()
        display_rel.columns = [
            'ID', 'Consumed in', 'Released to', 'Program', 'Total', 'Reviewer', 'Notes'
        ]
        st.dataframe(
            display_rel,
            use_container_width=True,
            hide_index=True,
            column_config={
                'Total': st.column_config.NumberColumn('Total', format="$%.2f"),
            },
        )


# =============================================================
# Public entry
# =============================================================

def render():
    st.title("Raw Material Cost")
    engine = get_engine()

    tab_invoice, tab_specs, tab_consumption, tab_program, tab_wip = st.tabs([
        "Invoice Detail", "Film Specs", "Consumption", "By Program", "WIP",
    ])

    with tab_invoice:
        _render_invoice_detail(engine)
    with tab_specs:
        _render_film_specs(engine)
    with tab_consumption:
        _render_consumption(engine)
    with tab_program:
        _render_by_program(engine)
    with tab_wip:
        _render_wip(engine)