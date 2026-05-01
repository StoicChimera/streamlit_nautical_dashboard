from __future__ import annotations

import os
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
CONN_STRING = os.getenv("SUPABASE_CONN")


@st.cache_resource
def get_engine():
    return create_engine(CONN_STRING)


def load_raw_goods(engine, start_date: str, end_date: str) -> pd.DataFrame:
    return pd.read_sql(
        text(
            """
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
            """
        ),
        engine,
        params={"start_date": start_date, "end_date": end_date},
    )


def load_allocs(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT id, period, customer_program, customer_parent, amount, notes, updated_at
        FROM public.dim_raw_material_manual_alloc
        ORDER BY period DESC, customer_program
        """,
        engine,
    )


def load_programs(engine) -> list[str]:
    df = pd.read_sql(
        "SELECT DISTINCT customer_program FROM mv_program_profitability ORDER BY customer_program",
        engine,
    )
    return df["customer_program"].tolist()


def load_parent_map(engine) -> dict[str, str]:
    df = pd.read_sql(
        "SELECT DISTINCT customer_program, customer_parent FROM mv_program_profitability",
        engine,
    )
    return dict(zip(df["customer_program"], df["customer_parent"]))


def upsert_alloc(engine, period, program, parent, amount, notes, row_id=None):
    with engine.begin() as conn:
        if row_id:
            conn.execute(
                text("""
                    UPDATE public.dim_raw_material_manual_alloc
                    SET period = :period, customer_program = :program,
                        customer_parent = :parent, amount = :amount, notes = :notes
                    WHERE id = :id
                """),
                {"period": period, "program": program, "parent": parent,
                 "amount": amount, "notes": notes, "id": row_id}
            )
        else:
            conn.execute(
                text("""
                    INSERT INTO public.dim_raw_material_manual_alloc
                        (period, customer_program, customer_parent, amount, notes)
                    VALUES (:period, :program, :parent, :amount, :notes)
                    ON CONFLICT (period, customer_program, notes)
                    DO UPDATE SET
                        amount = EXCLUDED.amount,
                        customer_parent = EXCLUDED.customer_parent,
                        updated_at = NOW()
                """),
                {"period": period, "program": program, "parent": parent,
                 "amount": amount, "notes": notes or ""}
            )


def delete_alloc(engine, row_id: int):
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM public.dim_raw_material_manual_alloc WHERE id = :id"),
            {"id": row_id}
        )


def refresh_mvs(engine):
    with engine.begin() as conn:
        conn.execute(text("REFRESH MATERIALIZED VIEW mv_raw_materials_by_program"))
        conn.execute(text("REFRESH MATERIALIZED VIEW mv_program_profitability"))


def render():
    st.title("Raw Material Cost")

    engine = get_engine()

    tab_invoice, tab_manual = st.tabs(["Invoice Detail", "Manual Allocations"])

    # ── Tab 1: Invoice Detail ───────────────────────────────────────────────
    with tab_invoice:
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
                "Accrual period end (exclusive)",
                value=default_end,
                key="raw_end",
            )

        if st.button("Run", key="btn_raw_run"):
            with st.spinner("Querying..."):
                df = load_raw_goods(engine, str(start_date), str(end_date))

            if df.empty:
                st.warning("No data found for that period.")
            else:
                detail = df[df["sort_order"] == 0].drop(columns=["sort_order"])
                total  = df[df["sort_order"] == 1].drop(columns=["sort_order", "invoice_num", "contract_completion_date"])

                detail["margin_pct"] = (
                    (detail["total_revenue"] - detail["raw_goods_cost"])
                    / detail["total_revenue"] * 100
                )

                if not total.empty:
                    t = total.iloc[0]
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Total Revenue",       f"${t['total_revenue']:,.2f}")
                    c2.metric("Total Raw Goods Cost", f"${t['raw_goods_cost']:,.2f}")
                    c3.metric("Gross Margin",         f"${t['total_revenue'] - t['raw_goods_cost']:,.2f}")

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

    # ── Tab 2: Manual Allocations ───────────────────────────────────────────
    with tab_manual:
        st.caption(
            "Manually allocate raw material costs (e.g. BOPP film consumption JEs) "
            "to specific programs by period. Entries flow into mv_raw_materials_by_program."
        )

        df_alloc = load_allocs(engine)
        programs  = load_programs(engine)
        parent_map = load_parent_map(engine)

        if not df_alloc.empty:
            col1, col2, col3 = st.columns(3)
            col1.metric("Total entries",    len(df_alloc))
            col2.metric("Total allocated",  f"${df_alloc['amount'].sum():,.2f}")
            col3.metric("Periods covered",  df_alloc["period"].nunique())

            period_filter = st.selectbox(
                "Filter by period",
                ["All"] + sorted(df_alloc["period"].unique().tolist(), reverse=True),
                key="manual_period_filter",
            )
            display = df_alloc if period_filter == "All" else df_alloc[df_alloc["period"] == period_filter]
            display = display.copy()
            display["amount"] = display["amount"].apply(lambda x: f"${x:,.2f}")
            st.dataframe(display.drop(columns=["id"]), use_container_width=True, hide_index=True)
        else:
            st.info("No manual allocations yet.")

        st.subheader("Raw Goods Journal Entries")

        je_period = st.text_input("Period to inspect (YYYY-MM)", value="2026-02", key="je_period")

        if st.button("Load JEs", key="btn_load_jes"):
            df_jes = pd.read_sql(
                text("""
                    SELECT num, txn_type, account_name, amount, description
                    FROM clean_qbo_transaction_splits_flat
                    WHERE chunk_month = :period
                    AND txn_type = 'Journal Entry'
                    AND (LOWER(account_name) LIKE '%raw goods%'
                    OR LOWER(account_name) LIKE '%50100%')
                    ORDER BY amount DESC
                """),
                engine,
                params={"period": je_period}
            )
            if df_jes.empty:
                st.info("No raw goods journal entries found for this period.")
            else:
                st.metric("Total JE amount", f"${df_jes['amount'].sum():,.2f}")
                st.dataframe(df_jes, use_container_width=True, hide_index=True)

        st.divider()
        st.subheader("Add entry")

        with st.form("form_add_alloc"):
            c1, c2 = st.columns(2)
            with c1:
                new_period = st.text_input("Period (YYYY-MM)", placeholder="2026-02")
            with c2:
                new_program = st.selectbox("Program", programs, key="add_program_sel")

            new_parent = parent_map.get(new_program, "")
            st.caption(f"Parent: {new_parent}")

            new_amount = st.number_input("Amount", min_value=0.0, step=0.01, format="%.2f")
            new_notes  = st.text_input("Notes (e.g. BOPP film Feb consumption JE)")

            if st.form_submit_button("Save"):
                if not new_period or not new_program or new_amount == 0:
                    st.error("Period, program and amount are required.")
                else:
                    upsert_alloc(engine, new_period, new_program, new_parent, new_amount, new_notes)
                    st.success(f"Saved: {new_program} | {new_period} | ${new_amount:,.2f}")
                    st.rerun()

        if not df_alloc.empty:
            st.divider()
            st.subheader("Edit or delete")

            row_labels = df_alloc.apply(
                lambda r: f"[{r['id']}] {r['period']} | {r['customer_program']} | ${r['amount']:,.2f}",
                axis=1,
            ).tolist()

            sel_label = st.selectbox("Select entry", row_labels, key="sel_alloc_edit")
            sel_idx   = row_labels.index(sel_label)
            sel_row   = df_alloc.iloc[sel_idx]

            with st.form("form_edit_alloc"):
                c1, c2 = st.columns(2)
                with c1:
                    edit_period = st.text_input("Period", value=sel_row["period"])
                with c2:
                    prog_idx    = programs.index(sel_row["customer_program"]) if sel_row["customer_program"] in programs else 0
                    edit_program = st.selectbox("Program", programs, index=prog_idx, key="edit_program_sel")

                edit_parent = parent_map.get(edit_program, sel_row["customer_parent"] or "")
                st.caption(f"Parent: {edit_parent}")

                edit_amount = st.number_input("Amount", value=float(sel_row["amount"]), step=0.01, format="%.2f")
                edit_notes  = st.text_input("Notes", value=sel_row["notes"] or "")

                c_save, c_del = st.columns(2)
                save_clicked   = c_save.form_submit_button("Update")
                delete_clicked = c_del.form_submit_button("Delete", type="secondary")

                if save_clicked:
                    upsert_alloc(engine, edit_period, edit_program, edit_parent,
                                 edit_amount, edit_notes, row_id=int(sel_row["id"]))
                    st.success("Updated.")
                    st.rerun()

                if delete_clicked:
                    delete_alloc(engine, int(sel_row["id"]))
                    st.warning(f"Deleted entry [{sel_row['id']}].")
                    st.rerun()

        st.divider()
        if st.button("Refresh MVs", key="refresh_raw_mvs"):
            with st.spinner("Refreshing..."):
                refresh_mvs(engine)
            st.success("MVs refreshed.")
            st.rerun()