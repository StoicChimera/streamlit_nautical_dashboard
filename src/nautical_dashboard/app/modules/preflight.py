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
from datetime import datetime
import pytz

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


def _status_pill(status: str) -> str:
    colors = {
        "fresh":     ("#0b5a25", "#c8e6c9"),
        "stale":     ("#92400e", "#fed7aa"),
        "never":     ("#374151", "#e5e7eb"),
        "done":      ("#0b5a25", "#c8e6c9"),
        "pending":   ("#92400e", "#fed7aa"),
        "configure": ("#374151", "#e5e7eb"),
        "blocked":   ("#9ca3af", "#f3f4f6"),  # grey on light grey
    }
    fg, bg = colors.get(status.lower(), ("#374151", "#e5e7eb"))
    return (
        f'<span style="background:{bg};color:{fg};padding:3px 10px;'
        f'border-radius:10px;font-size:0.72rem;font-weight:700;'
        f'letter-spacing:0.04em;text-transform:uppercase;'
        f'{"opacity:0.7;" if status == "blocked" else ""}">{status}</span>'
    )

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
    Two-pass probe runner with dependency-aware blocking.

    Pass 1: run every probe, collect raw done/pending/configure status.
    Pass 2: for dependent steps, if any depends_on is not 'done', flip to 'blocked'.
    """
    next_period = _next_month(period_start)
    period_str  = period_start.strftime("%Y-%m")
    prior_str   = _prior_month(period_start).strftime("%Y-%m")

    # Each probe declares: id, name, category, depends_on, sql, params, done_when
    # done_when: 'has_rows' (default) → done when n > 0
    #            'no_rows'             → done when n == 0 (used for unmatched-counters)
    steps = [
        {
            "id": "ow_film_je",
            "name": "OW Film usage JE entered",
            "category": "trigger",
            "depends_on": [],
            "sql": """
                SELECT COUNT(*) AS n
                FROM clean_qbo_journal_lines
                WHERE txn_date >= :p AND txn_date < :n
                  AND (description ILIKE '%overwrap%'
                    OR description ILIKE '%OW film%'
                    OR account_name ILIKE '%OW Film%')
            """,
            "params": {"p": period_start, "n": next_period},
            "label": "JE line(s) referencing OW Film/overwrap",
        },
        {
            "id": "ecomm_programs",
            "name": "E-Commerce programs flagged",
            "category": "trigger",
            "depends_on": [],
            "sql": """
                SELECT COUNT(*) AS n
                FROM stg_labor_ecomm_period_config
                WHERE accrual_period = :period_str AND active = TRUE
            """,
            "params": {"period_str": period_str},
            "label": "E-Commerce program(s) elected",
        },
        {
            "id": "receiving_returns",
            "name": "Receiving returns recorded",
            "category": "trigger",
            "depends_on": [],
            "sql": """
                SELECT COUNT(*) AS n
                FROM stg_labor_receiving_returns
                WHERE accrual_period = :period_str
            """,
            "params": {"period_str": period_str},
            "label": "return entry/entries recorded",
        },
        {
            "id": "container_unloads",
            "name": "Container unloads logged",
            "category": "trigger",
            "depends_on": [],
            "sql": """
                SELECT COUNT(*) AS n
                FROM stg_labor_container_unload
                WHERE accrual_period = :period_str
            """,
            "params": {"period_str": period_str},
            "label": "container unload entry/entries",
        },
        {
            "id": "freight_assigned",
            "name": "Freight assigned and approved",
            "category": "trigger",
            "depends_on": [],
            "sql": """
                SELECT COUNT(*) AS n
                FROM mv_wip_fulfillment_freight
                WHERE bill_date >= :p AND bill_date < :n
                  AND match_status = 'unmatched'
            """,
            "params": {"p": period_start, "n": next_period},
            "done_when": "no_rows",
            "label": "unmatched freight line(s) remaining",
        },
        {
            "id": "warehouse_allocations",
            "name": "Warehouse allocations committed",
            "category": "dependent",
            "depends_on": ["ecomm_programs", "receiving_returns", "container_unloads"],
            "sql": """
                SELECT COUNT(*) AS n
                FROM stg_warehouse_allocation
                WHERE month_start = :p
            """,
            "params": {"p": period_start},
            "label": "warehouse allocation row(s) committed",
        },
        {
            "id": "labor_committed",
            "name": "Labor allocation committed",
            "category": "dependent",
            "depends_on": ["ecomm_programs", "receiving_returns", "container_unloads"],
            "sql": """
                SELECT COUNT(*) AS n
                FROM stg_labor_allocation
                WHERE accrual_period = :period_str AND locked = TRUE
            """,
            "params": {"period_str": period_str},
            "label": "labor allocation row(s) locked",
        },
        {
            "id": "prior_wip_reviewed",
            "name": "Prior period WIP reviewed",
            "category": "dependent",
            "depends_on": [],
            "sql": """
                SELECT COUNT(*) AS n
                FROM data_source_sync_log
                WHERE source_name = 'wip_balance_review'
                  AND period = :prior_str
            """,
            "params": {"prior_str": prior_str},
            "label": f"review marker(s) for {prior_str}",
        },
    ]

    # --- Pass 1: run each probe ---
    raw_results: dict[str, dict] = {}
    for step in steps:
        try:
            with engine.connect() as conn:
                row = conn.execute(text(step["sql"]), step["params"]).first()
            n = int(row.n) if row else 0
            done_when = step.get("done_when", "has_rows")
            is_done = (n > 0) if done_when == "has_rows" else (n == 0)
            raw_results[step["id"]] = {
                "status": "done" if is_done else "pending",
                "detail": f"{n} {step['label']}",
            }
        except Exception as e:
            err = str(e).splitlines()[0][:140]
            raw_results[step["id"]] = {
                "status": "configure",
                "detail": f"probe needs schema adjustment: {err}",
            }

    # --- Pass 2: mark dependent steps as blocked if any dependency not done ---
    rows: list[dict] = []
    for step in steps:
        result = raw_results[step["id"]]
        blockers = [
            dep for dep in step["depends_on"]
            if raw_results.get(dep, {}).get("status") != "done"
        ]
        if blockers and result["status"] != "done":
            # Already-done dependents stay done — don't retro-block them.
            blocker_names = ", ".join(
                next(s["name"] for s in steps if s["id"] == b)
                for b in blockers
            )
            result = {
                "status": "blocked",
                "detail": f"blocked by: {blocker_names}",
            }
        depends_label = (
            ", ".join(next(s["name"] for s in steps if s["id"] == d) for d in step["depends_on"])
            if step["depends_on"] else "—"
        )
        rows.append({
            "step": step["name"],
            "status": result["status"],
            "detail": result["detail"],
            "depends_on": depends_label,
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
    col_title, col_refresh = st.columns([4, 1])
    with col_title:
        st.subheader("Data Source Freshness")
    with col_refresh:
        # Vertical alignment hack so button lines up with the subheader
        st.write("")
        if st.button("Refresh data", use_container_width=True):
            load_freshness.clear()
            st.rerun()
    st.caption(f"_Loaded: {datetime.now(pytz.timezone('US/Mountain')).strftime('%I:%M:%S %p MT')}_")

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
        "Probe queries check whether each period-close step is complete. "
        "Dependent steps are greyed out when their triggers are still pending."
    )

    df = probe_manual_steps(period_start)
    if df.empty:
        st.info("No manual steps configured.")
        return

    done_n      = int((df["status"] == "done").sum())
    pending_n   = int((df["status"] == "pending").sum())
    blocked_n   = int((df["status"] == "blocked").sum())
    configure_n = int((df["status"] == "configure").sum())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total Steps", len(df))
    c2.metric("Done", done_n)
    c3.metric("Pending", pending_n)
    c4.metric("Blocked", blocked_n)
    c5.metric("Configure", configure_n)

    out = df.copy()
    out["Status"] = out["status"].apply(_status_pill)
    out = out.rename(columns={
        "step": "Step",
        "detail": "Detail",
        "depends_on": "Depends On",
    })[["Step", "Status", "Detail", "Depends On"]]

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