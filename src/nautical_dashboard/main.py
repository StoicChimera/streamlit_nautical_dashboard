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
)
import subprocess
import os


st.set_page_config(page_title="Finance Hub", layout="wide")

PAGES = {
    "Profitability Dashboard": profitability.render,
    "Revenue":                 revenue.render,
    "Production Activity":     production_activity.render,
    "Raw Goods - COGS":        raw_goods.render,
    "SG&A":                    sga.render,
    "Warehouse Allocations":   allocations.render,
    "WIP Labor":               wip_labor.render,
    "WIP Freight":             wip_freight.render,
}

st.sidebar.title("Navigation")
page = st.sidebar.radio("Go to", list(PAGES.keys()))

# === Sync Button ===
st.sidebar.markdown("---")
st.sidebar.subheader("Data Management")

sync_clicked = st.sidebar.button("🔄 Refresh Data")

# Output area at the BOTTOM of the main page so errors are scrollable/copyable
sync_output = st.container()

if sync_clicked:
    with st.spinner("Syncing tables and refreshing views..."):
        sync_script = os.path.join(os.path.dirname(__file__), "supabase_sync.py")
        result = subprocess.run(
            ["python", sync_script],
            capture_output=True,
            text=True,
            check=False,   # don't raise — handle return code manually
        )

    with sync_output:
        st.markdown("---")
        st.markdown("### Sync Output")
        if result.returncode == 0:
            st.success("Sync complete.")
            with st.expander("View sync logs", expanded=False):
                st.code(result.stdout or "[no output]", language="text")
        else:
            st.error(f"Sync failed (exit code {result.returncode})")
            st.markdown("**stderr:**")
            st.code(result.stderr or "[no stderr]", language="text")
            if result.stdout:
                with st.expander("stdout (might have context)", expanded=False):
                    st.code(result.stdout, language="text")

# === Page Rendering ===
PAGES[page]()
