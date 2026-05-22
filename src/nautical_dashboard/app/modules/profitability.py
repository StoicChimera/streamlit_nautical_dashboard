from __future__ import annotations

import os
import io
import tempfile
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

CONN_STRING = os.getenv("SUPABASE_CONN")

ALL_NUMERIC = [
    "revenue", "rev_weight", "billed_amount", "temp_labor", "direct_hire",
    "freight_storage", "raw_materials", "equipment", "commission",
    "applied_wh", "applied_sga", "gross_profit", "gp_margin",
    "net_profit", "net_margin",
]


@st.cache_resource
def get_engine():
    return create_engine(CONN_STRING)


@st.cache_data(ttl=300, show_spinner=False)
def load_customer_flags(_engine) -> dict[str, set]:
    df = pd.read_sql(text("""
        SELECT customer_name, is_experiential, is_scaas
        FROM dim_customer
        WHERE active = TRUE AND is_revenue_customer = TRUE
    """), _engine)
    return {
        "experiential": set(df[df["is_experiential"] == True]["customer_name"]),
        "scaas":        set(df[df["is_scaas"] == True]["customer_name"]),
    }


@st.cache_data(ttl=60, show_spinner=False)
def load_sga_breakdown(_engine, year: int, month: int) -> pd.DataFrame:
    """SG&A category totals for a single period."""
    period = f"{year}-{month:02d}"
    return pd.read_sql(
        text("""
            SELECT category, SUM(amount) AS total
            FROM vw_sga_transactions
            WHERE accrual_period = :period
            GROUP BY category
            ORDER BY total DESC
        """),
        _engine,
        params={"period": period},
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_sga_labor_total(_engine, year: int, month: int) -> float:
    """
    Total labor SG&A actually hitting current P&L for the period.

    Pulls from stg_labor_applied (the post-allocation truth) filtered to
    sources that hit the income statement (period_allocation, fifo,
    fulfillment_wip_applied), and joins to mv_program_profitability so we
    only count labor for programs with current-period revenue — matching
    how mv_program_profitability.applied_sga is computed.

    Stranded direct_sga labor (current_fulfillment_wip — no revenue
    program to absorb it) is excluded; it sits as WIP on the balance
    sheet and surfaces in the Labor Fulfillment WIP section.
    """
    period = f"{year}-{month:02d}"
    df = pd.read_sql(
        text("""
            SELECT COALESCE(SUM(la.applied_cost), 0) AS total
            FROM stg_labor_applied la
            WHERE la.accrual_period = :period
              AND la.labor_type = 'direct_sga'
              AND la.source IN ('period_allocation', 'fifo', 'fulfillment_wip_applied')
              AND EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start = TO_DATE(la.accrual_period, 'YYYY-MM')
                    AND mv.customer_program = la.program
              )
        """),
        _engine,
        params={"period": period},
    )
    return float(df["total"].iloc[0])

@st.cache_data(ttl=60, show_spinner=False)
def load_sga_warehouse_total(_engine, year: int, month: int) -> float:
    """Total warehouse SG&A allocation for the period."""
    month_start = pd.Timestamp(year=year, month=month, day=1).date()
    df = pd.read_sql(
        text("""
            SELECT COALESCE(SUM(allocation_amount), 0) AS total
            FROM stg_warehouse_allocation
            WHERE month_start = :m
              AND cost_type = 'sga'
        """),
        _engine,
        params={"m": month_start},
    )
    return float(df["total"].iloc[0])


def _render_consolidated_pnl(
    df: pd.DataFrame,
    sga_df: pd.DataFrame,
    labor_sga: float,
    warehouse_sga: float,
    period_label: str,
) -> None:
    """Standard income statement rollup across all programs for the period."""
    if df.empty:
        return

    revenue       = float(df["revenue"].sum())
    temp_labor    = float(df["temp_labor"].sum())
    direct_hire   = float(df["direct_hire"].sum())
    raw_materials = float(df["raw_materials"].sum())
    equipment     = float(df["equipment"].sum())
    commission    = float(df["commission"].sum())
    freight       = float(df["freight_storage"].sum())
    applied_wh    = float(df["applied_wh"].sum())
    total_cogs    = (temp_labor + direct_hire + raw_materials + equipment
                     + commission + freight + applied_wh)
    gross_profit  = revenue - total_cogs
    gp_margin     = gross_profit / revenue if revenue else 0

    if not sga_df.empty:
        sga_categories = [
            (str(r["category"]), float(r["total"]))
            for _, r in sga_df.iterrows()
        ]
    else:
        sga_categories = []

    total_sga  = sum(amt for _, amt in sga_categories) + labor_sga + warehouse_sga
    net_profit = gross_profit - total_sga
    net_margin = net_profit / revenue if revenue else 0

    def _pct(v: float) -> str:
        return f"{(v / revenue * 100):.1f}%" if revenue else "—"

    def _amt(v: float) -> str:
        return f"${v:,.2f}"

    rows = [
        ("Revenue",                revenue,       "100.0%"),
        ("", None, ""),
        ("Cost of Goods Sold",     None,          ""),
        ("  Temp Labor",           temp_labor,    _pct(temp_labor)),
        ("  Direct Hire",          direct_hire,   _pct(direct_hire)),
        ("  Raw Materials",        raw_materials, _pct(raw_materials)),
        ("  Equipment",            equipment,     _pct(equipment)),
        ("  Commission",           commission,    _pct(commission)),
        ("  Freight & Storage",    freight,       _pct(freight)),
        ("  Applied Warehouse",    applied_wh,    _pct(applied_wh)),
        ("Total COGS",             total_cogs,    _pct(total_cogs)),
        ("", None, ""),
        ("Gross Profit",           gross_profit,  f"{gp_margin*100:.1f}%"),
        ("", None, ""),
        ("Operating Expenses",     None,          ""),
    ]
    for cat, amt in sga_categories:
        rows.append((f"  {cat}", amt, _pct(amt)))
    if labor_sga:
        rows.append(("  Salaries (SG&A)",  labor_sga,     _pct(labor_sga)))
    if warehouse_sga:
        rows.append(("  Warehouse (SG&A)", warehouse_sga, _pct(warehouse_sga)))
    rows.extend([
        ("Total SG&A",             total_sga,     _pct(total_sga)),
        ("", None, ""),
        ("Net Profit",             net_profit,    f"{net_margin*100:.1f}%"),
    ])

    pnl_df = pd.DataFrame([
        {
            "Line Item":     label,
            "Amount":        _amt(amount) if amount is not None else "",
            "% of Revenue":  pct,
        }
        for label, amount, pct in rows
    ])

    st.subheader(f"Consolidated P&L — {period_label}")

    def _highlight_pnl(row):
        label = str(row["Line Item"])
        styles = [""] * len(row)
        if label in ("Revenue", "Gross Profit", "Net Profit", "Total COGS", "Total SG&A"):
            styles = ["font-weight: bold"] * len(row)
        if label in ("Gross Profit", "Net Profit"):
            try:
                amt = float(row["Amount"].replace("$", "").replace(",", ""))
                if amt < 0:
                    styles = ["font-weight: bold; color: red"] * len(row)
            except Exception:
                pass
        return styles

    styled = pnl_df.style.apply(_highlight_pnl, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True)


@st.cache_data(ttl=60, show_spinner=False)
def load_wip_summary_as_of(_engine, year: int, month: int) -> dict:
    """
    Returns WIP balances as-of the selected period end.
    All queries are gated to only include data up to and including
    the selected period — no future transactions leak in.
    """
    period_end = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)
    period_str = f"{year}-{month:02d}"

    # ----------------------------------------------------------------
    # 1. Labor Production WIP
    # Units produced but not yet consumed as of this period.
    # Only layers from periods <= selected period.
    # Units consumed = FIFO matches from periods <= selected period.
    # ----------------------------------------------------------------
    labor_production_wip = pd.read_sql(
        text("""
            WITH layers AS (
                SELECT 
                    l.accrual_period,
                    l.cost_center,
                    l.customer_program,
                    l.output_type,
                    l.iso_week,
                    l.units_produced,
                    l.cost_per_unit,
                    l.units_produced * l.cost_per_unit AS layer_pool
                FROM stg_wip_production_layers l
                WHERE TO_DATE(l.accrual_period, 'YYYY-MM') <= :period_end
            ),
            consumed AS (
                -- Source-of-truth consumption from fifo_applied, not units_remaining
                -- (units_remaining can be corrupted by unlock cycles that don't restore it)
                SELECT
                    f.cost_center,
                    f.iso_week_produced AS iso_week,
                    -- Normalize program names to handle 'Advantage - X' vs 'X' mismatch
                    CASE 
                        WHEN f.customer_program LIKE 'Advantage - %' 
                        THEN REPLACE(f.customer_program, 'Advantage - ', '')
                        ELSE f.customer_program 
                    END AS canonical_program,
                    SUM(f.units_applied) AS units_consumed,
                    SUM(f.applied_cost)  AS cost_consumed
                FROM stg_wip_fifo_applied f
                WHERE TO_DATE(f.accrual_period, 'YYYY-MM') <= :period_end
                GROUP BY 1, 2, 3
            )
            SELECT
                l.accrual_period,
                l.cost_center,
                l.customer_program,
                SUM(l.layer_pool)                                          AS labor_pool,
                SUM(l.units_produced)                                      AS units_produced,
                COALESCE(SUM(c.units_consumed), 0)                         AS units_consumed,
                SUM(l.units_produced) - COALESCE(SUM(c.units_consumed), 0) AS units_remaining,
                SUM(l.layer_pool) - COALESCE(SUM(c.cost_consumed), 0)      AS wip_balance
            FROM layers l
            LEFT JOIN consumed c
                ON c.cost_center = l.cost_center
               AND c.iso_week    = l.iso_week
               AND c.canonical_program = CASE 
                    WHEN l.customer_program LIKE 'Advantage - %' 
                    THEN REPLACE(l.customer_program, 'Advantage - ', '')
                    ELSE l.customer_program 
               END
            GROUP BY l.accrual_period, l.cost_center, l.customer_program
            HAVING SUM(l.layer_pool) - COALESCE(SUM(c.cost_consumed), 0) > 0
            ORDER BY 1, 2, 3
        """),
        _engine,
        params={"period_end": period_end.date()},
    )

    # ----------------------------------------------------------------
    # 2. Labor Fulfillment WIP
    # Period allocation rows with no revenue as of selected period.
    # ----------------------------------------------------------------
    labor_fulfillment_wip = pd.read_sql(
        text("""
            SELECT
                la.accrual_period,
                la.bucket       AS cost_center,
                la.program      AS customer_program,
                la.labor_type,
                SUM(la.applied_cost) AS wip_balance
            FROM stg_labor_applied la
            WHERE la.source = 'period_allocation'
              AND TO_DATE(la.accrual_period, 'YYYY-MM') <= :period_end
              AND NOT EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start <= :period_end
                    AND mv.customer_program = la.program
              )
              AND NOT EXISTS (
                  SELECT 1 FROM stg_labor_applied la2
                  WHERE la2.source = 'fulfillment_wip_applied'
                    AND TO_DATE(la2.accrual_period, 'YYYY-MM') <= :period_end
                    AND la2.program     = la.program
                    AND la2.bucket      = la.bucket
                    AND la2.labor_type  = la.labor_type
              )
            GROUP BY 1, 2, 3, 4
            ORDER BY 1, 3
        """),
        _engine,
        params={"period_end": period_end.date()},
    )

    # ----------------------------------------------------------------
    # 3. Warehouse WIP
    # Allocated but no revenue as of selected period.
    # ----------------------------------------------------------------
    warehouse_wip = pd.read_sql(
        text("""
            SELECT
                TO_CHAR(wa.month_start, 'YYYY-MM') AS accrual_period,
                wa.program_bucket,
                wa.customer_program,
                SUM(wa.allocation_amount)           AS wip_balance
            FROM stg_warehouse_allocation wa
            WHERE wa.month_start <= :period_end
              AND NOT EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start <= :period_end
                    AND mv.customer_program = wa.customer_program
              )
              AND NOT EXISTS (
                  SELECT 1 FROM stg_warehouse_wip_applied wwa
                  WHERE TO_DATE(wwa.accrual_period, 'YYYY-MM') <= :period_end
                    AND wwa.customer_program = wa.customer_program
                    AND wwa.program_bucket   = wa.program_bucket
              )
            GROUP BY 1, 2, 3
            ORDER BY 1, 3
        """),
        _engine,
        params={"period_end": period_end.date()},
    )

    # ----------------------------------------------------------------
    # 4. Freight WIP
    # Bills received on or before period end that are unmatched
    # or matched to a period after the selected period.
    # ----------------------------------------------------------------
    freight_wip = pd.read_sql(
        text("""
            SELECT
                customer_full_name  AS customer_program,
                invoice_num,
                bill_date,
                amount              AS wip_balance,
                match_status,
                recognized_period
            FROM mv_wip_fulfillment_freight
            WHERE bill_date <= :period_end
              AND (
                  match_status = 'unmatched'
                  OR recognized_period > :period_end
              )
            ORDER BY bill_date DESC
        """),
        _engine,
        params={"period_end": period_end.date()},
    )

    return {
        "labor_production":   labor_production_wip,
        "labor_fulfillment":  labor_fulfillment_wip,
        "warehouse":          warehouse_wip,
        "freight":            freight_wip,
        "period_end":         period_end,
        "period_str":         period_str,
    }


