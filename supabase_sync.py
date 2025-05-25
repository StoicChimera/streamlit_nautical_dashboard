import pandas as pd
from sqlalchemy import create_engine, text
import logging
from dotenv import load_dotenv
import os

# === CONFIGURATION ===
load_dotenv()
LOCAL_CONN = os.getenv("POSTGRES_CONN")
SUPABASE_CONN = os.getenv("SUPABASE_CONN")

STG_TABLES = [
    "stg_ow_units",
    "stg_product_service_detail",
    "stg_smartsheet_demo",
    "stg_smartsheet_ogp",
    "stg_wip_fulfillment_expenses",
    "stg_wip_project_expenses"
]

MATERIALIZED_VIEWS = [
    "mv_demo_kits_by_iso_week",
    "mv_demo_ogp_labor_by_week",
    "mv_demo_unit_labor_cost_by_week",
    "mv_ow_ogp_unit_labor_cost",
    "mv_ow_ogp_units_by_week",
    "mv_smartsheet_labor_allocation_costed",
    "mv_wip_fulfillment_freight"
]

# === SETUP LOGGING ===
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def sync_tables(local_engine, supabase_engine):
    for table in STG_TABLES:
        logging.info(f"🔄 Syncing table: {table}")
        df = pd.read_sql(f"SELECT * FROM {table}", local_engine)

        with supabase_engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE"))
        df.to_sql(table, supabase_engine, if_exists="append", index=False)
        logging.info(f"✅ {len(df)} rows uploaded to {table}")

def refresh_views(supabase_engine):
    for view in MATERIALIZED_VIEWS:
        logging.info(f"🔁 Refreshing materialized view: {view}")
        with supabase_engine.begin() as conn:
            conn.execute(text(f"REFRESH MATERIALIZED VIEW {view}"))
        logging.info(f"✅ View refreshed: {view}")

def main():
    logging.info("🚀 Starting Supabase sync process...")
    local_engine = create_engine(LOCAL_CONN)
    supabase_engine = create_engine(SUPABASE_CONN)

    sync_tables(local_engine, supabase_engine)
    refresh_views(supabase_engine)

    logging.info("🎉 Sync complete.")

if __name__ == "__main__":
    main()
