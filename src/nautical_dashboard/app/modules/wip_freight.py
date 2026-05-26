from __future__ import annotations

import os
from datetime import date, datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from . import auth

load_dotenv()

CONN_STRING = os.getenv("SUPABASE_CONN")


@st.cache_resource
def get_engine():
    return create_engine(CONN_STRING)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def load_unmatched(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT
            invoice_num, bill_date, line_description, amount,
            distribution_account, customer_full_name,
            customer_ref_num, match_type, match_status
        FROM mv_wip_fulfillment_freight
        WHERE match_status = 'unmatched'
        ORDER BY bill_date DESC
        """,
        engine,
    )


def load_all_freight(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT
            invoice_num, bill_date, line_description, amount,
            distribution_account, customer_full_name, customer_ref_num,
            match_type, recognized_period, recognized_year,
            recognized_month, match_status
        FROM mv_wip_fulfillment_freight
        ORDER BY bill_date DESC
        """,
        engine,
    )


def load_existing_matches(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT id, invoice_num, line_description, customer_ref_num,
               customer_full_name, recognized_period, match_source,
               notes, updated_at
        FROM public.dim_freight_matching
        ORDER BY updated_at DESC
        """,
        engine,
    )


def load_customer_types(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT customer_parent, match_type, notes
        FROM public.dim_freight_customer_type
        ORDER BY match_type, customer_parent
        """,
        engine,
    )


def load_project_invoices(engine, start_date: str, end_date: str) -> pd.DataFrame:
    return pd.read_sql(
        text(
            """
            SELECT
                s.invoice_num,
                s.customer_full_name,
                s.customer_ref_num,
                MIN(s.contract_completion_date::date) AS contract_completion_date,
                SUM(s.amount)                          AS total_revenue
            FROM stg_product_service_detail s
            JOIN dim_freight_customer_type ct
                ON ct.customer_parent = TRIM(SPLIT_PART(s.customer_full_name, ':', 1))
               AND ct.match_type = 'project'
            WHERE s.contract_completion_date::date >= :start_date
              AND s.contract_completion_date::date <  :end_date
            GROUP BY s.invoice_num, s.customer_full_name, s.customer_ref_num
            ORDER BY s.customer_full_name, s.invoice_num
            """
        ),
        engine,
        params={"start_date": start_date, "end_date": end_date},
    )


def load_unmatched_project_freight(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT
            f.invoice_num, f.bill_date, f.line_description,
            f.amount, f.customer_full_name, f.customer_ref_num,
            f.match_status
        FROM mv_wip_fulfillment_freight f
        JOIN dim_freight_customer_type ct
            ON ct.customer_parent = f.customer_parent
           AND ct.match_type = 'project'
        WHERE f.match_status = 'unmatched'
        ORDER BY f.customer_full_name, f.bill_date DESC
        """,
        engine,
    )



def load_matched_freight(engine, year: int, month: int) -> pd.DataFrame:
    return pd.read_sql(
        text(
            """
            SELECT
                invoice_num, bill_date, line_description, amount,
                distribution_account, customer_full_name, customer_ref_num,
                match_type, recognized_period, match_status
            FROM mv_wip_fulfillment_freight
            WHERE match_status != 'unmatched'
              AND recognized_year  = :year
              AND recognized_month = :month
            ORDER BY customer_full_name, bill_date
            """
        ),
        engine,
        params={"year": year, "month": month},
    )


def load_matched_months(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT DISTINCT
            recognized_year,
            recognized_month,
            DATE_TRUNC('month', recognized_period)::DATE AS month_start
        FROM mv_wip_fulfillment_freight
        WHERE match_status != 'unmatched'
          AND recognized_period IS NOT NULL
        ORDER BY month_start DESC
        """,
        engine,
    )