@st.cache_data(ttl=60, show_spinner=False)
def load_program_labor(_engine, program: str, year: int, month: int) -> pd.DataFrame:
    period = f"{year}-{month:02d}"
    return pd.read_sql(
        text("""
            SELECT
                source_bucket       AS cost_center,
                program,
                labor_type,
                activity_driver,
                activity_value,
                weight,
                allocated_cost
            FROM stg_labor_incurred
            WHERE accrual_period = :period
              AND program = :program
            ORDER BY labor_type, cost_center
        """),
        _engine,
        params={"period": period, "program": program},
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_program_labor_employees(_engine, program: str, year: int, month: int) -> pd.DataFrame:
    period = f"{year}-{month:02d}"
    return pd.read_sql(
        text("""
            SELECT
                source_bucket   AS cost_center,
                labor_type,
                labor_source,
                employee_name   AS employee,
                role_detail     AS role,
                activity_driver,
                activity_value,
                weight,
                allocated_cost
            FROM stg_labor_incurred_employee
            WHERE accrual_period = :period
              AND target_program  = :program
            ORDER BY labor_type, cost_center, employee_name
        """),
        _engine,
        params={"period": period, "program": program},
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_program_warehouse(_engine, program: str, year: int, month: int) -> pd.DataFrame:
    month_start = pd.Timestamp(year=year, month=month, day=1).date()
    return pd.read_sql(
        text("""
            SELECT
                program_bucket,
                category,
                cost_type,
                driver_type,
                driver_value,
                allocation_pct,
                allocation_amount
            FROM stg_warehouse_allocation
            WHERE month_start = :m
              AND customer_program = :program
            ORDER BY category, program_bucket
        """),
        _engine,
        params={"m": month_start, "program": program},
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_program_freight(_engine, program: str, year: int, month: int) -> pd.DataFrame:
    return pd.read_sql(
        text("""
            SELECT
                invoice_num,
                bill_date,
                line_description,
                amount,
                match_type,
                match_status,
                recognized_period
            FROM mv_wip_fulfillment_freight
            WHERE recognized_year  = :year
              AND recognized_month = :month
              AND TRIM(SPLIT_PART(customer_full_name, ':', 1)) ILIKE :program
              AND match_status != 'unmatched'
            ORDER BY bill_date
        """),
        _engine,
        params={"year": year, "month": month, "program": f"%{program}%"},
    )


@st.cache_data(ttl=60, show_spinner=False)
def load_program_wip(_engine, program: str, year: int, month: int) -> dict:
    period_end = pd.Timestamp(year=year, month=month, day=1) + pd.offsets.MonthEnd(0)

    labor_prod = pd.read_sql(
        text("""
            SELECT
                l.accrual_period, l.cost_center,
                SUM(l.labor_pool) AS labor_pool,
                SUM(l.units_produced) AS units_produced,
                COALESCE(SUM(c.units_consumed), 0) AS units_consumed,
                SUM(l.labor_pool) - SUM(
                    l.labor_pool * CASE WHEN l.units_produced > 0
                        THEN LEAST(COALESCE(c.units_consumed, 0) / l.units_produced, 1.0)
                        ELSE 1.0 END
                ) AS wip_balance
            FROM stg_wip_production_layers l
            LEFT JOIN (
                SELECT cost_center, customer_program, SUM(units_applied) AS units_consumed
                FROM stg_wip_fifo_applied
                WHERE TO_DATE(accrual_period, 'YYYY-MM') <= :period_end
                GROUP BY 1, 2
            ) c ON c.cost_center = l.cost_center AND c.customer_program = l.customer_program
            WHERE TO_DATE(l.accrual_period, 'YYYY-MM') <= :period_end
              AND l.customer_program = :program
            GROUP BY 1, 2
            HAVING SUM(l.labor_pool) - SUM(
                l.labor_pool * CASE WHEN l.units_produced > 0
                    THEN LEAST(COALESCE(c.units_consumed, 0) / l.units_produced, 1.0)
                    ELSE 1.0 END
            ) > 0
        """),
        _engine,
        params={"period_end": period_end.date(), "program": program},
    )

    warehouse_wip = pd.read_sql(
        text("""
            SELECT
                TO_CHAR(wa.month_start, 'YYYY-MM') AS period,
                wa.program_bucket,
                SUM(wa.allocation_amount) AS wip_balance
            FROM stg_warehouse_allocation wa
            WHERE wa.month_start <= :period_end
              AND wa.customer_program = :program
              AND NOT EXISTS (
                  SELECT 1 FROM mv_program_profitability mv
                  WHERE mv.month_start <= :period_end
                    AND mv.customer_program = wa.customer_program
              )
            GROUP BY 1, 2
        """),
        _engine,
        params={"period_end": period_end.date(), "program": program},
    )

    return {
        "labor_production": labor_prod,
        "warehouse":        warehouse_wip,
    }


def _get_labor_incurred(engine, year: int, month: int) -> pd.DataFrame:
    period = f"{year}-{month:02d}"
    return pd.read_sql(
        text("""
            SELECT source_bucket, program, labor_type,
                   activity_driver, activity_value, weight, allocated_cost
            FROM stg_labor_incurred
            WHERE accrual_period = :period
        """),
        engine,
        params={"period": period},
    )


def _get_warehouse_allocation(engine, year: int, month: int) -> pd.DataFrame:
    month_start = pd.Timestamp(year=year, month=month, day=1).date()
    return pd.read_sql(
        text("""
            SELECT customer_program, program_bucket, category, cost_type,
                   driver_type, driver_value, allocation_pct, allocation_amount,
                   bucket_sqft, total_sqft
            FROM stg_warehouse_allocation
            WHERE month_start = :m
        """),
        engine,
        params={"m": month_start},
    )


def _dollar_wip(v) -> str:
    try:
        return f"${float(v):,.2f}"
    except Exception:
        return str(v)
    

def _render_wip_summary(engine, year: int, month: int):
    st.divider()
    st.markdown(
        """
        <div style="background-color:#2E86C1;padding:8px;border-radius:4px;margin-bottom:12px;">
            <h3 style="color:white;margin:0;">WIP Balance Summary — As Of Period</h3>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.caption(
        "Outstanding WIP balances as of the selected period end. "
        "Includes only costs incurred on or before this period. "
        "Revenue recognition is gated to the same cutoff — no future periods leak in."
    )

    wip = load_wip_summary_as_of(engine, year, month)

    lp  = wip["labor_production"]
    lf  = wip["labor_fulfillment"]
    wh  = wip["warehouse"]
    fr  = wip["freight"]

    total_labor_production  = float(lp["wip_balance"].sum())  if not lp.empty else 0.0
    total_labor_fulfillment = float(lf["wip_balance"].sum())  if not lf.empty else 0.0
    total_warehouse         = float(wh["wip_balance"].sum())  if not wh.empty else 0.0
    total_freight           = float(fr["wip_balance"].sum())  if not fr.empty else 0.0
    total_wip               = total_labor_production + total_labor_fulfillment + total_warehouse + total_freight

    # Top line metrics
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total WIP Balance",      _dollar_wip(total_wip))
    m2.metric("Labor — Production",     _dollar_wip(total_labor_production))
    m3.metric("Labor — Fulfillment",    _dollar_wip(total_labor_fulfillment))
    m4.metric("Warehouse",              _dollar_wip(total_warehouse))
    m5.metric("Freight",                _dollar_wip(total_freight))

    st.markdown("")

    # ----------------------------------------------------------------
    # Labor Production WIP expander
    # ----------------------------------------------------------------
    with st.expander(
        f"Labor — Production WIP  {_dollar_wip(total_labor_production)}",
        expanded=False,
    ):
        if lp.empty:
            st.success("No outstanding production WIP as of this period.")
        else:
            display = lp.copy()
            display["wip_balance"]   = display["wip_balance"].map(_dollar_wip)
            display["labor_pool"]    = display["labor_pool"].map(_dollar_wip)
            display["units_produced"]  = display["units_produced"].map(lambda x: f"{float(x):,.0f}")
            display["units_consumed"]  = display["units_consumed"].map(lambda x: f"{float(x):,.0f}")
            display["units_remaining"] = display["units_remaining"].map(lambda x: f"{float(x):,.0f}")
            st.dataframe(
                display.rename(columns={
                    "accrual_period":   "Period",
                    "cost_center":      "Cost Center",
                    "customer_program": "Program",
                    "labor_pool":       "Labor Pool",
                    "units_produced":   "Units Produced",
                    "units_consumed":   "Units Consumed",
                    "units_remaining":  "Units Remaining",
                    "wip_balance":      "WIP Balance",
                }),
                use_container_width=True,
                hide_index=True,
            )

    # ----------------------------------------------------------------
    # Labor Fulfillment WIP expander
    # ----------------------------------------------------------------
    with st.expander(
        f"Labor — Fulfillment WIP  {_dollar_wip(total_labor_fulfillment)}",
        expanded=False,
    ):
        if lf.empty:
            st.success("No outstanding fulfillment WIP as of this period.")
        else:
            display = lf.copy()
            display["wip_balance"] = display["wip_balance"].map(_dollar_wip)
            st.dataframe(
                display.rename(columns={
                    "accrual_period":   "Period",
                    "cost_center":      "Cost Center",
                    "customer_program": "Program",
                    "labor_type":       "Labor Type",
                    "wip_balance":      "WIP Balance",
                }),
                use_container_width=True,
                hide_index=True,
            )

    # ----------------------------------------------------------------
    # Warehouse WIP expander
    # ----------------------------------------------------------------
    with st.expander(
        f"Warehouse WIP  {_dollar_wip(total_warehouse)}",
        expanded=False,
    ):
        if wh.empty:
            st.success("No outstanding warehouse WIP as of this period.")
        else:
            display = wh.copy()
            display["wip_balance"] = display["wip_balance"].map(_dollar_wip)
            st.dataframe(
                display.rename(columns={
                    "accrual_period":   "Period",
                    "program_bucket":   "Bucket",
                    "customer_program": "Program",
                    "wip_balance":      "WIP Balance",
                }),
                use_container_width=True,
                hide_index=True,
            )

    # ----------------------------------------------------------------
    # Freight WIP expander
    # ----------------------------------------------------------------
    with st.expander(
        f"Freight WIP  {_dollar_wip(total_freight)}",
        expanded=False,
    ):
        if fr.empty:
            st.success("No outstanding freight WIP as of this period.")
        else:
            display = fr.copy()
            display["wip_balance"] = display["wip_balance"].map(_dollar_wip)
            display["bill_date"]   = pd.to_datetime(display["bill_date"]).dt.strftime("%Y-%m-%d")
            display["recognized_period"] = display["recognized_period"].apply(
                lambda x: pd.Timestamp(x).strftime("%Y-%m") if pd.notna(x) else "Unmatched"
            )
            st.dataframe(
                display.rename(columns={
                    "customer_program":  "Customer",
                    "invoice_num":       "Invoice",
                    "bill_date":         "Bill Date",
                    "wip_balance":       "Amount",
                    "match_status":      "Status",
                    "recognized_period": "Matched To",
                }),
                use_container_width=True,
                hide_index=True,
            )


def load_profitability(engine, year: int, month: int) -> pd.DataFrame:
    df = pd.read_sql(
        text(
            """
            SELECT
                customer_program, customer_parent,
                revenue, rev_weight, billed_amount,
                temp_labor, direct_hire, freight_storage,
                raw_materials, equipment, commission,
                applied_wh, applied_sga,
                gross_profit, gp_margin, net_profit, net_margin
            FROM mv_program_profitability
            WHERE recognized_year  = :year
              AND recognized_month = :month
            ORDER BY revenue DESC
            """
        ),
        engine,
        params={"year": year, "month": month},
    )
    # Force numeric right here so everything downstream is clean
    for col in ALL_NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    return df


def load_available_months(engine) -> pd.DataFrame:
    return pd.read_sql(
        """
        SELECT DISTINCT recognized_year, recognized_month, month_start
        FROM mv_program_profitability
        ORDER BY month_start DESC
        """,
        engine,
    )


def _render_program_snapshot(engine, df: pd.DataFrame, year: int, month: int, month_label: str = ""):
    st.divider()
    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:8px;border-radius:4px;margin-bottom:12px;">
            <h3 style="color:white;margin:0;">Program Snapshot</h3>
        </div>
        """,
        unsafe_allow_html=True,
    )

    programs = sorted(df["customer_program"].dropna().unique().tolist())
    selected = st.selectbox("Select program", programs, key="snapshot_program_select")

    if not selected:
        return

    tab_pnl, tab_labor, tab_warehouse, tab_freight, tab_wip = st.tabs([
        "P&L", "Labor", "Warehouse", "Freight", "WIP"
    ])

    col_snap, _ = st.columns([2, 6])
    with col_snap:
        if st.button(f"Export Snapshot — {selected}", key="btn_export_snapshot"):
            from app.export.program_snapshot import build_program_snapshot

            with st.spinner("Building snapshot..."):
                snap_labor  = load_program_labor(engine, selected, year, month)
                snap_wh     = load_program_warehouse(engine, selected, year, month)
                snap_fr     = load_program_freight(engine, selected, year, month)
                snap_wip    = load_program_wip(engine, selected, year, month)
                pnl_row     = df[df["customer_program"] == selected].iloc[0] if not df[df["customer_program"] == selected].empty else None
                logo_path   = os.path.join(os.path.dirname(__file__), "../export/logo_nautical.png")

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                    tmp_path = f.name

                snap_emp = load_program_labor_employees(engine, selected, year, month)

                build_program_snapshot(
                    out_path          = tmp_path,
                    program           = selected,
                    period_label      = month_label,
                    pnl_row           = pnl_row,
                    labor_df          = snap_labor,
                    warehouse_df      = snap_wh,
                    freight_df        = snap_fr,
                    wip               = snap_wip,
                    labor_employee_df = snap_emp,
                    logo_path         = logo_path,
                )

                with open(tmp_path, "rb") as f:
                    pdf_bytes = f.read()
                os.unlink(tmp_path)

            st.download_button(
                label     = f"Download — {selected} {month_label}.pdf",
                data      = pdf_bytes,
                file_name = f"Snapshot_{selected.replace(' ','_')}_{year}_{month:02d}.pdf",
                mime      = "application/pdf",
            )

    # ---- P&L ----
    with tab_pnl:
        row = df[df["customer_program"] == selected].copy()
        if row.empty:
            st.info("No P&L data for this program.")
        else:
            r = row.iloc[0]
            pnl_data = {
                "Revenue":         r["revenue"],
                "Temp Labor":      r["temp_labor"],
                "Direct Hire":     r["direct_hire"],
                "Raw Materials":   r["raw_materials"],
                "Equipment":       r["equipment"],
                "Commission":      r["commission"],
                "Freight":         r["freight_storage"],
                "Warehouse (WH)":  r["applied_wh"],
                "Gross Profit":    r["gross_profit"],
                "GP Margin":       None,
                "Applied SGA":     r["applied_sga"],
                "Net Profit":      r["net_profit"],
                "Net Margin":      None,
            }
            rows = []
            for label, val in pnl_data.items():
                if label in ("GP Margin", "Net Margin"):
                    margin_val = r["gp_margin"] if label == "GP Margin" else r["net_margin"]
                    rows.append({"Line Item": label, "Amount": f"{float(margin_val)*100:.1f}%"})
                else:
                    color = "red" if float(val) < 0 else "black"
                    rows.append({"Line Item": label, "Amount": f"${float(val):,.2f}"})
            pnl_df = pd.DataFrame(rows)
            st.dataframe(pnl_df, use_container_width=True, hide_index=True)

    # ---- Labor ----
    with tab_labor:
        labor_df = load_program_labor(engine, selected, year, month)
        emp_df   = load_program_labor_employees(engine, selected, year, month)

        if labor_df.empty:
            st.info("No labor allocated to this program for the period.")
        else:
            total_labor = float(labor_df["allocated_cost"].sum())
            st.metric("Total Allocated Labor", f"${total_labor:,.2f}")

            for ltype, label in [("direct_cogs", "Direct Hire"), ("temp", "Temp"), ("direct_sga", "SGA")]:
                sub = labor_df[labor_df["labor_type"] == ltype].copy()
                if sub.empty:
                    continue
                with st.expander(f"{label} — ${float(sub['allocated_cost'].sum()):,.2f}", expanded=True):
                    sub["allocated_cost"] = sub["allocated_cost"].map(lambda x: f"${float(x):,.2f}")
                    sub["weight"]         = sub["weight"].map(lambda x: f"{float(x):.2%}" if pd.notna(x) else "")
                    sub["activity_value"] = sub["activity_value"].map(lambda x: f"{float(x):,.2f}" if pd.notna(x) else "")
                    st.dataframe(
                        sub.rename(columns={
                            "cost_center":      "Cost Center",
                            "labor_type":       "Type",
                            "activity_driver":  "Driver",
                            "activity_value":   "Driver Value",
                            "weight":           "Weight",
                            "allocated_cost":   "Allocated Cost",
                        }).drop(columns=["program"], errors="ignore"),
                        use_container_width=True,
                        hide_index=True,
                    )

                    # Employee detail
                    if not emp_df.empty:
                        emp_sub = emp_df[emp_df["labor_type"] == ltype].copy()
                        if not emp_sub.empty:
                            st.markdown("**Employee Detail**")
                            emp_sub["allocated_cost"] = emp_sub["allocated_cost"].map(lambda x: f"${float(x):,.2f}")
                            emp_sub["weight"]         = emp_sub["weight"].map(lambda x: f"{float(x):.2%}" if pd.notna(x) else "")
                            emp_sub["activity_value"] = emp_sub["activity_value"].map(lambda x: f"{float(x):,.2f}" if pd.notna(x) else "")
                            st.dataframe(
                                emp_sub.rename(columns={
                                    "employee":        "Employee",
                                    "role":            "Role",
                                    "cost_center":     "Cost Center",
                                    "labor_source":    "Source",
                                    "activity_driver": "Driver",
                                    "activity_value":  "Driver Value",
                                    "weight":          "Weight",
                                    "allocated_cost":  "Allocated Cost",
                                }).drop(columns=["labor_type"], errors="ignore"),
                                use_container_width=True,
                                hide_index=True,
                            )

    # ---- Warehouse ----
    with tab_warehouse:
        wh_df = load_program_warehouse(engine, selected, year, month)
        if wh_df.empty:
            st.info("No warehouse cost allocated to this program for the period.")
        else:
            total_wh = float(wh_df["allocation_amount"].sum())
            st.metric("Total Warehouse Cost", f"${total_wh:,.2f}")
            display = wh_df.copy()
            display["allocation_amount"] = display["allocation_amount"].map(lambda x: f"${float(x):,.2f}")
            display["allocation_pct"] = display["allocation_pct"].map(lambda x: f"{float(x):.2%}")
            display["driver_value"] = display["driver_value"].map(lambda x: f"{float(x):,.2f}" if pd.notna(x) else "")
            st.dataframe(
                display.rename(columns={
                    "program_bucket":    "Bucket",
                    "category":         "Category",
                    "cost_type":        "Cost Type",
                    "driver_type":      "Driver",
                    "driver_value":     "Driver Value",
                    "allocation_pct":   "Alloc %",
                    "allocation_amount":"Allocated",
                }),
                use_container_width=True,
                hide_index=True,
            )

    # ---- Freight ----
    with tab_freight:
        fr_df = load_program_freight(engine, selected, year, month)
        if fr_df.empty:
            st.info("No matched freight lines for this program in the period.")
        else:
            total_fr = float(fr_df["amount"].sum())
            st.metric("Total Freight", f"${total_fr:,.2f}")
            display = fr_df.copy()
            display["amount"] = display["amount"].map(lambda x: f"${float(x):,.2f}")
            st.dataframe(
                display.rename(columns={
                    "invoice_num":       "Invoice",
                    "bill_date":         "Bill Date",
                    "line_description":  "Description",
                    "amount":            "Amount",
                    "match_type":        "Match Type",
                    "match_status":      "Status",
                    "recognized_period": "Period",
                }),
                use_container_width=True,
                hide_index=True,
            )

    # ---- WIP ----
    with tab_wip:
        wip = load_program_wip(engine, selected, year, month)
        lp = wip["labor_production"]
        wh = wip["warehouse"]

        total_lp = float(lp["wip_balance"].sum()) if not lp.empty else 0.0
        total_wh = float(wh["wip_balance"].sum()) if not wh.empty else 0.0
        total    = total_lp + total_wh

        if total == 0:
            st.success("No outstanding WIP for this program as of this period.")
        else:
            st.metric("Total WIP Balance", f"${total:,.2f}")

            if not lp.empty:
                st.markdown("**Labor Production WIP**")
                display = lp.copy()
                display["wip_balance"] = display["wip_balance"].map(lambda x: f"${float(x):,.2f}")
                display["labor_pool"]  = display["labor_pool"].map(lambda x: f"${float(x):,.2f}")
                st.dataframe(display.rename(columns={
                    "accrual_period": "Period",
                    "cost_center":    "Cost Center",
                    "labor_pool":     "Labor Pool",
                    "units_produced": "Units Produced",
                    "units_consumed": "Units Consumed",
                    "wip_balance":    "WIP Balance",
                }), use_container_width=True, hide_index=True)

            if not wh.empty:
                st.markdown("**Warehouse WIP**")
                display = wh.copy()
                display["wip_balance"] = display["wip_balance"].map(lambda x: f"${float(x):,.2f}")
                st.dataframe(display.rename(columns={
                    "period":         "Period",
                    "program_bucket": "Bucket",
                    "wip_balance":    "WIP Balance",
                }), use_container_width=True, hide_index=True)


def render():
    st.markdown(
        """
        <div style="background-color:#1f77b4;padding:12px;border-radius:6px;margin-bottom:20px;">
            <h2 style="color:white;margin:0;">Customer Profit Analysis</h2>
        </div>
        """,
        unsafe_allow_html=True,
    )

    engine = get_engine()

    months_df = load_available_months(engine)
    if months_df.empty:
        st.warning("No profitability data available yet.")
        return

    month_labels = months_df.apply(
        lambda r: pd.Timestamp(r["month_start"]).strftime("%B %Y"), axis=1
    ).tolist()

    selected_label = st.selectbox("Select period", month_labels)
    selected_idx   = month_labels.index(selected_label)
    selected_row   = months_df.iloc[selected_idx]
    year           = int(selected_row["recognized_year"])
    month          = int(selected_row["recognized_month"])
    month_label    = pd.Timestamp(selected_row["month_start"]).strftime("%B %Y")

    col_refresh, col_export, col_spacer = st.columns([2, 2, 4])
    with col_refresh:
        if st.button("Refresh View"):
            with st.spinner("Refreshing materialized views..."):
                with engine.begin() as conn:
                    conn.execute(text("REFRESH MATERIALIZED VIEW mv_raw_materials_by_program"))
                    conn.execute(text("REFRESH MATERIALIZED VIEW mv_equipment_by_program"))
                    conn.execute(text("REFRESH MATERIALIZED VIEW mv_commission_by_program"))
                    conn.execute(text("REFRESH MATERIALIZED VIEW mv_sga_by_program"))
                    conn.execute(text("REFRESH MATERIALIZED VIEW mv_program_profitability"))
                    conn.execute(text("REFRESH MATERIALIZED VIEW mv_freight_by_program"))
            st.cache_data.clear()
            st.rerun()

    with col_export:
        if st.button("Export Report Package"):
            from app.export.profitability_report import build_profitability_report
            from app.export.program_snapshot import build_program_snapshot

            with st.spinner("Building report..."):
                flags       = load_customer_flags(engine)
                df_full     = load_profitability(engine, year, month)
                exp_df      = df_full[df_full["customer_program"].isin(flags["experiential"])].copy()
                scaas_df    = df_full[df_full["customer_program"].isin(flags["scaas"])].copy()
                prod_df     = df_full[df_full["customer_program"].isin({"Arrived Co","RECESS Digital Inc."})].copy()
                other_df    = df_full[~df_full["customer_program"].isin(
                    flags["experiential"] | flags["scaas"] | {"Arrived Co","RECESS Digital Inc."}
                )].copy()
                wip         = load_wip_summary_as_of(engine, year, month)
                wh_df       = _get_warehouse_allocation(engine, year, month)
                labor_df    = _get_labor_incurred(engine, year, month)
                logo_path   = os.path.join(os.path.dirname(__file__), "../export/logo_nautical.png")

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                    tmp_path = f.name

                _period = f"{year}-{month:02d}"
                emp_df = pd.read_sql(
                    text("""
                        SELECT
                            source_bucket,
                            target_program,
                            labor_type,
                            employee_name,
                            labor_source,
                            role_detail,
                            activity_driver,
                            activity_value,
                            weight,
                            allocated_cost
                        FROM stg_labor_incurred_employee
                        WHERE accrual_period = :period
                    """),
                    engine,
                    params={"period": _period},
                )
                period_start = pd.Timestamp(year=year, month=month, day=1)
                start_3mo    = (period_start - pd.DateOffset(months=2)).date()
                end_3mo      = (period_start + pd.offsets.MonthEnd(0)).date()

                # --- SG&A 3 month breakdown ---
                sga_breakdown_df = pd.read_sql(
                    text("""
                        SELECT
                            category,
                            TO_CHAR(txn_date, 'YYYY-MM') AS period,
                            SUM(amount) AS total
                        FROM vw_sga_transactions
                        WHERE txn_date BETWEEN :start_3mo AND :end_3mo
                        GROUP BY 1, 2
                        ORDER BY 1, 2
                    """),
                    engine,
                    params={"start_3mo": start_3mo, "end_3mo": end_3mo},
                )

                sga_breakdown_df = (
                    sga_breakdown_df
                    .pivot(index="category", columns="period", values="total")
                    .fillna(0)
                )

                # force month order
                month_cols = sorted([c for c in sga_breakdown_df.columns if c != "Total"])
                sga_breakdown_df = sga_breakdown_df.reindex(columns=month_cols)

                sga_breakdown_df["Total"] = sga_breakdown_df.sum(axis=1)
                sga_breakdown_df = (
                    sga_breakdown_df
                    .sort_values("Total", ascending=False)
                    .reset_index()
                )

                # --- Production 3 month activity ---
                prod_activity_df = pd.read_sql(
                    text("""
                        SELECT
                            'Demo Kits' AS activity_type,
                            TO_CHAR(today_s_date::date, 'YYYY-MM') AS period,
                            SUM(number_of_cases_completed) AS units
                        FROM stg_smartsheet_demo
                        WHERE today_s_date IS NOT NULL
                        AND TRIM(today_s_date) <> ''
                        AND today_s_date::date BETWEEN :start_3mo AND :end_3mo
                        AND number_of_cases_completed IS NOT NULL
                        GROUP BY 1, 2

                        UNION ALL

                        SELECT
                            'OGP Units' AS activity_type,
                            TO_CHAR(date::date, 'YYYY-MM') AS period,
                            SUM(daily_production_complete) AS units
                        FROM stg_smartsheet_ogp
                        WHERE date IS NOT NULL
                        AND date::date BETWEEN :start_3mo AND :end_3mo
                        AND daily_production_complete IS NOT NULL
                        GROUP BY 1, 2

                        UNION ALL

                        SELECT
                            'Overwrap Units' AS activity_type,
                            TO_CHAR(date_started::date, 'YYYY-MM') AS period,
                            SUM(units_produced) AS units
                        FROM stg_smartsheet_overwrap
                        WHERE date_started IS NOT NULL
                        AND date_started::date BETWEEN :start_3mo AND :end_3mo
                        AND units_produced IS NOT NULL
                        GROUP BY 1, 2
                    """),
                    engine,
                    params={"start_3mo": start_3mo, "end_3mo": end_3mo},
                )

                production_activity_3mo_df = (
                    prod_activity_df
                    .pivot(index="activity_type", columns="period", values="units")
                    .fillna(0)
                )

                # force month order
                month_cols = sorted(production_activity_3mo_df.columns.tolist())
                production_activity_3mo_df = production_activity_3mo_df.reindex(columns=month_cols)

                # add simple MoM from last 2 visible months
                if len(month_cols) >= 2:
                    latest = month_cols[-1]
                    prior  = month_cols[-2]
                    production_activity_3mo_df["MoM Δ"] = production_activity_3mo_df[latest] - production_activity_3mo_df[prior]
                    production_activity_3mo_df["MoM %"] = production_activity_3mo_df.apply(
                        lambda r: (r[latest] / r[prior] - 1) if r[prior] not in (0, None) else 0,
                        axis=1,
                    )
                else:
                    production_activity_3mo_df["MoM Δ"] = 0
                    production_activity_3mo_df["MoM %"] = 0

                production_activity_3mo_df = production_activity_3mo_df.reset_index()

                build_profitability_report(
                    out_path        = tmp_path,
                    period_label    = month_label,
                    full_df         = df_full,
                    experiential_df = exp_df,
                    scaas_df        = scaas_df,
                    production_df   = prod_df,
                    other_df        = other_df,

                    # NEW
                    sga_breakdown_df          = sga_breakdown_df,
                    production_activity_3mo_df= production_activity_3mo_df,

                    wip             = wip,
                    warehouse_df    = wh_df,
                    direct_hire_df  = labor_df,
                    temp_df         = labor_df,
                    employee_df     = emp_df,
                    logo_path       = logo_path,
                )

                with open(tmp_path, "rb") as f:
                    pdf_bytes = f.read()
                os.unlink(tmp_path)

            st.download_button(
                label    = f"Download — {month_label}.pdf",
                data     = pdf_bytes,
                file_name = f"Profitability_Report_{year}_{month:02d}.pdf",
                mime     = "application/pdf",
            )

    df = load_profitability(engine, year, month)
    if df.empty:
        st.warning("No data for this period.")
        return

    sga_breakdown       = load_sga_breakdown(engine, year, month)
    sga_labor_total     = load_sga_labor_total(engine, year, month)
    sga_warehouse_total = load_sga_warehouse_total(engine, year, month)

    # ── Consolidated P&L ────────────────────────────────────────────────────
    st.divider()
    
    _render_consolidated_pnl(
        df, sga_breakdown, sga_labor_total, sga_warehouse_total, month_label
    )

    st.divider()

        # ── Grouped bar chart ───────────────────────────────────────────────────
    chart_df = (
        df.groupby("customer_program", dropna=False)[
            ["billed_amount", "gross_profit", "net_profit"]
        ]
        .sum()
        .rename(columns={
            "billed_amount": "Billed Amount",
            "gross_profit":  "GP",
            "net_profit":    "Net",
        })
        .sort_values("Billed Amount", ascending=False)
    )

    fig = go.Figure()
    fig.add_trace(go.Bar(name="Billed Amount", x=chart_df.index, y=chart_df["Billed Amount"]))
    fig.add_trace(go.Bar(name="GP",            x=chart_df.index, y=chart_df["GP"]))
    fig.add_trace(go.Bar(name="Net",           x=chart_df.index, y=chart_df["Net"]))
    fig.update_layout(
        barmode="group",
        height=450,
        title=f"Program Profitability — {month_label}",
        yaxis_tickformat="$,.0f",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        xaxis_title="Program",
        yaxis_title="Amount",
    )
    st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ── Summary metrics ─────────────────────────────────────────────────────
    total_rev  = df["revenue"].sum()
    total_cogs = df[["temp_labor","direct_hire","freight_storage",
                      "raw_materials","equipment","commission"]].sum().sum()
    total_gp   = df["gross_profit"].sum()
    total_net  = df["net_profit"].sum()
    total_wh   = df["applied_wh"].sum()
    total_sga  = df["applied_sga"].sum()

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Revenue",      f"${total_rev:,.0f}")
    c2.metric("Total COGS",   f"${total_cogs:,.0f}")
    c3.metric("Applied WH",   f"${total_wh:,.0f}")
    c4.metric("Applied SG&A", f"${total_sga:,.0f}")
    c5.metric("Gross Profit", f"${total_gp:,.0f}",
              delta=f"{total_gp/total_rev*100:.1f}%" if total_rev else None)
    c6.metric("Net Profit",   f"${total_net:,.0f}",
              delta=f"{total_net/total_rev*100:.1f}%" if total_rev else None)

    # ── Detail table ────────────────────────────────────────────────────────
    totals = pd.DataFrame([{
        "customer_program": "TOTAL",
        "customer_parent":  "",
        "revenue":          df["revenue"].sum(),
        "rev_weight":       df["rev_weight"].sum(),
        "billed_amount":    df["billed_amount"].sum(),
        "temp_labor":       df["temp_labor"].sum(),
        "direct_hire":      df["direct_hire"].sum(),
        "freight_storage":  df["freight_storage"].sum(),
        "raw_materials":    df["raw_materials"].sum(),
        "equipment":        df["equipment"].sum(),
        "commission":       df["commission"].sum(),
        "applied_wh":       df["applied_wh"].sum(),
        "applied_sga":      df["applied_sga"].sum(),
        "gross_profit":     df["gross_profit"].sum(),
        "gp_margin":        df["gross_profit"].sum() / total_rev if total_rev else 0,
        "net_profit":       df["net_profit"].sum(),
        "net_margin":       df["net_profit"].sum() / total_rev if total_rev else 0,
    }])

    df = df.sort_values(["customer_parent", "revenue"], ascending=[True, False])
    display = df.copy()

    # Convert ratios to % for display
    display["rev_weight"] = display["rev_weight"] * 100
    display["gp_margin"]  = display["gp_margin"]  * 100
    display["net_margin"] = display["net_margin"] * 100

    # Final numeric pass on display (catches totals row)
    for col in ALL_NUMERIC:
        display[col] = pd.to_numeric(display[col], errors="coerce").fillna(0)

    dollar_cols = [
            "revenue", "billed_amount", "temp_labor", "direct_hire",
            "freight_storage", "raw_materials", "equipment", "commission",
            "applied_wh", "applied_sga", "gross_profit", "net_profit",
        ]
    for col in dollar_cols:
        display[col] = display[col].apply(lambda x: f"${x:,.2f}")

    display["rev_weight"] = display["rev_weight"].apply(lambda x: f"{x:.2f}%")
    display["gp_margin"]  = display["gp_margin"].apply(lambda x: f"{x:.1f}%")
    display["net_margin"] = display["net_margin"].apply(lambda x: f"{x:.1f}%")

    display = display.rename(columns={
        "customer_program": "Program",
        "customer_parent":  "Parent",
        "revenue":          "Revenue",
        "rev_weight":       "Rev Weight",
        "billed_amount":    "Billed Amount",
        "temp_labor":       "Temp Labor",
        "direct_hire":      "Direct Hire",
        "freight_storage":  "Freight & Storage",
        "raw_materials":    "Raw Materials",
        "equipment":        "Equipment",
        "commission":       "Commission",
        "applied_wh":       "Applied WH",
        "applied_sga":      "Applied SG&A",
        "gross_profit":     "Gross Profit",
        "gp_margin":        "GP Margin",
        "net_profit":       "Net Profit",
        "net_margin":       "Net Margin",
    })

    def highlight_negative(val):
        try:
            num = float(str(val).replace("$","").replace(",","").replace("%",""))
            return "color: red" if num < 0 else ""
        except:
            return ""

    styled = display.style.applymap(
        highlight_negative,
        subset=["Gross Profit", "Net Profit", "GP Margin", "Net Margin"]
    )

    st.dataframe(styled, use_container_width=True, hide_index=True)

    # Totals row pinned below
    totals_display = totals.copy()
    totals_display["rev_weight"] = totals_display["rev_weight"] * 100
    totals_display["gp_margin"]  = totals_display["gp_margin"]  * 100
    totals_display["net_margin"] = totals_display["net_margin"] * 100
    for col in dollar_cols:
        totals_display[col] = totals_display[col].apply(lambda x: f"${x:,.2f}")
    totals_display["rev_weight"] = totals_display["rev_weight"].apply(lambda x: f"{x:.2f}%")
    totals_display["gp_margin"]  = totals_display["gp_margin"].apply(lambda x: f"{x:.1f}%")
    totals_display["net_margin"] = totals_display["net_margin"].apply(lambda x: f"{x:.1f}%")
    totals_display = totals_display.rename(columns={
        "customer_program": "Program", "customer_parent": "Parent",
        "revenue": "Revenue", "rev_weight": "Rev Weight",
        "billed_amount": "Billed Amount", "temp_labor": "Temp Labor",
        "direct_hire": "Direct Hire", "freight_storage": "Freight & Storage",
        "raw_materials": "Raw Materials", "equipment": "Equipment",
        "commission": "Commission", "applied_wh": "Applied WH",
        "applied_sga": "Applied SG&A", "gross_profit": "Gross Profit",
        "gp_margin": "GP Margin", "net_profit": "Net Profit",
        "net_margin": "Net Margin",
    })
    st.dataframe(
        totals_display.style.applymap(
            highlight_negative,
            subset=["Gross Profit", "Net Profit", "GP Margin", "Net Margin"]
        ),
        use_container_width=True,
        hide_index=True,
    )

    # ── Experiential breakdown ───────────────────────────────────────────────
    flags = load_customer_flags(engine)

    st.divider()
    st.markdown("### Experiential Programs")
    exp_df = df[df["Program"].isin(flags["experiential"])].copy() if "Program" in df.columns else pd.DataFrame()

    # re-load raw numeric df for sub-tables since display df is already formatted
    exp_raw = load_profitability(engine, year, month)
    exp_raw = exp_raw[exp_raw["customer_program"].isin(flags["experiential"])].copy()

    if exp_raw.empty:
        st.info("No experiential program data for this period.")
    else:
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Revenue",      f"${exp_raw['revenue'].sum():,.0f}")
        e2.metric("Gross Profit", f"${exp_raw['gross_profit'].sum():,.0f}")
        e3.metric("GP Margin",    f"{exp_raw['gross_profit'].sum() / exp_raw['revenue'].sum() * 100:.1f}%" if exp_raw['revenue'].sum() else "—")
        e4.metric("Net Profit",   f"${exp_raw['net_profit'].sum():,.0f}")

        exp_display = exp_raw.sort_values("revenue", ascending=False).copy()
        for col in ["revenue","billed_amount","temp_labor","direct_hire",
                    "freight_storage","raw_materials","equipment","commission",
                    "applied_wh","applied_sga","gross_profit","net_profit"]:
            exp_display[col] = exp_display[col].apply(lambda x: f"${x:,.2f}")
        exp_display["gp_margin"]  = exp_display["gp_margin"].apply(lambda x: f"{x*100:.1f}%")
        exp_display["net_margin"] = exp_display["net_margin"].apply(lambda x: f"{x*100:.1f}%")
        exp_display["rev_weight"] = exp_display["rev_weight"].apply(lambda x: f"{x*100:.2f}%")
        exp_display = exp_display.rename(columns={
            "customer_program": "Program", "customer_parent": "Parent",
            "revenue": "Revenue", "rev_weight": "Rev Weight",
            "billed_amount": "Billed Amount", "temp_labor": "Temp Labor",
            "direct_hire": "Direct Hire", "freight_storage": "Freight & Storage",
            "raw_materials": "Raw Materials", "equipment": "Equipment",
            "commission": "Commission", "applied_wh": "Applied WH",
            "applied_sga": "Applied SG&A", "gross_profit": "Gross Profit",
            "gp_margin": "GP Margin", "net_profit": "Net Profit",
            "net_margin": "Net Margin",
        })
        st.dataframe(exp_display.style.applymap(
            highlight_negative,
            subset=["Gross Profit", "Net Profit", "GP Margin", "Net Margin"]
        ), use_container_width=True, hide_index=True)

    # ── SCAAS breakdown ──────────────────────────────────────────────────────
    st.divider()
    st.markdown("### SCAAS Programs")
    scaas_raw = load_profitability(engine, year, month)
    scaas_raw = scaas_raw[scaas_raw["customer_program"].isin(flags["scaas"])].copy()

    if scaas_raw.empty:
        st.info("No SCAAS program data for this period.")
    else:
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Revenue",      f"${scaas_raw['revenue'].sum():,.0f}")
        s2.metric("Gross Profit", f"${scaas_raw['gross_profit'].sum():,.0f}")
        s3.metric("GP Margin",    f"{scaas_raw['gross_profit'].sum() / scaas_raw['revenue'].sum() * 100:.1f}%" if scaas_raw['revenue'].sum() else "—")
        s4.metric("Net Profit",   f"${scaas_raw['net_profit'].sum():,.0f}")

        scaas_display = scaas_raw.sort_values("revenue", ascending=False).copy()
        for col in ["revenue","billed_amount","temp_labor","direct_hire",
                    "freight_storage","raw_materials","equipment","commission",
                    "applied_wh","applied_sga","gross_profit","net_profit"]:
            scaas_display[col] = scaas_display[col].apply(lambda x: f"${x:,.2f}")
        scaas_display["gp_margin"]  = scaas_display["gp_margin"].apply(lambda x: f"{x*100:.1f}%")
        scaas_display["net_margin"] = scaas_display["net_margin"].apply(lambda x: f"{x*100:.1f}%")
        scaas_display["rev_weight"] = scaas_display["rev_weight"].apply(lambda x: f"{x*100:.2f}%")
        scaas_display = scaas_display.rename(columns={
            "customer_program": "Program", "customer_parent": "Parent",
            "revenue": "Revenue", "rev_weight": "Rev Weight",
            "billed_amount": "Billed Amount", "temp_labor": "Temp Labor",
            "direct_hire": "Direct Hire", "freight_storage": "Freight & Storage",
            "raw_materials": "Raw Materials", "equipment": "Equipment",
            "commission": "Commission", "applied_wh": "Applied WH",
            "applied_sga": "Applied SG&A", "gross_profit": "Gross Profit",
            "gp_margin": "GP Margin", "net_profit": "Net Profit",
            "net_margin": "Net Margin",
        })
        st.dataframe(scaas_display.style.applymap(
            highlight_negative,
            subset=["Gross Profit", "Net Profit", "GP Margin", "Net Margin"]
        ), use_container_width=True, hide_index=True)

        # ── Production / Assembly breakdown ─────────────────────────────────────
    st.divider()
    st.markdown("### Production Programs")
    PRODUCTION_PROGRAMS = {"Arrived Co", "RECESS Digital Inc."}
    prod_raw = load_profitability(engine, year, month)
    prod_raw = prod_raw[prod_raw["customer_program"].isin(PRODUCTION_PROGRAMS)].copy()

    if prod_raw.empty:
        st.info("No production program data for this period.")
    else:
        p1, p2, p3, p4 = st.columns(4)
        p1.metric("Revenue",      f"${prod_raw['revenue'].sum():,.0f}")
        p2.metric("Gross Profit", f"${prod_raw['gross_profit'].sum():,.0f}")
        p3.metric("GP Margin",    f"{prod_raw['gross_profit'].sum() / prod_raw['revenue'].sum() * 100:.1f}%" if prod_raw['revenue'].sum() else "—")
        p4.metric("Net Profit",   f"${prod_raw['net_profit'].sum():,.0f}")

        prod_display = prod_raw.sort_values("revenue", ascending=False).copy()
        for col in ["revenue","billed_amount","temp_labor","direct_hire",
                    "freight_storage","raw_materials","equipment","commission",
                    "applied_wh","applied_sga","gross_profit","net_profit"]:
            prod_display[col] = prod_display[col].apply(lambda x: f"${x:,.2f}")
        prod_display["gp_margin"]  = prod_display["gp_margin"].apply(lambda x: f"{x*100:.1f}%")
        prod_display["net_margin"] = prod_display["net_margin"].apply(lambda x: f"{x*100:.1f}%")
        prod_display["rev_weight"] = prod_display["rev_weight"].apply(lambda x: f"{x*100:.2f}%")
        prod_display = prod_display.rename(columns={
            "customer_program": "Program", "customer_parent": "Parent",
            "revenue": "Revenue", "rev_weight": "Rev Weight",
            "billed_amount": "Billed Amount", "temp_labor": "Temp Labor",
            "direct_hire": "Direct Hire", "freight_storage": "Freight & Storage",
            "raw_materials": "Raw Materials", "equipment": "Equipment",
            "commission": "Commission", "applied_wh": "Applied WH",
            "applied_sga": "Applied SG&A", "gross_profit": "Gross Profit",
            "gp_margin": "GP Margin", "net_profit": "Net Profit",
            "net_margin": "Net Margin",
        })
        st.dataframe(prod_display.style.applymap(
            highlight_negative,
            subset=["Gross Profit", "Net Profit", "GP Margin", "Net Margin"]
        ), use_container_width=True, hide_index=True)

    # ── All other programs ───────────────────────────────────────────────────
    st.divider()
    st.markdown("### Other Fulfillment Programs")
    excluded = flags["experiential"] | flags["scaas"] | PRODUCTION_PROGRAMS
    other_raw = load_profitability(engine, year, month)
    other_raw = other_raw[~other_raw["customer_program"].isin(excluded)].copy()

    if other_raw.empty:
        st.info("No other program data for this period.")
    else:
        o1, o2, o3, o4 = st.columns(4)
        o1.metric("Revenue",      f"${other_raw['revenue'].sum():,.0f}")
        o2.metric("Gross Profit", f"${other_raw['gross_profit'].sum():,.0f}")
        o3.metric("GP Margin",    f"{other_raw['gross_profit'].sum() / other_raw['revenue'].sum() * 100:.1f}%" if other_raw['revenue'].sum() else "—")
        o4.metric("Net Profit",   f"${other_raw['net_profit'].sum():,.0f}")

        other_display = other_raw.sort_values("revenue", ascending=False).copy()
        for col in ["revenue","billed_amount","temp_labor","direct_hire",
                    "freight_storage","raw_materials","equipment","commission",
                    "applied_wh","applied_sga","gross_profit","net_profit"]:
            other_display[col] = other_display[col].apply(lambda x: f"${x:,.2f}")
        other_display["gp_margin"]  = other_display["gp_margin"].apply(lambda x: f"{x*100:.1f}%")
        other_display["net_margin"] = other_display["net_margin"].apply(lambda x: f"{x*100:.1f}%")
        other_display["rev_weight"] = other_display["rev_weight"].apply(lambda x: f"{x*100:.2f}%")
        other_display = other_display.rename(columns={
            "customer_program": "Program", "customer_parent": "Parent",
            "revenue": "Revenue", "rev_weight": "Rev Weight",
            "billed_amount": "Billed Amount", "temp_labor": "Temp Labor",
            "direct_hire": "Direct Hire", "freight_storage": "Freight & Storage",
            "raw_materials": "Raw Materials", "equipment": "Equipment",
            "commission": "Commission", "applied_wh": "Applied WH",
            "applied_sga": "Applied SG&A", "gross_profit": "Gross Profit",
            "gp_margin": "GP Margin", "net_profit": "Net Profit",
            "net_margin": "Net Margin",
        })
        st.dataframe(other_display.style.applymap(
            highlight_negative,
            subset=["Gross Profit", "Net Profit", "GP Margin", "Net Margin"]
        ), use_container_width=True, hide_index=True)
    
    # ── Program Snapshot ────────────────────────────────────────────────────
    _render_program_snapshot(engine, df, year, month, month_label)

    # ── WIP Balance Summary ─────────────────────────────────────────────────
    _render_wip_summary(engine, year, month)