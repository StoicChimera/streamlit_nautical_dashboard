"""
revenue_mix.py
==============

Revenue Mix page: how revenue splits across Production, Fulfillment,
Parcel Shipping, Sourcing and Other, with subcategory detail.

Data comes from vw_revenue_mix and vw_revenue_line_category
(sql/revenue_mix.sql). Categories are assigned in the database by
dim_revenue_customer and dim_revenue_category_rule, so changing how a
customer or item is classified is a table edit, not a code change.

Sections:
  - Filters: month range, category, subcategory, program, customer type
  - Category callouts (dollars and share)
  - Monthly mix chart (dollars or share)
  - Subcategory table
  - Program breakdown (dollars or share of program) and single program drilldown
  - Unmapped lines and programs missing from dim_revenue_customer
  - Excel export of everything on the page

Run on its own:
    streamlit run src/nautical_dashboard/app/modules/revenue_mix.py

Embed in an existing page:
    from . import revenue_mix
    revenue_mix.render()

For a program page (e.g. profitability):
    revenue_mix.render_program_mix_callout(program, start_month, end_month)
"""

from __future__ import annotations

import io
import os
from datetime import date

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

try:
    import altair as alt
except ImportError:  # altair ships with streamlit, but do not crash without it
    alt = None

load_dotenv()

# Display order. Excluded and Unmapped are not revenue mix categories but are
# kept so nothing silently disappears.
CATEGORY_ORDER = [
    "Production",
    "Fulfillment",
    "Parcel Shipping",
    "Sourcing",
    "Other",
    "Unmapped",
    "Excluded",
]
MIX_CATEGORIES = ["Production", "Fulfillment", "Parcel Shipping", "Sourcing", "Other"]
CATEGORY_COLORS = {
    "Production":      "#1F3864",
    "Fulfillment":     "#2E75B6",
    "Parcel Shipping": "#C55A11",
    "Sourcing":        "#548235",
    "Other":           "#7F7F7F",
    "Unmapped":        "#C00000",
    "Excluded":        "#BFBFBF",
}


# -------------------------------------------------------------
# Connection and data
# -------------------------------------------------------------

@st.cache_resource(show_spinner=False)
def _engine():
    conn = os.getenv("SUPABASE_CONN")
    if not conn:
        raise RuntimeError("Missing SUPABASE_CONN environment variable.")
    return create_engine(conn, pool_pre_ping=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_mix() -> pd.DataFrame:
    """Full month x program x category x subcategory grid."""
    sql = text("""
        SELECT month_start, customer_program, customer_type,
               category, subcategory, revenue, line_count
        FROM vw_revenue_mix
    """)
    df = pd.read_sql(sql, _engine())
    if df.empty:
        return df
    df["month_start"] = pd.to_datetime(df["month_start"])
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0.0)
    df["customer_program"] = df["customer_program"].fillna("(blank)")
    return df


@st.cache_data(ttl=300, show_spinner=False)
def load_unmapped(start: date, end: date) -> pd.DataFrame:
    sql = text("""
        SELECT customer_program, product_service, item_class, distribution_account,
               COUNT(*) AS lines, SUM(amount) AS amount
        FROM vw_revenue_line_category
        WHERE category = 'Unmapped'
          AND month_start BETWEEN :start AND :end
        GROUP BY 1, 2, 3, 4
        ORDER BY ABS(SUM(amount)) DESC
    """)
    return pd.read_sql(sql, _engine(), params={"start": start, "end": end})


@st.cache_data(ttl=300, show_spinner=False)
def load_unknown_customers(start: date, end: date) -> pd.DataFrame:
    sql = text("""
        SELECT customer_program, SUM(amount) AS amount, COUNT(*) AS lines
        FROM vw_revenue_line_category
        WHERE NOT customer_mapped
          AND month_start BETWEEN :start AND :end
        GROUP BY 1
        ORDER BY ABS(SUM(amount)) DESC
    """)
    return pd.read_sql(sql, _engine(), params={"start": start, "end": end})


# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------

