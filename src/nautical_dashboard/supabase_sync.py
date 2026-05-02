import io
import os
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from financial_data_platform.etl._common.refresh_logger import (
    init_refresh_log_table,
    log_refresh,
)

load_dotenv()
LOCAL_CONN = os.getenv("POSTGRES_CONN")
SUPABASE_CONN = os.getenv("SUPABASE_CONN")

# ---------------------------------------------------------------------------
# Table lists
# ---------------------------------------------------------------------------

# public schema on local -> public schema on Supabase
STG_TABLES = [
    "stg_ow_units",
    "stg_product_service_detail",
    "stg_wip_fulfillment_expenses",
    "stg_wip_project_expenses",
    "clean_qbo_journal_lines",
    "clean_qbo_invoice_lines",
]

# production schema on local -> public schema on Supabase
PRODUCTION_TABLES = [
    "stg_smartsheet_demo",
    "stg_smartsheet_ogp",
    "stg_smartsheet_overwrap",
]

# labor schema on local -> public schema on Supabase
LABOR_TABLES = [
    "stg_labor_direct_hire",
    "stg_labor_temp",
    "stg_labor_program_map",
]

LOCAL_LABOR_TABLE_MAP = {
    "stg_labor_direct_hire": "labor.stg_labor_direct_hire",
    "stg_labor_temp":        "labor.stg_labor_temp",
    "stg_labor_program_map": "labor.dim_labor_program",
}

# freight schema on local -> public schema on Supabase
FREIGHT_TABLES = [
    "stg_wip_freight",
    "dim_freight_matching",
    "dim_freight_customer_type",
]

# public schema on local -> public schema on Supabase (large tables, COPY path)
LARGE_TABLES = [
    "clean_qbo_transaction_splits_flat",
]

# extensiv schema on local -> public schema on Supabase (COPY path)
EXTENSIV_TABLES = [
    "stg_extensiv_stock_status",
    "stg_extensiv_receipts",
    "stg_extensiv_shipments",
]

MATERIALIZED_VIEWS = [
    "mv_demo_kits_by_iso_week",
    "mv_demo_ogp_labor_by_week",
    "mv_demo_unit_labor_cost_by_week",
    "mv_ow_ogp_unit_labor_cost",
    "mv_ow_ogp_units_by_week",
    "mv_smartsheet_labor_allocation_costed",
    "mv_wip_fulfillment_freight",
]

# ---------------------------------------------------------------------------
# Type coercions for production tables
# ---------------------------------------------------------------------------

PRODUCTION_TABLE_TYPES = {
    "stg_smartsheet_ogp": {
        "date":                                    "datetime",
        "project_ship_date":                       "datetime",
        "how_many_items_in_bag":                   "numeric",
        "daily_production_goal":                   "numeric",
        "daily_production_complete":               "numeric",
        "number_of_people_planned":                "numeric",
        "number_of_people_working":                "numeric",
        "cumulative_bag_version_total_produced":   "numeric",
        "total_hours_staffed":                     "numeric",
        "sbs_hours":                               "numeric",
        "lsi_hours":                               "numeric",
        "nautical_direct_hours":                   "numeric",
    },
    "stg_smartsheet_overwrap": {
        "date_started":                            "datetime",
        "date_finished":                           "datetime",
        "sale_price_per_unit_for_overwrap_portion":"numeric",
        "hours_worked":                            "numeric",
        "units_produced":                          "numeric",
    },
}


def _coerce_types(df: pd.DataFrame, table_name: str) -> pd.DataFrame:
    type_map = PRODUCTION_TABLE_TYPES.get(table_name, {})
    for col, dtype in type_map.items():
        if col not in df.columns:
            continue
        if dtype == "datetime":
            df[col] = pd.to_datetime(df[col], errors="coerce")
        elif dtype == "numeric":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def get_table_columns(engine, schema_name: str, table_name: str) -> list[str]:
    sql = f"""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = '{schema_name}'
          AND table_name   = '{table_name}'
        ORDER BY ordinal_position
    """
    return pd.read_sql(sql, engine)["column_name"].tolist()


