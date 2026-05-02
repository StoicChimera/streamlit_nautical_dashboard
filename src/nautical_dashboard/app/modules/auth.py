"""
auth.py
=======

Authentication and authorization helpers.

Streamlit Community Cloud handles login (allow-list + magic link).
This module handles authorization — looking up the logged-in user's
role from dim_app_users and gating features by role.

Public surface:
    require_login()     — call once at the top of every page; returns user dict
    require_role(roles) — gate a feature behind one or more roles
    current_user()      — read-only access to the cached user dict
"""

import os
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

_engine = create_engine(SUPABASE_CONN)


# -------------------------------------------------------------
# Login resolver
# -------------------------------------------------------------

def _streamlit_user_email() -> str | None:
    """
    Returns the logged-in user's email, or None if not authenticated.

    Streamlit Community Cloud sets st.user.email when viewer auth is on.
    On older Streamlit versions it's st.experimental_user.email.
    Locally (no auth), both are None.
    """
    user = getattr(st, "user", None) or getattr(st, "experimental_user", None)
    if user is None:
        return None
    email = getattr(user, "email", None)
    return email.lower().strip() if email else None


@st.cache_data(ttl=300, show_spinner=False)
def _lookup_user(email: str) -> dict | None:
    """Returns {email, name, role} or None if not found / inactive."""
    df = pd.read_sql(
        text("""
            SELECT email, name, role
            FROM dim_app_users
            WHERE LOWER(email) = LOWER(:email)
              AND active = TRUE
        """),
        _engine,
        params={"email": email},
    )
    if df.empty:
        return None
    row = df.iloc[0]
    return {
        "email": str(row["email"]),
        "name":  str(row["name"]),
        "role":  str(row["role"]),
    }


# -------------------------------------------------------------
# Public API
# -------------------------------------------------------------

def require_login() -> dict:
    """
    Call at the top of every page. Returns the logged-in user's
    {email, name, role} dict, or halts the page with st.stop() if
    the user is not authenticated or not provisioned.
    """
    email = _streamlit_user_email()

    # Local dev escape hatch — set DEV_USER_EMAIL in .env to bypass
    if not email:
        dev_email = os.getenv("DEV_USER_EMAIL")
        if dev_email:
            email = dev_email.lower().strip()
        else:
            st.error("Not signed in. Reload the app and log in.")
            st.stop()

    user = _lookup_user(email)
    if user is None:
        st.error(
            f"Account `{email}` is not configured for this app. "
            "Contact Alex to be added."
        )
        st.stop()

    # Cache on session_state for the rest of the request
    st.session_state["_auth_user"] = user
    return user


def current_user() -> dict:
    """Returns the user dict set by require_login(). Raises if not called yet."""
    user = st.session_state.get("_auth_user")
    if not user:
        raise RuntimeError("current_user() called before require_login()")
    return user


def require_role(*allowed_roles: str) -> None:
    """
    Halts the page with st.error + st.stop if the current user's role
    is not in allowed_roles.

    Usage:
        require_role("admin", "controller")
    """
    user = current_user()
    if user["role"] not in allowed_roles:
        st.error(
            f"Your role (`{user['role']}`) does not have access to this view. "
            f"Required: {', '.join(allowed_roles)}."
        )
        st.stop()


def has_role(*allowed_roles: str) -> bool:
    """Non-blocking version of require_role. Returns bool, doesn't halt."""
    user = current_user()
    return user["role"] in allowed_roles