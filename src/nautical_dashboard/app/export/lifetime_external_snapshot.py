from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from program_snapshot import build_program_snapshot


# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------
PERIOD = "2026-02"
PERIOD_LABEL = "February 2026"
PROGRAM = "Life Time"

BASE_DIR = Path(__file__).resolve().parent
ROSTER_PATH = BASE_DIR / "Revised Roster With Allocations Excel 1.xlsx"
OUT_PATH = BASE_DIR / "Life_Time_External_Snapshot_Revised.pdf"


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------
def _source_to_labor_type(source: str) -> str:
    s = str(source or "").strip().lower()
    if s == "direct sg&a":
        return "direct_sga"
    if s == "temp":
        return "temp"
    return "direct_cogs"


def _dollar(v: float) -> float:
    return round(float(v or 0.0), 2)


def _safe_div(n: float, d: float) -> float:
    return 0.0 if not d else float(n) / float(d)


# -----------------------------------------------------------------------------
# LOAD REVISED ROSTER
# -----------------------------------------------------------------------------
roster = pd.read_excel(ROSTER_PATH, sheet_name="Revised Roster With Allocations")
roster.columns = [str(c).strip() for c in roster.columns]

# Keep only rows for LifeTime / Life Time
roster = roster[roster["Target Program"].astype(str).str.strip().str.lower().isin(["lifetime", "life time"])].copy()

# Normalize columns
roster["Allocated Labor"] = pd.to_numeric(roster["Allocated Labor"], errors="coerce").fillna(0.0)
roster["Revised Weight"] = pd.to_numeric(roster["Revised Weight"], errors="coerce").fillna(0.0)
roster["Revised Allocated Labor"] = pd.to_numeric(roster["Revised Allocated Labor"], errors="coerce").fillna(0.0)
roster["labor_type"] = roster["Source"].map(_source_to_labor_type)

# Build revised employee detail for the snapshot
labor_employee_df = pd.DataFrame({
    "employee": roster["Employee"].fillna(""),
    "role": roster["Role / Detail"].fillna(""),
    "cost_center": roster["Cost Center"].fillna(""),
    "activity_driver": roster["Driver"].fillna(""),
    "weight": roster["Revised Weight"].fillna(0.0),
    "allocated_cost": roster["Revised Allocated Labor"].fillna(0.0),
    "labor_type": roster["labor_type"].fillna("direct_cogs"),
})

# Build revised labor summary for the snapshot
labor_df = (
    labor_employee_df
    .groupby(["labor_type", "cost_center", "activity_driver"], dropna=False, as_index=False)["allocated_cost"]
    .sum()
)

labor_df["program"] = PROGRAM
labor_df["activity_value"] = None
labor_df["weight"] = None

labor_df = labor_df.rename(columns={
    "allocated_cost": "allocated_cost",
    "cost_center": "cost_center",
    "activity_driver": "activity_driver",
})

labor_df = labor_df[[
    "labor_type",
    "cost_center",
    "activity_driver",
    "activity_value",
    "weight",
    "allocated_cost",
    "program",
]]

# -----------------------------------------------------------------------------
# BASE P&L INPUTS
# Replace these if you want to pull from SQL instead of hardcoding the one-off.
# -----------------------------------------------------------------------------
revenue = 167_093.59
raw_materials = 11_458.49
equipment = 15_075.49
commission = 0.00
freight_storage = 0.00
warehouse = 59_263.11

# Revised labor totals from roster
direct_hire = _dollar(labor_employee_df.loc[labor_employee_df["labor_type"] == "direct_cogs", "allocated_cost"].sum())
temp_labor = _dollar(labor_employee_df.loc[labor_employee_df["labor_type"] == "temp", "allocated_cost"].sum())
applied_sga = _dollar(labor_employee_df.loc[labor_employee_df["labor_type"] == "direct_sga", "allocated_cost"].sum())

gross_profit = _dollar(
    revenue
    - temp_labor
    - direct_hire
    - raw_materials
    - equipment
    - commission
    - freight_storage
    - warehouse
)

gp_margin = _safe_div(gross_profit, revenue)

net_profit = _dollar(gross_profit - applied_sga)
net_margin = _safe_div(net_profit, revenue)