def assert_columns_match(
    local_engine,
    supabase_engine,
    table_name: str,
    local_schema: str = "public",
) -> None:
    """
    Strict schema check — local and Supabase must match 1:1.
    Used for all non-labor tables.
    """
    local_cols = get_table_columns(local_engine, local_schema, table_name)
    sb_cols    = get_table_columns(supabase_engine, "public", table_name)

    missing_in_supabase = [c for c in local_cols if c not in sb_cols]
    extra_in_supabase   = [c for c in sb_cols   if c not in local_cols]

    if missing_in_supabase or extra_in_supabase:
        raise RuntimeError(
            f"{table_name}: schema mismatch | "
            f"missing_in_supabase={missing_in_supabase} | "
            f"extra_in_supabase={extra_in_supabase}"
        )


def validate_and_align_shared_columns(
    local_engine,
    supabase_engine,
    local_schema: str,
    local_table: str,
    supabase_table: str,
    df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Labor-table-friendly alignment.
    Supabase may have extra app-side columns (reviewed / reviewed_by / reviewed_at).
    Supabase must still contain every local column we intend to sync.
    DataFrame is reordered to Supabase column order, limited to shared cols.
    """
    local_cols = get_table_columns(local_engine, local_schema, local_table)
    sb_cols    = get_table_columns(supabase_engine, "public", supabase_table)

    missing_in_supabase = [c for c in local_cols if c not in sb_cols]
    if missing_in_supabase:
        raise RuntimeError(
            f"{supabase_table}: supabase missing columns required by local table "
            f"{local_schema}.{local_table}: {missing_in_supabase}"
        )

    shared_cols = [c for c in sb_cols if c in local_cols]
    if not shared_cols:
        raise RuntimeError(
            f"{supabase_table}: no shared columns found between "
            f"{local_schema}.{local_table} and public.{supabase_table}"
        )

    missing_in_df = [c for c in shared_cols if c not in df.columns]
    if missing_in_df:
        raise RuntimeError(
            f"{supabase_table}: dataframe missing expected shared columns: {missing_in_df}"
        )

    return df[shared_cols].copy()


# ---------------------------------------------------------------------------
# COPY-based bulk sync helper
# ---------------------------------------------------------------------------

def _copy_sync(
    local_engine,
    direct_engine,
    local_schema: str,
    table_name: str,
    supabase_table: str | None = None,
    exclude_cols: list[str] | None = None,
) -> tuple[int, int]:
    target = supabase_table or table_name

    df = pd.read_sql(f"SELECT * FROM {local_schema}.{table_name}", local_engine)

    if exclude_cols:
        df = df.drop(columns=[c for c in exclude_cols if c in df.columns])

    n = len(df)

    with direct_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE public.{target}"))

    if n:
        buf = io.StringIO()
        df.to_csv(buf, index=False, header=False, na_rep="")
        buf.seek(0)
        cols = ", ".join(df.columns)

        raw = direct_engine.raw_connection()
        try:
            with raw.cursor() as cur:
                cur.copy_expert(
                    f"COPY public.{target} ({cols}) FROM STDIN WITH (FORMAT CSV, NULL '')",
                    buf,
                )
            raw.commit()
        finally:
            raw.close()

    return n, n


# ---------------------------------------------------------------------------
# Sync functions
# ---------------------------------------------------------------------------

def sync_tables(local_engine, supabase_engine) -> tuple[int, int]:
    """public schema on local -> public schema on Supabase."""
    total_pulled = total_written = 0

    for table_name in STG_TABLES:
        logging.info(f"Syncing table: {table_name}")
        assert_columns_match(local_engine, supabase_engine, table_name)

        df = pd.read_sql(f"SELECT * FROM public.{table_name}", local_engine)
        n  = len(df)
        total_pulled += n

        with supabase_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE public.{table_name}"))

        if n:
            df.to_sql(
                table_name,
                supabase_engine,
                schema="public",
                if_exists="append",
                index=False,
                method="multi",
                chunksize=5000,
            )

        total_written += n
        logging.info(f"{n} rows uploaded to public.{table_name}")

    return total_pulled, total_written


def sync_production_tables(local_engine, supabase_engine) -> tuple[int, int]:
    """production schema on local -> public schema on Supabase."""
    total_pulled = total_written = 0

    for table_name in PRODUCTION_TABLES:
        logging.info(f"Syncing production table: {table_name}")
        assert_columns_match(local_engine, supabase_engine, table_name, local_schema="production")

        df = pd.read_sql(f"SELECT * FROM production.{table_name}", local_engine)
        df = _coerce_types(df, table_name)
        n  = len(df)
        total_pulled += n

        with supabase_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE public.{table_name}"))

        if n:
            df.to_sql(
                table_name,
                supabase_engine,
                schema="public",
                if_exists="append",
                index=False,
                method="multi",
                chunksize=5000,
            )

        total_written += n
        logging.info(f"{n} rows synced to public.{table_name}")

    return total_pulled, total_written


def sync_labor_tables(local_engine, direct_engine) -> tuple[int, int]:
    total_pulled = total_written = 0

    for table_name in LABOR_TABLES:
        logging.info(f"Syncing labor table: {table_name}")

        local_table_ref         = LOCAL_LABOR_TABLE_MAP.get(table_name, f"labor.{table_name}")
        local_schema, local_tbl = local_table_ref.split(".")

        df = pd.read_sql(f"SELECT * FROM {local_table_ref}", local_engine)
        n  = len(df)
        total_pulled += n

        df = validate_and_align_shared_columns(
            local_engine=local_engine,
            supabase_engine=direct_engine,
            local_schema=local_schema,
            local_table=local_tbl,
            supabase_table=table_name,
            df=df,
        )

        # --- preserve review state before truncate ---
        # is_correction is part of the dedup key, so it must be in the restore predicate
        # to avoid flipping both the original and the correction row to reviewed=TRUE.
        try:
            review_state = pd.read_sql(
                f"""
                SELECT employee_name, accrual_period, is_correction,
                       reviewed, reviewed_by, reviewed_at
                FROM public.{table_name}
                WHERE reviewed = TRUE
                """,
                direct_engine,
            )
        except Exception:
            review_state = pd.DataFrame()

        with direct_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE public.{table_name}"))

        if n:
            df.to_sql(
                table_name,
                direct_engine,
                schema="public",
                if_exists="append",
                index=False,
                method="multi",
                chunksize=500,
            )

        # --- restore review state ---
        if not review_state.empty:
            with direct_engine.begin() as conn:
                for _, r in review_state.iterrows():
                    conn.execute(text(f"""
                        UPDATE public.{table_name}
                        SET reviewed     = TRUE,
                            reviewed_by  = :reviewed_by,
                            reviewed_at  = :reviewed_at
                        WHERE employee_name   = :employee_name
                          AND accrual_period  = :accrual_period
                          AND is_correction   = :is_correction
                    """), {
                        "reviewed_by":    r.get("reviewed_by"),
                        "reviewed_at":    r.get("reviewed_at"),
                        "employee_name":  r["employee_name"],
                        "accrual_period": r["accrual_period"],
                        "is_correction":  bool(r["is_correction"]),
                    })
            logging.info(f"Restored {len(review_state)} reviewed row(s) on public.{table_name}")

        total_written += n
        logging.info(f"{n} rows synced to public.{table_name}")

    return total_pulled, total_written


def sync_freight_tables(local_engine, supabase_engine) -> tuple[int, int]:
    """freight schema on local -> public schema on Supabase."""
    total_pulled = total_written = 0

    for table_name in FREIGHT_TABLES:
        logging.info(f"Syncing freight table: {table_name}")
        assert_columns_match(local_engine, supabase_engine, table_name, local_schema="freight")

        df = pd.read_sql(f"SELECT * FROM freight.{table_name}", local_engine)
        n  = len(df)
        total_pulled += n

        with supabase_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE public.{table_name}"))

        if n:
            df.to_sql(
                table_name,
                supabase_engine,
                schema="public",
                if_exists="append",
                index=False,
                method="multi",
                chunksize=5000,
            )

        total_written += n
        logging.info(f"{n} rows synced to public.{table_name}")

    return total_pulled, total_written


def sync_large_tables(local_engine, direct_engine) -> tuple[int, int]:
    """
    public schema on local -> public schema on Supabase.
    COPY-based for large row counts.
    """
    total_pulled = total_written = 0

    for table_name in LARGE_TABLES:
        logging.info(f"Syncing large table: {table_name}")
        assert_columns_match(local_engine, direct_engine, table_name, local_schema="public")
        pulled, written = _copy_sync(local_engine, direct_engine, "public", table_name)
        total_pulled  += pulled
        total_written += written
        logging.info(f"{pulled} rows synced to public.{table_name}")

    return total_pulled, total_written


def sync_extensiv_tables(local_engine, direct_engine) -> tuple[int, int]:
    total_pulled = total_written = 0

    for table_name in EXTENSIV_TABLES:
        logging.info(f"Syncing extensiv table: {table_name}")

        # id is a local-only bigserial surrogate — not present on Supabase
        pulled, written = _copy_sync(
            local_engine, direct_engine, "public", table_name,
            exclude_cols=["id"],
        )
        total_pulled  += pulled
        total_written += written
        logging.info(f"{pulled} rows synced to public.{table_name}")

    return total_pulled, total_written


def refresh_views(supabase_engine) -> tuple[int, int]:
    attempted = refreshed = 0

    for view in MATERIALIZED_VIEWS:
        attempted += 1
        logging.info(f"Refreshing materialized view: {view}")
        try:
            with supabase_engine.begin() as conn:
                conn.execute(text(f"REFRESH MATERIALIZED VIEW {view}"))
            refreshed += 1
            logging.info(f"View refreshed: {view}")
        except Exception as e:
            logging.warning(f"Could not refresh {view}: {e}")

    return attempted, refreshed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.info("Starting Supabase sync process...")

    if not LOCAL_CONN or not SUPABASE_CONN:
        raise RuntimeError("Missing POSTGRES_CONN or SUPABASE_CONN")

    local_engine    = create_engine(LOCAL_CONN, pool_pre_ping=True)
    supabase_engine = create_engine(SUPABASE_CONN, hide_parameters=True, pool_pre_ping=True)

    # Direct port 5432 engine — used for COPY-based syncs and labor tables.
    # Supabase's pooler (6543) does not support COPY; direct connection required.
    direct_conn_str = SUPABASE_CONN.replace(":6543/", ":5432/")
    direct_engine   = create_engine(
        direct_conn_str,
        pool_pre_ping=True,
        connect_args={"options": "-c statement_timeout=0"},
    )

    init_refresh_log_table(engine=supabase_engine)

    source_system  = "sync"
    api_name       = "local_to_supabase"
    start_time     = datetime.utcnow()
    status         = "success"
    error_message  = None
    total_pulled   = 0
    total_written  = 0
    num_requests   = 0

    try:
        pulled, written = sync_tables(local_engine, supabase_engine)
        total_pulled  += pulled
        total_written += written
        num_requests  += len(STG_TABLES)

        pulled, written = sync_production_tables(local_engine, supabase_engine)
        total_pulled  += pulled
        total_written += written
        num_requests  += len(PRODUCTION_TABLES)

        # labor and large use direct_engine (COPY / direct port)
        pulled, written = sync_labor_tables(local_engine, direct_engine)
        total_pulled  += pulled
        total_written += written
        num_requests  += len(LABOR_TABLES)

        pulled, written = sync_freight_tables(local_engine, supabase_engine)
        total_pulled  += pulled
        total_written += written
        num_requests  += len(FREIGHT_TABLES)

        pulled, written = sync_large_tables(local_engine, direct_engine)
        total_pulled  += pulled
        total_written += written
        num_requests  += len(LARGE_TABLES)

        pulled, written = sync_extensiv_tables(local_engine, direct_engine)
        total_pulled  += pulled
        total_written += written
        num_requests  += len(EXTENSIV_TABLES)

        attempted, refreshed = refresh_views(supabase_engine)
        num_requests += attempted

        logging.info("Sync complete.")

    except Exception as e:
        status        = "fail"
        error_message = str(e)
        logging.exception("Supabase sync failed.")
        raise

    finally:
        end_time = datetime.utcnow()
        log_refresh(
            source_system=source_system,
            api_name=api_name,
            start_time=start_time,
            end_time=end_time,
            num_requests=num_requests or 1,
            num_rows_pulled=total_pulled,
            num_rows_written=total_written,
            status=status,
            error_message=error_message,
            engine=supabase_engine,
        )


if __name__ == "__main__":
    main()