def upsert_match(engine, invoice_num, line_description, customer_ref_num,
                 customer_full_name, recognized_period, notes):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO public.dim_freight_matching
                    (invoice_num, line_description, customer_ref_num, customer_full_name,
                     recognized_period, match_source, notes)
                VALUES
                    (:invoice_num, :line_description, :customer_ref_num, :customer_full_name,
                     :recognized_period, 'manual', :notes)
                ON CONFLICT (invoice_num, line_description)
                DO UPDATE SET
                    recognized_period  = EXCLUDED.recognized_period,
                    match_source       = 'manual',
                    notes              = EXCLUDED.notes,
                    customer_ref_num   = EXCLUDED.customer_ref_num,
                    customer_full_name = EXCLUDED.customer_full_name,
                    updated_at         = NOW();
                """
            ),
            {
                "invoice_num":        invoice_num,
                "line_description":   line_description,
                "customer_ref_num":   customer_ref_num or None,
                "customer_full_name": customer_full_name or None,
                "recognized_period":  recognized_period,
                "notes":              notes or None,
            },
        )


def delete_match(engine, match_id: int):
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM public.dim_freight_matching WHERE id = :id"),
            {"id": match_id},
        )


def upsert_customer_type(engine, customer_parent, match_type, notes):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO public.dim_freight_customer_type (customer_parent, match_type, notes)
                VALUES (:customer_parent, :match_type, :notes)
                ON CONFLICT (customer_parent)
                DO UPDATE SET match_type = EXCLUDED.match_type, notes = EXCLUDED.notes;
                """
            ),
            {"customer_parent": customer_parent.strip(), "match_type": match_type, "notes": notes or None},
        )


def delete_customer_type(engine, customer_parent):
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM public.dim_freight_customer_type WHERE customer_parent = :cp"),
            {"cp": customer_parent},
        )


def refresh_mv(engine):
    with engine.begin() as conn:
        conn.execute(text("REFRESH MATERIALIZED VIEW mv_wip_fulfillment_freight"))


def upsert_freight_signoff(engine, accrual_period, signed_off_by, notes,
                            unmatched_count, unmatched_total):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO public.dim_freight_period_signoff
                    (accrual_period, signed_off_by, notes,
                     unmatched_count, unmatched_total)
                VALUES
                    (:period, :by, :notes, :cnt, :total)
                ON CONFLICT (accrual_period) DO UPDATE SET
                    signed_off_by   = EXCLUDED.signed_off_by,
                    signed_off_at   = NOW(),
                    notes           = EXCLUDED.notes,
                    unmatched_count = EXCLUDED.unmatched_count,
                    unmatched_total = EXCLUDED.unmatched_total;
            """),
            {
                "period": accrual_period,
                "by":     signed_off_by,
                "notes":  notes or None,
                "cnt":    int(unmatched_count),
                "total":  float(unmatched_total),
            },
        )


def load_freight_signoff(engine, accrual_period: str):
    df = pd.read_sql(
        text("""
            SELECT accrual_period, signed_off_by, signed_off_at,
                   notes, unmatched_count, unmatched_total
            FROM dim_freight_period_signoff
            WHERE accrual_period = :period
        """),
        engine,
        params={"period": accrual_period},
    )
    return df.iloc[0].to_dict() if not df.empty else None


# ---------------------------------------------------------------------------
# Bulk freight assignment helper (virtualized, off-screen preserving)
# ---------------------------------------------------------------------------

def _bulk_freight_set_key(prefix: str) -> str:
    return f"freight_bulk_set_{prefix}"


def _bulk_freight_editor_key(prefix: str) -> str:
    return f"freight_bulk_editor_{prefix}"


def _bulk_freight_cache_key(prefix: str) -> str:
    return f"freight_bulk_cache_{prefix}"


def _bulk_freight_sig_key(prefix: str) -> str:
    return f"freight_bulk_sig_{prefix}"


def _row_uid(row) -> str:
    """Stable identifier for a freight line — invoice_num + line_description.
    Mirrors the unique key used in dim_freight_matching's ON CONFLICT clause."""
    return f"{row['invoice_num']}||{row['line_description']}"


