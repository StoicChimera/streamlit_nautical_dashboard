"""
Preflight - Period Close Readiness

Renders two stacked panels:
  1. Data source freshness from vw_data_source_freshness (all 18 sources)
  2. Period-close manual step status from SQL probes (5 steps)

Reads from Supabase only. Drop into:
  apps/streamlit_nautical_dashboard/src/nautical_dashboard/app/modules/preflight.py

Then add to your dashboard's navigation. Render entry point is render().
"""

from __future__ import annotations

import os
from datetime import date

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    st.error("Missing SUPABASE_CONN environment variable.")
    st.stop()

engine = create_engine(SUPABASE_CONN, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def _status_pill(status: str) -> str:
    """Colored status pill rendered inline as HTML."""
    colors = {
        "fresh":     ("#0b5a25", "#c8e6c9"),
        "stale":     ("#92400e", "#fed7aa"),
        "never":     ("#374151", "#e5e7eb"),
        "done":      ("#0b5a25", "#c8e6c9"),
        "pending":   ("#92400e", "#fed7aa"),
        "configure": ("#374151", "#e5e7eb"),
    }
    fg, bg = colors.get(status.lower(), ("#374151", "#e5e7eb"))
    return (
        f'<span style="background:{bg};color:{fg};padding:3px 10px;'
        f'border-radius:10px;font-size:0.72rem;font-weight:700;'
        f'letter-spacing:0.04em;text-transform:uppercase;">{status}</span>'
    )


def _next_month(d: date) -> date:
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _prior_month(d: date) -> date:
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def _period_options() -> list[date]:
    out: list[date] = []
    cur = date(2025, 1, 1)
    today = date.today().replace(day=1)
    while cur <= today:
        out.append(cur)
        cur = _next_month(cur)
    return list(reversed(out))


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

@st.cache_data(ttl=60)
def load_freshness() -> pd.DataFrame:
    sql = text("""
        SELECT
            source_name,
            source_type,
            display_name,
            consuming_modules,
            expected_cadence_days,
            last_synced_at,
            last_row_count,
            last_status,
            freshness,
            sort_order
        FROM vw_data_source_freshness
        ORDER BY sort_order, source_name
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn)


def probe_manual_steps(period_start: date) -> pd.DataFrame:
    """
    SQL probes for the 5 manual period-close steps.

    Each probe runs in its own try/except. Probe failures show as 'configure'
    status without breaking the page so SQL can be refined later when actual
    schema names are confirmed.
    """
    next_period = _next_month(period_start)
    prior_period_str = _prior_month(period_start).strftime("%Y-%m")
    base_params = {"p": period_start, "n": next_period}

    probes = [
        (
            "OW Film usage JE entered",
            """
                SELECT COUNT(*) AS n
                FROM clean_qbo_journal_lines
                WHERE txn_date >= :p AND txn_date < :n
                  AND (
                       memo ILIKE '%OW Film%'
                    OR description ILIKE '%overwrap%'
                    OR account_name ILIKE '%OW Film%'
                  )
            """,
            base_params,
            "JE line(s) referencing OW Film/overwrap in period",
        ),
        (
            "E-Commerce programs flagged for period",
            """
                SELECT COUNT(*) AS n
                FROM stg_smartsheet_demo
                WHERE date >= :p AND date < :n
                  AND (
                       channel ILIKE '%ecom%'
                    OR channel ILIKE '%e-commerce%'
                  )
            """,
            base_params,
            "demo row(s) flagged as e-commerce in period",
        ),
        (
            "Receiving returns processed",
            """
                SELECT COUNT(*) AS n
                FROM stg_extensiv_receipts
                WHERE received_date >= :p AND received_date < :n
                  AND COALESCE(receipt_type, '') ILIKE '%return%'
            """,
            base_params,
            "return-type receipt(s) in period",
        ),
        (
            "Container unloads logged",
            """
                SELECT COUNT(*) AS n
                FROM stg_labor_temp
                WHERE accrual_period = TO_CHAR(CAST(:p AS date), 'YYYY-MM')
                  AND (
                       job_code ILIKE '%container%'
                    OR job_code ILIKE '%unload%'
                  )
            """,
            base_params,
            "container/unload labor entry(s) in period",
        ),
        (
            "Prior period WIP reviewed",
            """
                SELECT COUNT(*) AS n
                FROM data_source_sync_log
                WHERE source_name = 'wip_balance_review'
                  AND period = :prior_period
            """,
            {"prior_period": prior_period_str},
            f"review marker(s) recorded for {prior_period_str}",
        ),
    ]

    rows: list[dict] = []
    for step_name, sql, params, detail_label in probes:
        try:
            with engine.connect() as conn:
                row = conn.execute(text(sql), params).first()
            n = int(row.n) if row else 0
            rows.append({
                "step": step_name,
                "status": "done" if n > 0 else "pending",
                "detail": f"{n} {detail_label}",
            })
        except Exception as e:
            err = str(e).splitlines()[0][:140]
            rows.append({
                "step": step_name,
                "status": "configure",
                "detail": f"probe needs schema adjustment: {err}",
            })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def render() -> None:
    _inject_styles()

    st.title("Preflight - Period Close Readiness")
    st.caption(
        "Run before trusting profitability, labor allocation, or WIP numbers "
        "for a period. Green means good. Orange means do something."
    )

    options = _period_options()
    labels = [d.strftime("%B %Y") for d in options]

    col_period, _ = st.columns([1, 3])
    with col_period:
        sel = st.selectbox(
            "Period",
            range(len(options)),
            format_func=lambda i: labels[i],
            index=0,
            label_visibility="collapsed",
        )
    period_start = options[sel]
    st.caption(f"Selected period: **{labels[sel]}**")

    st.markdown("---")
    _render_freshness_panel()
    st.markdown("---")
    _render_manual_steps_panel(period_start, labels[sel])


def _render_freshness_panel() -> None:
    st.subheader("Data Source Freshness")
    st.caption(
        "Every ETL writes to data_source_sync_log on success. "
        "Anything stale means rerun that source before relying on downstream numbers."
    )

    df = load_freshness()
    if df.empty:
        st.warning("No data sources registered in dim_data_sources.")
        return

    fresh_n = int((df["freshness"] == "fresh").sum())
    stale_n = int((df["freshness"] == "stale").sum())
    never_n = int((df["freshness"] == "never").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Sources", len(df))
    c2.metric("Fresh", fresh_n)
    c3.metric("Stale", stale_n)
    c4.metric("Never Synced", never_n)

    out = df.copy()
    out["Status"] = out["freshness"].apply(_status_pill)
    out["Last Synced"] = (
        pd.to_datetime(out["last_synced_at"])
          .dt.strftime("%Y-%m-%d %H:%M")
          .fillna("--")
          .replace({"NaT": "--"})
    )
    out["Cadence"] = out["expected_cadence_days"].fillna(0).astype(int).astype(str) + " day(s)"
    out["Rows"] = (
        out["last_row_count"]
          .fillna(0)
          .astype(int)
          .map("{:,}".format)
    )
    out = out.rename(columns={
        "display_name": "Source",
        "source_type": "Type",
        "consuming_modules": "Used By",
    })[["Source", "Type", "Last Synced", "Rows", "Cadence", "Status", "Used By"]]

    st.markdown(
        out.to_html(escape=False, index=False, classes="preflight-tbl"),
        unsafe_allow_html=True,
    )


def _render_manual_steps_panel(period_start: date, period_label: str) -> None:
    st.subheader(f"Period-Close Manual Steps - {period_label}")
    st.caption(
        "Probe queries check whether each manual close step has been performed. "
        "'Configure' means the probe SQL needs schema adjustment for your data model."
    )

    df = probe_manual_steps(period_start)
    if df.empty:
        st.info("No manual steps configured.")
        return

    done_n      = int((df["status"] == "done").sum())
    pending_n   = int((df["status"] == "pending").sum())
    configure_n = int((df["status"] == "configure").sum())

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total Steps", len(df))
    c2.metric("Done", done_n)
    c3.metric("Pending", pending_n)
    c4.metric("Configure", configure_n)

    out = df.copy()
    out["Status"] = out["status"].apply(_status_pill)
    out = out.rename(columns={"step": "Step", "detail": "Detail"})[["Step", "Status", "Detail"]]

    st.markdown(
        out.to_html(escape=False, index=False, classes="preflight-tbl"),
        unsafe_allow_html=True,
    )


def _inject_styles() -> None:
    st.markdown(
        """
        <style>
        table.preflight-tbl {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin-top: 0.75rem;
            font-size: 0.88rem;
        }
        table.preflight-tbl thead th {
            background: #fafafa;
            font-weight: 600;
            text-align: left;
            padding: 10px 14px;
            border-bottom: 2px solid #e5e7eb;
            color: #374151;
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        table.preflight-tbl tbody td {
            padding: 10px 14px;
            border-bottom: 1px solid #f3f4f6;
            color: #1f2937;
            vertical-align: middle;
        }
        table.preflight-tbl tbody tr:hover {
            background: #fafbfc;
        }
        table.preflight-tbl tbody td:nth-child(4) {
            font-variant-numeric: tabular-nums;
            text-align: right;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    render()