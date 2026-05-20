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
        "Manage Overrides",
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
            st.warning(f"{len(df_unmatched)} unmatched line(s) — ${df_unmatched['amount'].sum():,.2f} total")
            st.dataframe(df_unmatched, use_container_width=True, hide_index=True)

            st.divider()
            st.subheader("Manually assign a period")

            row_labels = df_unmatched.apply(
                lambda r: (
                    f"{r['invoice_num']} | "
                    f"{r['bill_date'].strftime('%Y-%m-%d') if pd.notna(r['bill_date']) else 'N/A'} | "
                    f"{str(r['customer_full_name'])[:30]} | "
                    f"{str(r['line_description'])[:40]}"
                ),
                axis=1,
            ).tolist()

            selected_label = st.selectbox("Select line", row_labels, key="sel_unmatched_line")
            selected_idx   = row_labels.index(selected_label)
            selected_row   = df_unmatched.iloc[selected_idx]

            with st.form("form_assign_period"):
                st.write(f"**Customer:** {selected_row['customer_full_name']}")
                st.write(f"**Match type:** {selected_row['match_type'] or 'not configured'}")
                st.write(f"**Description:** {selected_row['line_description']}")
                st.write(f"**Amount:** ${selected_row['amount']:,.2f}")

                recognized_period = st.date_input(
                    "Recognized period — pick any day in the target month, normalized to 1st",
                    value=selected_row["bill_date"].date() if pd.notna(selected_row["bill_date"]) else date.today(),
                )
                notes = st.text_input("Notes (optional)")

                if st.form_submit_button("Save override"):
                    rp = recognized_period.replace(day=1)
                    upsert_match(
                        engine,
                        invoice_num        = selected_row["invoice_num"],
                        line_description   = selected_row["line_description"],
                        customer_ref_num   = selected_row.get("customer_ref_num"),
                        customer_full_name = selected_row["customer_full_name"],
                        recognized_period  = rp,
                        notes              = notes,
                    )
                    refresh_mv(engine)
                    st.success(f"Manually matched to {rp.strftime('%B %Y')}")
                    st.rerun()

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
        st.caption("Check lines to bulk-assign a recognized period.")

        df_proj_unmatched = load_unmatched_project_freight(engine)

        if df_proj_unmatched.empty:
            st.success("No unmatched project freight lines.")
        else:
            st.warning(f"{len(df_proj_unmatched)} unmatched line(s) — ${df_proj_unmatched['amount'].sum():,.2f} total")

            # Checkbox selection via data_editor
            df_proj_unmatched.insert(0, "select", False)

            edited = st.data_editor(
                df_proj_unmatched,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "select": st.column_config.CheckboxColumn("Select", default=False),
                },
                disabled=[c for c in df_proj_unmatched.columns if c != "select"],
                key="proj_unmatched_editor",
            )

            selected_rows = edited[edited["select"] == True]

            if not selected_rows.empty:
                st.info(f"{len(selected_rows)} line(s) selected — ${selected_rows['amount'].sum():,.2f} total")

                with st.form("form_bulk_assign"):
                    bulk_period = st.date_input(
                        "Recognized period — normalized to 1st of month",
                        value=date.today().replace(day=1),
                        key="bulk_period_input",
                    )
                    bulk_notes = st.text_input("Notes (optional)", key="bulk_notes")

                    if st.form_submit_button("Save override for selected"):
                        rp = bulk_period.replace(day=1)
                        for _, row in selected_rows.iterrows():
                            upsert_match(
                                engine,
                                invoice_num        = row["invoice_num"],
                                line_description   = row["line_description"],
                                customer_ref_num   = row.get("customer_ref_num"),
                                customer_full_name = row["customer_full_name"],
                                recognized_period  = rp,
                                notes              = bulk_notes,
                            )
                        refresh_mv(engine)
                        st.success(f"Matched {len(selected_rows)} line(s) to {rp.strftime('%B %Y')}")
                        st.rerun()

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
        st.subheader("Manual Overrides — dim_freight_matching")
        st.caption("Lines where automated matching failed and a period was manually assigned.")

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