def render_freight_bulk_assigner(
    engine,
    df: pd.DataFrame,
    key_prefix: str,
    *,
    show_columns: list[str] | None = None,
    empty_msg: str = "No unmatched lines.",
):
    """Virtualized bulk-assign UI for freight lines.

    Renders:
      - placeholder slot for the bulk-assign panel (filled after the list)
      - filterable st.data_editor with a Select checkbox column
      - Select all visible / Clear buttons
      - bulk period + notes form (in the placeholder, rendered last)

    df must contain: invoice_num, line_description, customer_full_name,
                     customer_ref_num, amount, bill_date.
    """
    if df.empty:
        st.success(empty_msg)
        return

    set_k    = _bulk_freight_set_key(key_prefix)
    editor_k = _bulk_freight_editor_key(key_prefix)
    cache_k  = _bulk_freight_cache_key(key_prefix)
    sig_k    = _bulk_freight_sig_key(key_prefix)

    if set_k not in st.session_state:
        st.session_state[set_k] = set()

    # Reserve the bulk panel slot up front so it appears above the list
    # but renders content after the list has synced the selection.
    bulk_panel_slot = st.empty()

    st.warning(f"{len(df)} unmatched line(s) — ${df['amount'].sum():,.2f} total")

    # --- Filter ---
    search_term = st.text_input(
        "Filter by customer or description",
        key=f"freight_bulk_search_{key_prefix}",
        placeholder="Type to filter...",
    )

    filtered = df.copy()
    if search_term:
        mask = (
            filtered['customer_full_name'].astype(str).str.contains(search_term, case=False, na=False)
            | filtered['line_description'].astype(str).str.contains(search_term, case=False, na=False)
        )
        filtered = filtered[mask]

    if filtered.empty:
        st.info("No lines match the current filter.")
        return

    # Build a stable row_uid so off-screen preservation works across filter changes.
    filtered = filtered.reset_index(drop=True)
    filtered['_row_uid'] = filtered.apply(_row_uid, axis=1)

    prior_selected = st.session_state[set_k]

    # Cache signature: filter + row count. Rebuilds when filter changes
    # or underlying data shrinks (post-Apply).
    filter_sig = f"{search_term}|n={len(filtered)}"
    if st.session_state.get(sig_k) != filter_sig:
        display_cols = {
            'Select':       filtered['_row_uid'].isin(prior_selected).values,
            'Invoice':      filtered['invoice_num'].astype(str).values,
            'Bill Date':    [
                d.strftime('%Y-%m-%d') if pd.notna(d) else 'N/A'
                for d in filtered['bill_date']
            ],
            'Customer':     filtered['customer_full_name'].astype(str).values,
            'Customer Ref': filtered['customer_ref_num'].fillna('').astype(str).values,
            'Description':  filtered['line_description'].astype(str).values,
            'Amount':       [f"${float(a):,.2f}" for a in filtered['amount']],
        }
        if show_columns:
            display_cols = {k: v for k, v in display_cols.items() if k in show_columns or k == 'Select'}

        st.session_state[cache_k] = pd.DataFrame(display_cols)
        if editor_k in st.session_state:
            del st.session_state[editor_k]
        st.session_state[sig_k] = filter_sig

    display_df = st.session_state[cache_k]
    caption_slot = st.empty()

    edited = st.data_editor(
        display_df,
        use_container_width=True,
        height=520,
        hide_index=True,
        key=editor_k,
        disabled=[c for c in display_df.columns if c != 'Select'],
        column_config={
            'Select':       st.column_config.CheckboxColumn('Select', default=False, width='small'),
            'Invoice':      st.column_config.TextColumn('Invoice', width='small'),
            'Bill Date':    st.column_config.TextColumn('Bill Date', width='small'),
            'Customer':     st.column_config.TextColumn('Customer', width='medium'),
            'Customer Ref': st.column_config.TextColumn('Customer Ref', width='small'),
            'Description':  st.column_config.TextColumn('Description', width='large'),
            'Amount':       st.column_config.TextColumn('Amount', width='small'),
        },
    )

    # Sync visible truth, preserve off-screen.
    visible_uids = set(filtered['_row_uid'])
    # Map visible editor rows back to row_uid via the Invoice + Description columns.
    # display_df preserves order, so we can zip.
    edited_with_uid = edited.copy()
    edited_with_uid['_row_uid'] = filtered['_row_uid'].values
    visible_selected = set(edited_with_uid[edited_with_uid['Select']]['_row_uid'])
    preserved_offscreen = prior_selected - visible_uids
    st.session_state[set_k] = preserved_offscreen | visible_selected

    n_after = len(st.session_state[set_k])
    selected_amount = float(
        df[df.apply(_row_uid, axis=1).isin(st.session_state[set_k])]['amount'].sum()
    )

    cap = (
        f"Showing {len(filtered)} of {len(df)}  "
        f"·  **{n_after}** selected  ·  ${selected_amount:,.2f} selected total"
    )
    if preserved_offscreen:
        cap += f"  ·  {len(preserved_offscreen)} off-screen retained"
    caption_slot.caption(cap)

    # --- Select all visible / Clear ---
    not_yet = visible_uids - visible_selected
    col_sa, col_cl = st.columns(2)
    with col_sa:
        if not_yet and st.button(
            f"Select all {len(not_yet)} visible",
            key=f"freight_select_all_{key_prefix}",
            use_container_width=True,
            help="Checks every visible row. Off-screen selections are preserved.",
        ):
            new_cached = st.session_state[cache_k].copy()
            new_cached['Select'] = True
            st.session_state[cache_k] = new_cached
            if editor_k in st.session_state:
                del st.session_state[editor_k]
            st.rerun()
    with col_cl:
        if n_after and st.button(
            f"Clear selection ({n_after})",
            key=f"freight_clear_sel_{key_prefix}",
            use_container_width=True,
        ):
            new_cached = st.session_state[cache_k].copy()
            new_cached['Select'] = False
            st.session_state[cache_k] = new_cached
            if editor_k in st.session_state:
                del st.session_state[editor_k]
            st.session_state[set_k] = set()
            st.rerun()

    # --- Bulk-assign panel (rendered into the reserved slot) ---
    with bulk_panel_slot.container():
        with st.container(border=True):
            if n_after == 0:
                st.markdown("### Bulk assign period")
                st.caption(
                    "Select lines in the list below, then pick a period to apply "
                    "to all of them in one transaction."
                )
            else:
                # Build a small preview (up to 5 customers)
                selected_df = df[df.apply(_row_uid, axis=1).isin(st.session_state[set_k])]
                preview_customers = sorted(set(selected_df['customer_full_name'].astype(str)))[:5]
                preview = ", ".join(f"`{c}`" for c in preview_customers)
                if len(set(selected_df['customer_full_name'])) > 5:
                    preview += f" + {len(set(selected_df['customer_full_name'])) - 5} more"

                st.markdown(
                    f"### Bulk assign period — {n_after} line(s) selected  "
                    f"·  ${selected_amount:,.2f}"
                )
                st.caption(preview)

            with st.form(f"freight_bulk_form_{key_prefix}"):
                bulk_period = st.date_input(
                    "Recognized period — normalized to 1st of month",
                    value=date.today().replace(day=1),
                    key=f"freight_bulk_period_{key_prefix}",
                )
                bulk_notes = st.text_input(
                    "Notes (optional)",
                    key=f"freight_bulk_notes_{key_prefix}",
                )
                submitted = st.form_submit_button(
                    f"Apply to {n_after} selected" if n_after else "Apply to selected",
                    type="primary",
                    use_container_width=True,
                    disabled=(n_after == 0),
                )

                if submitted and n_after > 0:
                    rp = bulk_period.replace(day=1)
                    selected_df = df[df.apply(_row_uid, axis=1).isin(st.session_state[set_k])]
                    for _, row in selected_df.iterrows():
                        upsert_match(
                            engine,
                            invoice_num        = row['invoice_num'],
                            line_description   = row['line_description'],
                            customer_ref_num   = row.get('customer_ref_num'),
                            customer_full_name = row['customer_full_name'],
                            recognized_period  = rp,
                            notes              = bulk_notes,
                        )
                    refresh_mv(engine)
                    # Reset selection and editor state so the same batch
                    # can't be re-applied by accident.
                    st.session_state[set_k] = set()
                    if editor_k in st.session_state:
                        del st.session_state[editor_k]
                    st.success(f"Matched {len(selected_df)} line(s) to {rp.strftime('%B %Y')}")
                    st.rerun()


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------

