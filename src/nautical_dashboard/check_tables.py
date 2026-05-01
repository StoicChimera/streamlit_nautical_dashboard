import pandas as pd
from sqlalchemy import create_engine
from dotenv import load_dotenv
import os

load_dotenv()
local = create_engine(os.getenv("POSTGRES_CONN"))
supa  = create_engine(os.getenv("SUPABASE_CONN"))

local_cols = pd.read_sql("""
    SELECT column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'production' AND table_name = 'stg_smartsheet_ogp'
    ORDER BY ordinal_position
""", local)

supa_cols = pd.read_sql("""
    SELECT column_name, data_type FROM information_schema.columns
    WHERE table_schema = 'public' AND table_name = 'stg_smartsheet_ogp'
    ORDER BY ordinal_position
""", supa)

print("LOCAL:")
print(local_cols.to_string())
print("\nSUPABASE:")
print(supa_cols.to_string())