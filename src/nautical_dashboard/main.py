import sys
import os

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")

sys.path.insert(0, SRC_DIR)

import streamlit as st
from nautical_dashboard.app.modules import (
    revenue,
    allocations,
    profitability,
    sga,
    wip_labor,
    production_activity,
    wip_freight,
    raw_goods,
    auth_admin,
)

from nautical_dashboard.app.modules import auth

st.set_page_config(page_title="Finance Hub", layout="wide")

user = auth.require_login()
auth.render_logout_button()

PAGES = {
    "Profitability Dashboard": profitability.render,
    "Revenue":                 revenue.render,
    "Production Activity":     production_activity.render,
    "Raw Goods - COGS":        raw_goods.render,
    "SG&A":                    sga.render,
    "Warehouse Allocations":   allocations.render,
    "WIP Labor":               wip_labor.render,
    "WIP Freight":             wip_freight.render,
    "Auth Admin":              auth_admin.render,
}

st.sidebar.title("Navigation")
page = st.sidebar.radio("Go to", list(PAGES.keys()))

# === Page Rendering ===
PAGES[page]()