def render():
    st.title("WIP Freight — Period Matching")

    engine = get_engine()

    tab_matched, tab_unmatched, tab_project, tab_all, tab_manage, tab_customers = st.tabs([
        "Matched Lines",
        "Unmatched Lines",
        "Project Invoice Lookup",
        "All Freight Lines",
        "Manage Applied Lines",
        "Customer Types",
    ])

    with tab_matched:
            st.subheader("Matched Freight Lines")

            months_df = load_matched_months(engine)

            if months_df.empty:
                st.info("No matched freight lines yet.")
            else:
                month_labels = months_df.apply(
                    lambda r: pd.Timestamp(r["month_start"]).strftime("%B %Y"), axis=1
                ).tolist()

                selected_label = st.selectbox("Select period", month_labels, key="matched_month_sel")
                selected_idx   = month_labels.index(selected_label)
                selected_row   = months_df.iloc[selected_idx]
                year  = int(selected_row["recognized_year"])
                month = int(selected_row["recognized_month"])

                df_matched = load_matched_freight(engine, year, month)

                col1, col2, col3 = st.columns(3)
                col1.metric("Matched lines", len(df_matched))
                col2.metric("Total amount",  f"${df_matched['amount'].sum():,.2f}")
                col3.metric("Customers",     df_matched["customer_full_name"].nunique())

                st.dataframe(df_matched, use_container_width=True, hide_index=True)

    # ── Tab 1: Unmatched lines ──────────────────────────────────────────────
    with tab_unmatched:
        st.subheader("Unmatched Freight Lines")
        st.caption(
            "Lines that could not be automatically matched to a revenue period. "
            "Common causes: customer not in dim_freight_customer_type, project customer "
            "with no matching invoice yet, or revenue-match customer with no revenue that month."
        )

        if st.button("Refresh view", key="refresh_unmatched"):
            with st.spinner("Refreshing..."):
                refresh_mv(engine)
            st.rerun()

        df_unmatched = load_unmatched(engine)

        # -----------------------------------------------------------------
        # Period sign-off section
        # -----------------------------------------------------------------
        st.divider()
        st.markdown("#### Period Sign-Off")
        st.caption(
            "Attest that you've reviewed all unmatched freight for a period "
            "and accept that the remaining lines are legitimate WIP awaiting "
            "future revenue. Required for preflight to clear."
        )

        user = auth.current_user()
        reviewer_name = user["name"]

        col_p, col_n = st.columns([2, 6])
        with col_p:
            signoff_period = st.date_input(
                "Period",
                value=date.today().replace(day=1),
                key="freight_signoff_period",
            )
        with col_n:
            signoff_notes = st.text_input(
                "Notes (optional)",
                key="freight_signoff_notes",
            )

        period_str = signoff_period.strftime("%Y-%m")
        existing_signoff = load_freight_signoff(engine, period_str)

        if existing_signoff:
            st.success(
                f"Signed off for {period_str} by **{existing_signoff['signed_off_by']}** "
                f"at {existing_signoff['signed_off_at']}  ·  "
                f"{existing_signoff['unmatched_count']} unmatched line(s) at sign-off  ·  "
                f"${float(existing_signoff['unmatched_total']):,.2f} total"
            )
            if existing_signoff.get("notes"):
                st.caption(f"Notes: {existing_signoff['notes']}")

        if st.button("Sign Off Freight for Period", type="primary", key="btn_freight_signoff"):
            upsert_freight_signoff(
                engine,
                accrual_period=period_str,
                signed_off_by=reviewer_name,
                notes=signoff_notes,
                unmatched_count=len(df_unmatched),
                unmatched_total=float(df_unmatched["amount"].sum()) if not df_unmatched.empty else 0.0,
            )
            st.success(f"Freight signed off for {period_str} by {reviewer_name}.")
            st.rerun()

        st.divider()

        if df_unmatched.empty:
            st.success("No unmatched lines — all freight is period-matched.")
        else:
            st.divider()
            render_freight_bulk_assigner(
                engine,
                df_unmatched,
                key_prefix="unmatched_all",
                empty_msg="No unmatched lines.",
            )

    # ── Tab 3: Project Invoice Lookup ───────────────────────────────────────
    with tab_project:
        st.subheader("Project Invoice Lookup — Arrived Co & RECESS")
        st.caption(
            "Shows invoices posted in stg_product_service_detail for project-type customers "
            "in a selected period. Cross-reference the Customer Ref # against unmatched freight "
            "lines below, check the lines you want to assign, then set the period and save."
        )

        col_s, col_e = st.columns(2)
        with col_s:
            proj_start = st.date_input(
                "Period start",
                value=pd.Timestamp.today().replace(day=1),
                key="proj_start",
            )
        with col_e:
            proj_end = st.date_input(
                "Period end (exclusive)",
                value=pd.Timestamp.today().replace(day=1) + pd.DateOffset(months=1),
                key="proj_end",
            )

        if st.button("Load invoices", key="btn_load_project_invoices"):
            df_invoices = load_project_invoices(engine, str(proj_start), str(proj_end))

            if df_invoices.empty:
                st.info("No project invoices found for this period.")
            else:
                st.success(f"{len(df_invoices)} invoice(s) found — ${df_invoices['total_revenue'].sum():,.2f} total revenue")
                df_invoices["total_revenue"] = df_invoices["total_revenue"].apply(lambda x: f"${x:,.2f}")
                st.dataframe(
                    df_invoices.rename(columns={
                        "invoice_num":              "Invoice #",
                        "customer_full_name":       "Customer",
                        "customer_ref_num":         "Customer Ref #",
                        "contract_completion_date": "Completion Date",
                        "total_revenue":            "Revenue",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

        st.divider()
        st.subheader("Unmatched Project Freight Lines")
        st.caption("Filter, select, and bulk-assign a recognized period.")

        df_proj_unmatched = load_unmatched_project_freight(engine)
        render_freight_bulk_assigner(
            engine,
            df_proj_unmatched,
            key_prefix="project_unmatched",
            empty_msg="No unmatched project freight lines.",
        )

    # ── Tab 4: All freight lines ────────────────────────────────────────────
    with tab_all:
        st.subheader("All Freight Lines")

        if st.button("Refresh view", key="refresh_all"):
            with st.spinner("Refreshing..."):
                refresh_mv(engine)
            st.rerun()

        df_all = load_all_freight(engine)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Total lines",  len(df_all))
        col2.metric("Total amount", f"${df_all['amount'].sum():,.2f}")
        matched   = df_all[df_all["match_status"] != "unmatched"]
        unmatched = df_all[df_all["match_status"] == "unmatched"]
        col3.metric("Matched",   f"{len(matched)} lines  •  ${matched['amount'].sum():,.2f}")
        col4.metric("Unmatched", f"{len(unmatched)} lines  •  ${unmatched['amount'].sum():,.2f}")

        status_options = df_all["match_status"].dropna().unique().tolist()
        status_filter  = st.multiselect("Filter by match status", options=status_options, default=status_options)
        st.dataframe(
            df_all[df_all["match_status"].isin(status_filter)],
            use_container_width=True,
            hide_index=True,
        )

    # ── Tab 5: Manage manual overrides ─────────────────────────────────────
    with tab_manage:
        st.subheader("Manage Applied Lines — dim_freight_matching")
        st.caption("Freight lines that were manually assigned a period. Edit or delete assignments here.")

        df_matches = load_existing_matches(engine)

        if df_matches.empty:
            st.info("No manual overrides recorded yet.")
        else:
            st.dataframe(df_matches, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("Edit or delete an override")

            match_labels = df_matches.apply(
                lambda r: f"[{r['id']}] {r['invoice_num']} | {str(r['line_description'])[:50]}",
                axis=1,
            ).tolist()
            sel_label = st.selectbox("Select override", match_labels, key="sel_manage_match")
            sel_idx   = match_labels.index(sel_label)
            sel_match = df_matches.iloc[sel_idx]

            with st.form("form_edit_match"):
                rp_current = sel_match["recognized_period"]
                if isinstance(rp_current, str):
                    rp_current = datetime.strptime(rp_current[:10], "%Y-%m-%d").date()

                new_period = st.date_input("Recognized period", value=rp_current)
                new_notes  = st.text_input("Notes", value=sel_match["notes"] or "")

                col_save, col_del = st.columns(2)
                save_clicked   = col_save.form_submit_button("Update")
                delete_clicked = col_del.form_submit_button("Delete", type="secondary")

                if save_clicked:
                    upsert_match(
                        engine,
                        invoice_num        = sel_match["invoice_num"],
                        line_description   = sel_match["line_description"],
                        customer_ref_num   = sel_match.get("customer_ref_num"),
                        customer_full_name = sel_match.get("customer_full_name"),
                        recognized_period  = new_period.replace(day=1),
                        notes              = new_notes,
                    )
                    refresh_mv(engine)
                    st.success("Override updated.")
                    st.rerun()

                if delete_clicked:
                    delete_match(engine, int(sel_match["id"]))
                    refresh_mv(engine)
                    st.warning(f"Override [{sel_match['id']}] deleted.")
                    st.rerun()

    # ── Tab 6: Customer type config ─────────────────────────────────────────
    with tab_customers:
        st.subheader("Customer Match Types — dim_freight_customer_type")
        st.caption(
            "revenue_match = expense when revenue hits that month. "
            "project = hold until invoice appears in stg_product_service_detail via customer_ref_num."
        )

        df_ct = load_customer_types(engine)
        st.dataframe(df_ct, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Add or update a customer")

        with st.form("form_customer_type"):
            new_parent     = st.text_input("customer_parent (must match exactly as it appears in customer_full_name)")
            new_match_type = st.selectbox("Match type", ["revenue_match", "project"])
            new_notes      = st.text_input("Notes (optional)")

            if st.form_submit_button("Save"):
                if not new_parent.strip():
                    st.error("customer_parent is required.")
                else:
                    upsert_customer_type(engine, new_parent, new_match_type, new_notes)
                    st.success(f"Saved: {new_parent.strip()} → {new_match_type}")
                    st.rerun()

        if not df_ct.empty:
            st.divider()
            st.subheader("Delete a customer type")
            del_label = st.selectbox(
                "Select customer to remove",
                df_ct["customer_parent"].tolist(),
                key="sel_del_ct",
            )
            if st.button("Delete", type="secondary", key="btn_del_ct"):
                delete_customer_type(engine, del_label)
                st.warning(f"Removed: {del_label}")
                st.rerun()