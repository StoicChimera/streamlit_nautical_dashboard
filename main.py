import streamlit as st
from modules import revenue, cogs

st.set_page_config(page_title="Finance Hub", layout="wide")

st.sidebar.title("Navigation")
page = st.sidebar.radio("Go to", ["Revenue Dashboard", "COGS Dashboard"])

if page == "Revenue Dashboard":
    revenue.render()
elif page == "COGS Dashboard":
    cogs.render()
