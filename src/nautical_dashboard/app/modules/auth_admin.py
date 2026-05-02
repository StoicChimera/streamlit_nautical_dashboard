"""
auth_admin.py
=============

Admin-only views for managing the auth system:
  - Recent login activity
  - Failed attempt analysis
  - Locked accounts
  - User management (list, lock/unlock, reset password)
"""

import os
import secrets
import string
from datetime import datetime, timezone
import bcrypt
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from . import auth

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

_engine = create_engine(SUPABASE_CONN)


# ============================================================
# Helpers
# ============================================================

def _generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw)
            and any(c.isupper() for c in pw)
            and any(c.isdigit() for c in pw)):
            return pw


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


@st.cache_data(ttl=30, show_spinner=False)
def _load_recent_attempts(limit: int = 200) -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT id, attempted_at, email, success, reason
        FROM auth_audit_log
        ORDER BY attempted_at DESC
        LIMIT :limit
    """), _engine, params={"limit": limit})


@st.cache_data(ttl=30, show_spinner=False)
def _load_failed_summary(days: int = 7) -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT
            email,
            reason,
            COUNT(*)             AS attempts,
            MAX(attempted_at)    AS last_attempt
        FROM auth_audit_log
        WHERE success = FALSE
          AND attempted_at > NOW() - (:days || ' days')::interval
        GROUP BY email, reason
        ORDER BY attempts DESC, last_attempt DESC
    """), _engine, params={"days": days})


@st.cache_data(ttl=30, show_spinner=False)
def _load_users() -> pd.DataFrame:
    return pd.read_sql(text("""
        SELECT
            email, name, role, active,
            pw_hash IS NOT NULL              AS has_password,
            must_change_pw,
            failed_login_count,
            lockout_until,
            last_login_at,
            created_at
        FROM dim_app_users
        ORDER BY name
    """), _engine)


def _set_user_active(email: str, active: bool):
    with _engine.begin() as conn:
        conn.execute(text("""
            UPDATE dim_app_users
            SET active = :active
            WHERE LOWER(email) = LOWER(:email)
        """), {"active": active, "email": email})


def _clear_lockout(email: str):
    with _engine.begin() as conn:
        conn.execute(text("""
            UPDATE dim_app_users
            SET failed_login_count = 0,
                lockout_until      = NULL
            WHERE LOWER(email) = LOWER(:email)
        """), {"email": email})


def _reset_password(email: str) -> str:
    new_pw   = _generate_password()
    new_hash = _hash_password(new_pw)
    with _engine.begin() as conn:
        result = conn.execute(text("""
            UPDATE dim_app_users
            SET pw_hash             = :pw_hash,
                must_change_pw      = TRUE,
                failed_login_count  = 0,
                lockout_until       = NULL
            WHERE LOWER(email) = LOWER(:email)
            RETURNING email
        """), {"pw_hash": new_hash, "email": email})
        if not result.fetchone():
            raise RuntimeError(f"No user found with email {email}")
    return new_pw


# ============================================================
# Render
# ============================================================