def _money(v: float) -> str:
    if pd.isna(v):
        return ""
    return f"(${abs(v):,.0f})" if v < 0 else f"${v:,.0f}"


def _ordered(values) -> list[str]:
    vals = [v for v in values if pd.notna(v)]
    known = [c for c in CATEGORY_ORDER if c in vals]
    return known + sorted(v for v in vals if v not in CATEGORY_ORDER)


def _month_label(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m")


def _style_money(df: pd.DataFrame, money_cols, pct_cols=()):
    fmt = {c: _money for c in money_cols}
    fmt.update({c: "{:.1%}" for c in pct_cols})
    return df.style.format(fmt, na_rep="")


def _show_df(obj) -> None:
    """st.dataframe at full width on old and new Streamlit versions."""
    try:
        _show_df(obj, hide_index=True, width="stretch")
    except (TypeError, Exception):
        st.dataframe(obj)


def _show_chart(chart) -> None:
    try:
        st.altair_chart(chart, width="stretch")
    except (TypeError, Exception):
        _show_chart(chart)


def _mix_total(df: pd.DataFrame) -> float:
    """Denominator for shares: everything except Excluded."""
    return float(df.loc[df["category"] != "Excluded", "revenue"].sum())


# -------------------------------------------------------------
# Filters
# -------------------------------------------------------------

def _filters(df: pd.DataFrame, key: str):
    months = sorted(df["month_start"].dropna().unique())
    if not months:
        return df.iloc[0:0], None, None

    default_start = months[max(0, len(months) - 12)]
    c1, c2 = st.columns([3, 2])
    with c1:
        start, end = st.select_slider(
            "Months (contract completion date)",
            options=months,
            value=(default_start, months[-1]),
            format_func=_month_label,
            key=f"{key}_months",
        )
    with c2:
        types = sorted(df["customer_type"].dropna().unique())
        sel_types = st.multiselect(
            "Customer type", options=types, default=types, key=f"{key}_types",
        )

    in_range = df[(df["month_start"] >= start) & (df["month_start"] <= end)]

    c3, c4, c5 = st.columns(3)
    with c3:
        cat_options = _ordered(in_range["category"].unique())
        cat_default = [c for c in cat_options if c != "Excluded"]
        sel_cats = st.multiselect(
            "Category", options=cat_options, default=cat_default, key=f"{key}_cats",
        )
    with c4:
        sub_pool = in_range[in_range["category"].isin(sel_cats)]
        sub_options = sorted(
            (sub_pool["category"] + " / " + sub_pool["subcategory"]).unique()
        )
        sel_subs = st.multiselect(
            "Subcategory (blank = all in selected categories)",
            options=sub_options, default=[], key=f"{key}_subs",
        )
    with c5:
        prog_options = sorted(in_range["customer_program"].unique())
        sel_progs = st.multiselect(
            "Program (blank = all)", options=prog_options, default=[], key=f"{key}_progs",
        )

    out = in_range[in_range["category"].isin(sel_cats)]
    if sel_types:
        out = out[out["customer_type"].isin(sel_types)]
    if sel_subs:
        out = out[(out["category"] + " / " + out["subcategory"]).isin(sel_subs)]
    if sel_progs:
        out = out[out["customer_program"].isin(sel_progs)]

    return out, pd.Timestamp(start).date(), pd.Timestamp(end).date()


# -------------------------------------------------------------
# Sections
# -------------------------------------------------------------

def _callouts(df: pd.DataFrame) -> None:
    total = _mix_total(df)
    by_cat = df.groupby("category")["revenue"].sum()
    cats = [c for c in _ordered(by_cat.index) if c != "Excluded"]

    st.metric("Total revenue (selection)", _money(total))
    if not cats:
        return
    cols = st.columns(len(cats))
    for col, cat in zip(cols, cats):
        val = float(by_cat.get(cat, 0.0))
        share = val / total if total else 0.0
        col.metric(cat, _money(val), help=f"{share:.1%} of selected revenue")
        col.caption(f"{share:.1%} of revenue")

    if "Excluded" in by_cat.index and by_cat["Excluded"] != 0:
        st.caption(
            f"Excluded from totals (loans, financing, deposits): {_money(by_cat['Excluded'])}"
        )
    if "Unmapped" in by_cat.index and abs(by_cat["Unmapped"]) >= 0.5:
        st.warning(
            f"{_money(by_cat['Unmapped'])} is Unmapped. See 'Unmapped lines' below and add a "
            "rule or customer row so it lands in a category."
        )


def _monthly_chart(df: pd.DataFrame, key: str) -> None:
    st.markdown("#### Mix by month")
    mode = st.radio(
        "Show", ["Dollars", "Share"], horizontal=True, key=f"{key}_chart_mode",
        label_visibility="collapsed",
    )
    data = (
        df[df["category"] != "Excluded"]
        .groupby(["month_start", "category"], as_index=False)["revenue"].sum()
    )
    if data.empty:
        st.info("No revenue in this selection.")
        return
    data["month"] = data["month_start"].dt.strftime("%Y-%m")

    if alt is None:
        pivot = data.pivot_table(index="month", columns="category", values="revenue",
                                 aggfunc="sum", fill_value=0)
        if mode == "Share":
            pivot = pivot.div(pivot.sum(axis=1).replace(0, pd.NA), axis=0)
        st.bar_chart(pivot)
        return

    cats = _ordered(data["category"].unique())
    color = alt.Color(
        "category:N",
        sort=cats,
        scale=alt.Scale(domain=cats, range=[CATEGORY_COLORS.get(c, "#999999") for c in cats]),
        legend=alt.Legend(title=None, orient="bottom"),
    )
    y = alt.Y(
        "revenue:Q",
        stack="normalize" if mode == "Share" else "zero",
        title=None,
        axis=alt.Axis(format="%" if mode == "Share" else "$,.0f"),
    )
    chart = (
        alt.Chart(data)
        .mark_bar()
        .encode(
            x=alt.X("month:N", title=None, sort=sorted(data["month"].unique())),
            y=y,
            color=color,
            order=alt.Order("cat_rank:Q"),
            tooltip=[
                alt.Tooltip("month:N", title="Month"),
                alt.Tooltip("category:N", title="Category"),
                alt.Tooltip("revenue:Q", title="Revenue", format="$,.0f"),
            ],
        )
        .transform_calculate(cat_rank=f"indexof({cats!r}, datum.category)")
        .properties(height=320)
    )
    _show_chart(chart)


def _subcategory_table(df: pd.DataFrame) -> pd.DataFrame:
    st.markdown("#### By subcategory")
    total = _mix_total(df)
    t = (
        df.groupby(["category", "subcategory"], as_index=False)["revenue"].sum()
        .rename(columns={"category": "Category", "subcategory": "Subcategory",
                         "revenue": "Revenue"})
    )
    t["Share"] = t["Revenue"] / total if total else 0.0
    t.loc[t["Category"] == "Excluded", "Share"] = float("nan")
    t["_o"] = t["Category"].map({c: i for i, c in enumerate(CATEGORY_ORDER)}).fillna(99)
    t = t.sort_values(["_o", "Revenue"], ascending=[True, False]).drop(columns="_o")
    _show_df(_style_money(t, ["Revenue"], ["Share"]))
    return t


def _program_breakdown(df: pd.DataFrame, key: str):
    st.markdown("#### Program breakdown")
    mix = df[df["category"] != "Excluded"]
    if mix.empty:
        st.info("No revenue in this selection.")
        return pd.DataFrame(), pd.DataFrame()

    types = (mix.groupby("customer_program")["customer_type"].first())
    pivot = mix.pivot_table(index="customer_program", columns="category",
                            values="revenue", aggfunc="sum", fill_value=0.0)
    cats = _ordered(pivot.columns)
    pivot = pivot[cats]
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("Total", ascending=False)

    dollars = pivot.reset_index().rename(columns={"customer_program": "Program"})
    dollars.insert(1, "Type", dollars["Program"].map(types))

    share = pivot[cats].div(pivot["Total"].where(pivot["Total"] != 0), axis=0)
    share = share.reset_index().rename(columns={"customer_program": "Program"})
    share.insert(1, "Type", share["Program"].map(types))
    share.insert(2, "Total", pivot["Total"].values)

    mode = st.radio(
        "View", ["Dollars", "Share of program"], horizontal=True, key=f"{key}_prog_mode",
        label_visibility="collapsed",
    )
    if mode == "Dollars":
        _show_df(_style_money(dollars, cats + ["Total"]))
    else:
        _show_df(_style_money(share, ["Total"], cats))

    # Single program drilldown with subcategory detail
    programs = dollars["Program"].tolist()
    pick = st.selectbox("Program detail", options=programs, key=f"{key}_prog_pick")
    if pick:
        p = df[df["customer_program"] == pick]
        ptotal = _mix_total(p)
        d = (p.groupby(["category", "subcategory"], as_index=False)["revenue"].sum()
             .rename(columns={"category": "Category", "subcategory": "Subcategory",
                              "revenue": "Revenue"}))
        d["Share"] = d["Revenue"] / ptotal if ptotal else 0.0
        d["_o"] = d["Category"].map({c: i for i, c in enumerate(CATEGORY_ORDER)}).fillna(99)
        d = d.sort_values(["_o", "Revenue"], ascending=[True, False]).drop(columns="_o")
        _show_df(_style_money(d, ["Revenue"], ["Share"]))

    return dollars, share


def _data_quality(start: date, end: date):
    unmapped = pd.DataFrame()
    unknown = pd.DataFrame()
    with st.expander("Unmapped lines"):
        try:
            unmapped = load_unmapped(start, end)
        except Exception as e:  # view missing or permissions
            st.error(f"Could not load unmapped lines: {e}")
        if unmapped.empty:
            st.success("Every line in this date range is mapped to a category.")
        else:
            st.caption(
                "Fix by adding a row to dim_revenue_category_rule (item, class or GL) "
                "or to dim_revenue_customer (whole program)."
            )
            _show_df(_style_money(unmapped, ["amount"]))
    with st.expander("Programs not in dim_revenue_customer"):
        try:
            unknown = load_unknown_customers(start, end)
        except Exception as e:
            st.error(f"Could not load program list: {e}")
        if unknown.empty:
            st.success("Every program with revenue in this range has a customer row.")
        else:
            st.caption(
                "These are treated as Mixed and classified line by line from item class "
                "and GL account. Add a row to set their type."
            )
            _show_df(_style_money(unknown, ["amount"]))
    return unmapped, unknown


def _export(df, subs, dollars, share, unmapped, unknown, start, end, key) -> None:
    cat = (df.groupby("category", as_index=False)["revenue"].sum()
           .rename(columns={"category": "Category", "revenue": "Revenue"}))
    total = _mix_total(df)
    cat["Share"] = cat["Revenue"] / total if total else 0.0
    cat.loc[cat["Category"] == "Excluded", "Share"] = float("nan")
    cat["_o"] = cat["Category"].map({c: i for i, c in enumerate(CATEGORY_ORDER)}).fillna(99)
    cat = cat.sort_values("_o").drop(columns="_o")

    monthly = (df.pivot_table(index="month_start", columns="category", values="revenue",
                              aggfunc="sum", fill_value=0.0))
    monthly = monthly[_ordered(monthly.columns)].reset_index()
    monthly["month_start"] = monthly["month_start"].dt.strftime("%Y-%m")
    monthly = monthly.rename(columns={"month_start": "Month"})

    detail = df.copy()
    detail["month_start"] = detail["month_start"].dt.strftime("%Y-%m")
    detail = detail.rename(columns={
        "month_start": "Month", "customer_program": "Program", "customer_type": "Type",
        "category": "Category", "subcategory": "Subcategory", "revenue": "Revenue",
        "line_count": "Lines",
    })

    sheets = {
        "Category Summary": cat,
        "Subcategory": subs,
        "Program Mix Dollars": dollars,
        "Program Mix Share": share,
        "Monthly Mix": monthly,
        "Detail": detail,
        "Unmapped": unmapped,
        "Programs Not Mapped": unknown,
    }

    buf = io.BytesIO()
    engine_name = None
    for name in ("openpyxl", "xlsxwriter"):
        try:
            __import__(name)
            engine_name = name
            break
        except ImportError:
            continue

    fname = f"revenue_mix_{start:%Y-%m}_{end:%Y-%m}"
    if engine_name:
        with pd.ExcelWriter(buf, engine=engine_name) as xw:
            for sheet, frame in sheets.items():
                if frame is None or frame.empty:
                    pd.DataFrame({"note": ["No rows"]}).to_excel(xw, sheet_name=sheet, index=False)
                else:
                    frame.to_excel(xw, sheet_name=sheet, index=False)
        st.download_button(
            "Download revenue mix (Excel)", data=buf.getvalue(),
            file_name=f"{fname}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"{key}_dl",
        )
    else:
        st.download_button(
            "Download revenue mix detail (CSV)", data=detail.to_csv(index=False),
            file_name=f"{fname}.csv", mime="text/csv", key=f"{key}_dl",
        )


# -------------------------------------------------------------
# Public entry points
# -------------------------------------------------------------

def render(key: str = "revmix") -> None:
    """Full Revenue Mix page."""
    st.subheader("Revenue Mix")
    st.caption(
        "Production, Fulfillment, Parcel Shipping, Sourcing and Other. "
        "Months follow contract completion date and tie to the revenue page totals."
    )

    try:
        df = load_mix()
    except Exception as e:
        st.error(
            "Could not read vw_revenue_mix. Run sql/revenue_mix.sql in Supabase first.\n\n"
            f"{e}"
        )
        return
    if df.empty:
        st.info("vw_revenue_mix returned no rows.")
        return

    c_refresh, _ = st.columns([1, 5])
    with c_refresh:
        if st.button("Refresh data", key=f"{key}_refresh"):
            load_mix.clear()
            load_unmapped.clear()
            load_unknown_customers.clear()
            st.rerun()

    filtered, start, end = _filters(df, key)
    if filtered.empty:
        st.info("No revenue matches these filters.")
        return

    _callouts(filtered)
    st.divider()
    _monthly_chart(filtered, key)
    subs = _subcategory_table(filtered)
    st.divider()
    dollars, share = _program_breakdown(filtered, key)
    st.divider()
    unmapped, unknown = _data_quality(start, end)
    _export(filtered, subs, dollars, share, unmapped, unknown, start, end, key)


def render_program_mix_callout(program: str, start_month: date, end_month: date,
                               key: str = "progmix") -> None:
    """Compact mix callout for one program, for use on a program page.

    Shows revenue by category for the program and date range. Display only:
    costs are not split by revenue category, so this does not filter margins.
    """
    try:
        df = load_mix()
    except Exception as e:
        st.caption(f"Revenue mix unavailable: {e}")
        return
    start_ts, end_ts = pd.Timestamp(start_month), pd.Timestamp(end_month)
    p = df[(df["customer_program"] == program)
           & (df["month_start"] >= start_ts) & (df["month_start"] <= end_ts)]
    if p.empty:
        st.caption("No revenue mix data for this program and period.")
        return
    total = _mix_total(p)
    by_cat = p[p["category"] != "Excluded"].groupby("category")["revenue"].sum()
    cats = _ordered(by_cat.index)
    st.markdown("##### Revenue mix")
    cols = st.columns(max(1, len(cats)))
    for col, cat in zip(cols, cats):
        val = float(by_cat[cat])
        share = val / total if total else 0.0
        col.metric(cat, _money(val))
        col.caption(f"{share:.1%}")
    with st.expander("Subcategory detail"):
        d = (p.groupby(["category", "subcategory"], as_index=False)["revenue"].sum()
             .rename(columns={"category": "Category", "subcategory": "Subcategory",
                              "revenue": "Revenue"}))
        d["Share"] = d["Revenue"] / total if total else 0.0
        _show_df(_style_money(d, ["Revenue"], ["Share"]))


if __name__ == "__main__":
    st.set_page_config(page_title="Revenue Mix", layout="wide")
    render()