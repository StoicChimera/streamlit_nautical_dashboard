"""
wip_labor_container_unload.py
=============================

Container Unload entry tab. Manual input for containers received in a period,
one row per container. Driver is SUM(pallet_count) per customer per ISO week,
dispatched by the 'unload_pallets' driver_key in Phase 1b.3.

Mirrors the Receiving Returns pattern:
  - Table of existing entries at top
  - Add / Update form below
  - Customer dropdown from active dim_customer rows
"""

import os
import pandas as pd
import streamlit as st
from datetime import datetime, timezone, date
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from . import wip_labor_allocation as wla

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

engine = create_engine(SUPABASE_CONN)


# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------

def _units(v):
    if pd.isna(v):
        return ""
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return str(v)


@st.cache_data(ttl=300, show_spinner=False)
def _get_active_customers() -> pd.DataFrame:
    """customer_name + canonical_key for active billable programs."""
    return pd.read_sql(text("""
        SELECT customer_name, canonical_key
        FROM dim_customer
        WHERE active = TRUE
          AND is_revenue_customer = TRUE
          AND roll_up_for_cost = FALSE
          AND canonical_key IS NOT NULL
        ORDER BY customer_name
    """), engine)


def _derive_period_and_week(received: date) -> tuple[str, int]:
    """Returns (YYYY-MM, iso_week) for the given date."""
    period = received.strftime("%Y-%m")
    iso_week = received.isocalendar()[1]
    return period, iso_week


# -------------------------------------------------------------
# Data readers / writers
# -------------------------------------------------------------

@st.cache_data(ttl=60, show_spinner=False)
def get_container_unload(period: str) -> pd.DataFrame:
    sql = text("""
        SELECT container_id, date_received, pallet_count,
               customer_canonical_key, accrual_period, iso_week,
               notes, set_by, set_at
        FROM stg_labor_container_unload
        WHERE accrual_period = :period
        ORDER BY date_received, container_id
    """)
    try:
        return pd.read_sql(sql, engine, params={"period": period})
    except Exception:
        return pd.DataFrame()


def upsert_container_unload(
    container_id: str,
    date_received: date,
    pallet_count: int,
    customer_canonical_key: str,
    notes: str,
    set_by: str,
) -> None:
    if not container_id or not container_id.strip():
        raise ValueError("container_id is required.")
    if pallet_count < 0:
        raise ValueError("pallet_count must be >= 0.")
    if not customer_canonical_key:
        raise ValueError("customer is required.")
    if not set_by or not set_by.strip():
        raise ValueError("set_by is required.")

    # Container IDs are ISO 6346 — always uppercase. Normalize to avoid
    # case-sensitive duplicates (e.g. BSDU125481 vs bsdu125481).
    normalized_id = container_id.strip().upper()

    period, iso_week = _derive_period_and_week(date_received)
    now = datetime.now(timezone.utc).isoformat()

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO stg_labor_container_unload
                (container_id, date_received, pallet_count,
                 customer_canonical_key, accrual_period, iso_week,
                 notes, set_by, set_at)
            VALUES
                (:container_id, :date_received, :pallet_count,
                 :customer_canonical_key, :accrual_period, :iso_week,
                 :notes, :set_by, :set_at)
            ON CONFLICT (container_id) DO UPDATE SET
                date_received          = EXCLUDED.date_received,
                pallet_count           = EXCLUDED.pallet_count,
                customer_canonical_key = EXCLUDED.customer_canonical_key,
                accrual_period         = EXCLUDED.accrual_period,
                iso_week               = EXCLUDED.iso_week,
                notes                  = EXCLUDED.notes,
                set_by                 = EXCLUDED.set_by,
                set_at                 = EXCLUDED.set_at
        """), {
            "container_id":           normalized_id,
            "date_received":          date_received,
            "pallet_count":           int(pallet_count),
            "customer_canonical_key": customer_canonical_key,
            "accrual_period":         period,
            "iso_week":               int(iso_week),
            "notes":                  (notes or "").strip() or None,
            "set_by":                 set_by.strip(),
            "set_at":                 now,
        })

    get_container_unload.clear()


def delete_container_unload(container_id: str) -> None:
    normalized_id = container_id.strip().upper()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM stg_labor_container_unload WHERE UPPER(container_id) = :container_id"),
            {"container_id": normalized_id},
        )
    get_container_unload.clear()


def _lookup_container_by_id(container_id: str) -> dict | None:
    """Look up a single container across all periods. Returns None if not found.
    Used by the tab renderer to flag overwrites before the user saves.
    Case-insensitive match so pre-normalization rows are still found."""
    if not container_id or not container_id.strip():
        return None

    normalized_id = container_id.strip().upper()
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT container_id, date_received, pallet_count,
                   customer_canonical_key, accrual_period, iso_week, notes
            FROM stg_labor_container_unload
            WHERE UPPER(container_id) = :container_id
            LIMIT 1
        """), {"container_id": normalized_id}).mappings().first()

    return dict(row) if row else None