# This is the row the snapshot builder expects
pnl_row = pd.Series({
    "customer_program": PROGRAM,
    "revenue": revenue,
    "temp_labor": temp_labor,
    "direct_hire": direct_hire,
    "raw_materials": raw_materials,
    "equipment": equipment,
    "commission": commission,
    "freight_storage": freight_storage,
    "applied_wh": warehouse,
    "gross_profit": gross_profit,
    "gp_margin": gp_margin,
    "applied_sga": applied_sga,
    "net_profit": net_profit,
    "net_margin": net_margin,
    "rev_weight": 0.0,
})

# -----------------------------------------------------------------------------
# OPTIONAL: warehouse / freight / wip inputs
# For this one-off, keep them simple if you do not want to query them live.
# -----------------------------------------------------------------------------
warehouse_df = pd.DataFrame([
    {
        "program_bucket": "Direct - Storage",
        "category": "Storage",
        "cost_type": "cogs",
        "driver_type": "Direct Sqft",
        "allocation_pct": 0.0956,
        "allocation_amount": 31_846.45,
    },
    {
        "program_bucket": "Direct - Dock - Inbound",
        "category": "Dock - Inbound",
        "cost_type": "cogs",
        "driver_type": "Direct Sqft",
        "allocation_pct": 0.0429,
        "allocation_amount": 14_287.03,
    },
    {
        "program_bucket": "Direct - Dock - Outbound",
        "category": "Dock - Outbound",
        "cost_type": "cogs",
        "driver_type": "Direct Sqft",
        "allocation_pct": 0.0030,
        "allocation_amount": 1_008.66,
    },
    {
        "program_bucket": "Direct - Production",
        "category": "Production",
        "cost_type": "cogs",
        "driver_type": "Direct Sqft",
        "allocation_pct": 0.0131,
        "allocation_amount": 4_376.95,
    },
    {
        "program_bucket": "Office/Inventory",
        "category": "Shared",
        "cost_type": "cogs",
        "driver_type": "Revenue (Ops 77.5% of headcount)",
        "allocation_pct": 0.1456,
        "allocation_amount": 5_394.14,
    },
    {
        "program_bucket": "Office/Inventory",
        "category": "Shared",
        "cost_type": "sga",
        "driver_type": "Revenue (SGA 22.5% of headcount)",
        "allocation_pct": 0.1456,
        "allocation_amount": 1_564.96,
    },
    {
        "program_bucket": "Shared/Unassigned",
        "category": "Shared",
        "cost_type": "sga",
        "driver_type": "Revenue",
        "allocation_pct": 0.1456,
        "allocation_amount": 827.53,
    },
    {
        "program_bucket": "Shared Storage - A Racks",
        "category": "Storage",
        "cost_type": "cogs",
        "driver_type": "A-Rack Count",
        "allocation_pct": 0.4707,
        "allocation_amount": 2_349.88,
    },
])

freight_df = pd.DataFrame(columns=["invoice_num", "bill_date", "line_description", "amount", "match_type"])

wip = {
    "labor_production": pd.DataFrame(),
    "warehouse": pd.DataFrame(),
}

# -----------------------------------------------------------------------------
# IMPORTANT: suppress SGA employee detail for external version
# -----------------------------------------------------------------------------
labor_employee_df_external = labor_employee_df[labor_employee_df["labor_type"] != "direct_sga"].copy()

# -----------------------------------------------------------------------------
# BUILD PDF
# -----------------------------------------------------------------------------
pdf_path = build_program_snapshot(
    out_path=str(OUT_PATH),
    program=PROGRAM,
    period_label=PERIOD_LABEL,
    pnl_row=pnl_row,
    labor_df=labor_df,
    warehouse_df=warehouse_df,
    freight_df=freight_df,
    wip=wip,
    labor_employee_df=labor_employee_df_external,
    logo_path=None,
)

print(f"Built: {pdf_path}")
print(f"Revenue: {revenue:,.2f}")
print(f"Direct Hire (revised): {direct_hire:,.2f}")
print(f"Temp Labor (revised): {temp_labor:,.2f}")
print(f"Applied SGA (revised summary only): {applied_sga:,.2f}")
print(f"Net Profit: {net_profit:,.2f}")