def render():
    auth.require_role("admin")  # hard gate

    st.title("Auth Admin")
    st.caption("Login activity, user management, and password resets.")

    tab_activity, tab_users, tab_failed = st.tabs([
        "Recent Activity", "Users", "Failed Attempts",
    ])

    # =========================================================
    # TAB 1 — RECENT ACTIVITY
    # =========================================================
    with tab_activity:
        st.subheader("Recent Login Activity")

        col_limit, col_refresh = st.columns([2, 1])
        with col_limit:
            limit = st.slider("Show last N attempts", 50, 500, 200, step=50)
        with col_refresh:
            st.markdown("&nbsp;")
            if st.button("Refresh", use_container_width=True):
                _load_recent_attempts.clear()
                st.rerun()

        df = _load_recent_attempts(limit)

        if df.empty:
            st.info("No login attempts logged yet.")
        else:
            # Top-line metrics for the visible window
            total       = len(df)
            successes   = int(df["success"].sum())
            failures    = total - successes
            unique_users = df[df["success"]]["email"].nunique()

            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Attempts", total)
            k2.metric("Successful", successes)
            k3.metric("Failed", failures)
            k4.metric("Unique users (success)", unique_users)

            # Format for display
            display = df.copy()
            display["attempted_at"] = pd.to_datetime(display["attempted_at"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            display["success"]      = display["success"].map({True: "✓", False: "✗"})
            display = display.rename(columns={
                "attempted_at": "When",
                "email":        "Email",
                "success":      "OK",
                "reason":       "Reason (if failed)",
            })[["When", "Email", "OK", "Reason (if failed)"]]
            st.dataframe(display, use_container_width=True, hide_index=True)

    # =========================================================
    # TAB 2 — USERS
    # =========================================================
    with tab_users:
        st.subheader("User Management")

        users_df = _load_users()
        if users_df.empty:
            st.warning("No users in dim_app_users.")
            return

        # Quick metrics
        active_count   = int(users_df["active"].sum())
        locked_count   = int(users_df["lockout_until"].notna().sum())
        no_pw_count    = int((~users_df["has_password"]).sum())

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Total users", len(users_df))
        k2.metric("Active", active_count)
        k3.metric("Currently locked", locked_count)
        k4.metric("No password set", no_pw_count)

        st.markdown("---")

        # User table with action buttons inline
        for _, row in users_df.iterrows():
            email   = row["email"]
            name    = row["name"]
            role    = row["role"]
            active  = bool(row["active"])
            locked  = pd.notna(row["lockout_until"])

            status_bits = []
            if not active:
                status_bits.append("**INACTIVE**")
            if locked:
                lockout_dt = pd.to_datetime(row["lockout_until"], utc=True)
                if lockout_dt > datetime.now(timezone.utc):
                    mins = int((lockout_dt - datetime.now(timezone.utc)).total_seconds() / 60) + 1
                    status_bits.append(f"**LOCKED** ({mins}m)")
            if row["must_change_pw"]:
                status_bits.append("must change pw")
            if not row["has_password"]:
                status_bits.append("no password set")

            failed = int(row["failed_login_count"] or 0)
            if failed > 0 and not locked:
                status_bits.append(f"{failed} failed attempts")

            last_login = "never"
            if pd.notna(row["last_login_at"]):
                last_login = pd.to_datetime(row["last_login_at"]).strftime("%Y-%m-%d %H:%M")

            status_text = " · ".join(status_bits) if status_bits else "OK"

            col_info, col_unlock, col_reset, col_active = st.columns([4, 1, 1, 1])

            with col_info:
                st.markdown(
                    f"**{name}** · `{role}` · {email}  \n"
                    f"_Last login: {last_login} · {status_text}_"
                )

            with col_unlock:
                if locked or failed > 0:
                    if st.button("Unlock", key=f"unlock_{email}", use_container_width=True):
                        _clear_lockout(email)
                        _load_users.clear()
                        st.success(f"Unlocked {email}")
                        st.rerun()

            with col_reset:
                if st.button("Reset PW", key=f"reset_{email}", use_container_width=True):
                    new_pw = _reset_password(email)
                    _load_users.clear()
                    st.session_state[f"_new_pw_{email}"] = new_pw
                    st.rerun()

            with col_active:
                action_label = "Deactivate" if active else "Reactivate"
                if st.button(action_label, key=f"toggle_{email}", use_container_width=True):
                    _set_user_active(email, not active)
                    _load_users.clear()
                    st.rerun()

            # Show generated password in a code block if just reset
            new_pw_key = f"_new_pw_{email}"
            if new_pw_key in st.session_state:
                st.code(
                    f"New password for {email}:\n{st.session_state[new_pw_key]}",
                    language="text",
                )
                st.caption(
                    "Send this to the user via secure channel. They will be forced to "
                    "change it on next login. This will only be shown once — copy now."
                )
                if st.button("Dismiss", key=f"dismiss_{email}"):
                    del st.session_state[new_pw_key]
                    st.rerun()

            st.markdown("---")

    # =========================================================
    # TAB 3 — FAILED ATTEMPTS ANALYSIS
    # =========================================================
    with tab_failed:
        st.subheader("Failed Login Analysis")

        days = st.slider("Look back days", 1, 30, 7)

        df = _load_failed_summary(days)
        if df.empty:
            st.success(f"No failed login attempts in the last {days} days.")
        else:
            total_failed = int(df["attempts"].sum())
            unique_emails = df["email"].nunique()

            k1, k2 = st.columns(2)
            k1.metric("Total failed attempts", total_failed)
            k2.metric("Unique emails", unique_emails)

            # Group by reason
            by_reason = df.groupby("reason", as_index=False)["attempts"].sum().sort_values("attempts", ascending=False)
            st.markdown("#### By failure reason")
            st.dataframe(by_reason, use_container_width=True, hide_index=True)

            # Group by email
            by_email = (
                df.groupby("email", as_index=False)
                  .agg(total_attempts=("attempts", "sum"),
                       last_attempt=("last_attempt", "max"))
                  .sort_values("total_attempts", ascending=False)
            )
            by_email["last_attempt"] = pd.to_datetime(by_email["last_attempt"]).dt.strftime("%Y-%m-%d %H:%M:%S")
            st.markdown("#### By email")
            st.dataframe(by_email, use_container_width=True, hide_index=True)

            # Suspicious activity callout
            suspicious = by_email[by_email["total_attempts"] >= 10]
            if not suspicious.empty:
                st.warning(
                    f"**{len(suspicious)} email(s) with 10+ failed attempts in the window.** "
                    "Investigate whether this is brute-force activity or legitimate password issues."
                )