# -------------------------------------------------------------
# Tab renderer
# -------------------------------------------------------------

def render_container_unload_tab(period: str, reviewer_name: str) -> None:
    st.subheader("Container Unload Entry")
    st.caption(
        "Record each container received this period. Driver is pallet_count "
        "per customer per ISO week — a 25-pallet container is weighted 2.5x a "
        "10-pallet container. Labor from the Container Unload cost center gets "
        "distributed accordingly."
    )

    customers_df = _get_active_customers()
    if customers_df.empty:
        st.warning("No active customers found in dim_customer.")
        return

    # Build customer lookup — display name -> canonical_key
    label_to_key = dict(zip(customers_df["customer_name"], customers_df["canonical_key"]))
    key_to_label = dict(zip(customers_df["canonical_key"], customers_df["customer_name"]))
    customer_options = customers_df["customer_name"].tolist()

    period_committed = wla.is_period_committed(period)
    if period_committed:
        st.warning(
            f"Period {period} allocation is **committed**. Container Unload "
            "entries are read-only for this period — adding or modifying "
            "containers would silently invalidate the locked allocation. "
            "Unlock from the Allocation tab before making changes."
        )

    # -------------------------------------------------------------
    # Existing entries
    # -------------------------------------------------------------
    existing = get_container_unload(period)

    if existing.empty:
        st.info("No containers recorded for this period yet.")
    else:
        st.markdown("#### Recorded Containers for Period")

        total_containers = len(existing)
        total_pallets    = int(existing["pallet_count"].sum())
        distinct_customers = existing["customer_canonical_key"].nunique()

        k1, k2, k3 = st.columns(3)
        k1.metric("Containers", total_containers)
        k2.metric("Total pallets", _units(total_pallets))
        k3.metric("Customers", distinct_customers)

        display = existing.copy()
        display["Customer"]      = display["customer_canonical_key"].map(
            lambda k: key_to_label.get(k, k)
        )
        display["Date Received"] = pd.to_datetime(display["date_received"]).dt.strftime("%Y-%m-%d")
        display["ISO Week"]      = display["iso_week"]
        display["Pallets"]       = display["pallet_count"].map(_units)
        display["Notes"]         = display["notes"].fillna("")
        display["Set By"]        = display["set_by"].fillna("")

        st.dataframe(
            display[[
                "container_id", "Date Received", "ISO Week",
                "Customer", "Pallets", "Notes", "Set By",
            ]].rename(columns={"container_id": "Container ID"}),
            use_container_width=True,
            hide_index=True,
        )

    if period_committed:
        return

    st.markdown("---")
    st.markdown("#### Add / Update Container")
    st.caption(
        "Container ID is the natural key — re-entering an existing ID updates "
        "that row. Period and ISO week are derived from the receive date."
    )

    col_id, col_date, col_cust = st.columns([2, 2, 3])
    with col_id:
        container_id = st.text_input(
            "Container ID",
            key=f"cu_id_{period}",
            placeholder="e.g. MSKU1234567",
        )
    with col_date:
        default_date = date.today()
        # If period is in the past, default to the 1st of that month
        try:
            period_year, period_month = int(period[:4]), int(period[5:7])
            default_date = date(period_year, period_month, 1)
        except (ValueError, IndexError):
            pass
        date_received = st.date_input(
            "Date Received",
            value=default_date,
            key=f"cu_date_{period}",
        )
    with col_cust:
        selected_customer = st.selectbox(
            "Customer",
            options=customer_options,
            key=f"cu_customer_{period}",
        )

    col_pallets, col_notes = st.columns([1, 4])
    with col_pallets:
        pallet_count = st.number_input(
            "Pallets",
            min_value=0, step=1, value=0,
            key=f"cu_pallets_{period}",
        )
    with col_notes:
        notes = st.text_input(
            "Notes (optional)",
            key=f"cu_notes_{period}",
        )

    # -----------------------------------------------------------------
    # Existing-entry detection — if the typed container ID already exists,
    # show its current values. Warn + require confirmation if the date
    # would change (common muscle-memory mistake: default date left in
    # place while updating an older container).
    # -----------------------------------------------------------------
    existing_row = _lookup_container_by_id(container_id)
    date_change_confirmed = True  # default: no existing row, or no change

    if existing_row is not None:
        existing_date     = existing_row["date_received"]
        existing_pallets  = existing_row["pallet_count"]
        existing_key      = existing_row["customer_canonical_key"]
        existing_period   = existing_row["accrual_period"]
        existing_customer = key_to_label.get(existing_key, existing_key)

        st.info(
            f"**Existing entry for {existing_row['container_id']}:**  \n"
            f"Date: {existing_date}  ·  Period: {existing_period}  ·  "
            f"{existing_pallets} pallets  ·  {existing_customer}"
        )

        date_would_change = (existing_date != date_received)
        if date_would_change:
            st.warning(
                f"Saving will change the receive date from **{existing_date}** "
                f"to **{date_received}**. Verify this is intentional."
            )
            date_change_confirmed = st.checkbox(
                "Yes, change the receive date",
                key=f"cu_confirm_date_{period}",
            )

    # Show derived period/iso_week as a sanity check
    if date_received:
        derived_period, derived_week = _derive_period_and_week(date_received)
        if derived_period != period:
            st.warning(
                f"This container falls in period **{derived_period}** (ISO week {derived_week}), "
                f"not the currently-selected period **{period}**. "
                "It will be saved to its own period and won't appear in this tab's table above."
            )
        else:
            st.caption(f"Will record under period {derived_period}, ISO week {derived_week}.")

    col_save, col_del, _ = st.columns([2, 2, 4])
    with col_save:
        if st.button(
            "Save Container",
            key=f"cu_save_{period}",
            type="primary",
            use_container_width=True,
        ):
            if not reviewer_name.strip():
                st.warning("Enter your name in the Reviewer's Name field above before saving.")
            elif not container_id.strip():
                st.warning("Container ID is required.")
            elif pallet_count <= 0:
                st.warning("Pallet count must be greater than zero.")
            elif not date_change_confirmed:
                st.warning("Tick the confirmation box above to change the receive date.")
            else:
                try:
                    upsert_container_unload(
                        container_id=container_id,
                        date_received=date_received,
                        pallet_count=pallet_count,
                        customer_canonical_key=label_to_key[selected_customer],
                        notes=notes,
                        set_by=reviewer_name,
                    )
                    # Clear the date-confirm checkbox so it doesn't persist to the next save
                    confirm_k = f"cu_confirm_date_{period}"
                    if confirm_k in st.session_state:
                        del st.session_state[confirm_k]
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))

    with col_del:
        if st.button(
            "Remove Container",
            key=f"cu_del_{period}",
            type="secondary",
            use_container_width=True,
        ):
            if not reviewer_name.strip():
                st.warning("Enter your name above before removing.")
            elif not container_id.strip():
                st.warning("Enter a container ID to remove.")
            else:
                delete_container_unload(container_id.strip())
                st.rerun()