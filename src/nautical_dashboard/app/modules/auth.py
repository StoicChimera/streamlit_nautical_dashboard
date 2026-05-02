"""
auth.py — app-level authentication

Username + password login backed by dim_app_users with bcrypt hashes.
Session persists via signed cookie. 8-hour idle timeout. Account lockout
after 5 failed attempts. All login attempts logged to auth_audit_log.

Public surface:
    require_login()       — call once at the top of every page
    current_user()        — returns dict with email/name/role
    require_role(*roles)  — halt if role not allowed
    has_role(*roles)      — non-blocking bool check
    logout()              — clear session, redirect to login
"""

import os
import secrets
import bcrypt
import pandas as pd
import streamlit as st
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from streamlit_cookies_controller import CookieController

load_dotenv()
SUPABASE_CONN = os.getenv("SUPABASE_CONN")
if not SUPABASE_CONN:
    raise RuntimeError("Missing SUPABASE_CONN environment variable.")

_engine = create_engine(SUPABASE_CONN)

# Configuration
IDLE_TIMEOUT_HOURS = 8
MAX_FAILED_LOGINS  = 5
LOCKOUT_MINUTES    = 15
SESSION_COOKIE     = "nautical_session"

_cookies = CookieController()


# ============================================================
# Password verification
# ============================================================

def _verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


# ============================================================
# Audit log
# ============================================================

def _log_attempt(email: str, success: bool, reason: str = None):
    try:
        with _engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO auth_audit_log (email, success, reason)
                VALUES (:email, :success, :reason)
            """), {"email": email.lower(), "success": success, "reason": reason})
    except Exception:
        pass  # Never let audit logging break login


# ============================================================
# Lockout management
# ============================================================

def _check_lockout(email: str) -> tuple[bool, str | None]:
    """Returns (is_locked, message)."""
    df = pd.read_sql(text("""
        SELECT failed_login_count, lockout_until
        FROM dim_app_users WHERE LOWER(email) = LOWER(:email)
    """), _engine, params={"email": email})
    if df.empty:
        return False, None
    lockout_until = df.iloc[0]["lockout_until"]
    if pd.notna(lockout_until):
        lockout_dt = pd.to_datetime(lockout_until, utc=True)
        if lockout_dt > datetime.now(timezone.utc):
            mins = int((lockout_dt - datetime.now(timezone.utc)).total_seconds() / 60) + 1
            return True, f"Account locked. Try again in {mins} minute(s)."
    return False, None


def _record_failed_login(email: str):
    with _engine.begin() as conn:
        conn.execute(text("""
            UPDATE dim_app_users
            SET failed_login_count = failed_login_count + 1,
                lockout_until = CASE
                    WHEN failed_login_count + 1 >= :max_attempts
                    THEN NOW() + INTERVAL ':lockout_min minutes'
                    ELSE lockout_until
                END
            WHERE LOWER(email) = LOWER(:email)
        """), {
            "email": email,
            "max_attempts": MAX_FAILED_LOGINS,
            "lockout_min": LOCKOUT_MINUTES,
        })


def _reset_failed_logins(email: str):
    with _engine.begin() as conn:
        conn.execute(text("""
            UPDATE dim_app_users
            SET failed_login_count = 0,
                lockout_until      = NULL,
                last_login_at      = NOW()
            WHERE LOWER(email) = LOWER(:email)
        """), {"email": email})


# ============================================================
# User lookup
# ============================================================

def _lookup_user(email: str) -> dict | None:
    df = pd.read_sql(text("""
        SELECT email, name, role, pw_hash, must_change_pw, active
        FROM dim_app_users WHERE LOWER(email) = LOWER(:email)
    """), _engine, params={"email": email})
    if df.empty:
        return None
    row = df.iloc[0]
    if not bool(row["active"]):
        return None
    return {
        "email":          str(row["email"]),
        "name":           str(row["name"]),
        "role":           str(row["role"]),
        "pw_hash":        str(row["pw_hash"]) if pd.notna(row["pw_hash"]) else None,
        "must_change_pw": bool(row["must_change_pw"]),
    }


# ============================================================
# Session management via cookie
# ============================================================

def _make_session_token() -> str:
    return secrets.token_urlsafe(32)


def _save_session(email: str, name: str, role: str):
    expires = datetime.now(timezone.utc) + timedelta(hours=IDLE_TIMEOUT_HOURS)
    session = {
        "email":   email,
        "name":    name,
        "role":    role,
        "expires": expires.isoformat(),
    }
    st.session_state["_auth_user"] = session
    _cookies.set(SESSION_COOKIE, session, expires=expires)


def _refresh_session():
    """Extend expiry on activity."""
    user = st.session_state.get("_auth_user")
    if not user:
        return
    expires = datetime.now(timezone.utc) + timedelta(hours=IDLE_TIMEOUT_HOURS)
    user["expires"] = expires.isoformat()
    st.session_state["_auth_user"] = user
    _cookies.set(SESSION_COOKIE, user, expires=expires)


def _load_session() -> dict | None:
    """Load session from cookie if valid, else None."""
    if "_auth_user" in st.session_state:
        user = st.session_state["_auth_user"]
        if _is_session_valid(user):
            return user
        else:
            del st.session_state["_auth_user"]

    cookie = _cookies.get(SESSION_COOKIE)
    if not cookie or not isinstance(cookie, dict):
        return None
    if not _is_session_valid(cookie):
        _cookies.remove(SESSION_COOKIE)
        return None

    st.session_state["_auth_user"] = cookie
    return cookie


def _is_session_valid(user: dict) -> bool:
    if not user or "expires" not in user:
        return False
    try:
        expires = datetime.fromisoformat(user["expires"])
        return expires > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _clear_session():
    if "_auth_user" in st.session_state:
        del st.session_state["_auth_user"]
    _cookies.remove(SESSION_COOKIE)


# ============================================================
# Login form
# ============================================================

def _render_login_form():
    st.title("Nautical Financial Platform")
    st.caption("Sign in to continue")

    with st.form("login_form", clear_on_submit=False):
        email = st.text_input("Email", key="login_email").strip().lower()
        password = st.text_input("Password", type="password", key="login_password")
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if not submitted:
        st.stop()

    if not email or not password:
        st.error("Email and password required.")
        st.stop()

    is_locked, lock_msg = _check_lockout(email)
    if is_locked:
        _log_attempt(email, False, "lockout")
        st.error(lock_msg)
        st.stop()

    user = _lookup_user(email)
    if not user:
        _log_attempt(email, False, "unknown_email")
        st.error("Invalid email or password.")
        st.stop()

    if not user["pw_hash"]:
        _log_attempt(email, False, "no_password_set")
        st.error("Account exists but no password set. Contact admin.")
        st.stop()

    if not _verify_password(password, user["pw_hash"]):
        _record_failed_login(email)
        _log_attempt(email, False, "bad_password")
        st.error("Invalid email or password.")
        st.stop()

    # Success
    _reset_failed_logins(email)
    _log_attempt(email, True)

    if user["must_change_pw"]:
        st.session_state["_pending_pw_change"] = user
        st.rerun()

    _save_session(user["email"], user["name"], user["role"])
    st.rerun()


def _render_password_change():
    user = st.session_state["_pending_pw_change"]

    st.title("Change Password")
    st.info(f"First-time login for **{user['name']}**. Set a new password to continue.")

    with st.form("pw_change_form"):
        new_pw = st.text_input("New password", type="password", key="new_pw")
        confirm = st.text_input("Confirm password", type="password", key="confirm_pw")
        submitted = st.form_submit_button("Set password", type="primary", use_container_width=True)

    if not submitted:
        st.stop()

    if len(new_pw) < 12:
        st.error("Password must be at least 12 characters.")
        st.stop()
    if new_pw != confirm:
        st.error("Passwords do not match.")
        st.stop()

    new_hash = _hash_password(new_pw)
    with _engine.begin() as conn:
        conn.execute(text("""
            UPDATE dim_app_users
            SET pw_hash = :pw_hash, must_change_pw = FALSE
            WHERE LOWER(email) = LOWER(:email)
        """), {"pw_hash": new_hash, "email": user["email"]})

    del st.session_state["_pending_pw_change"]
    _save_session(user["email"], user["name"], user["role"])
    st.success("Password changed. Loading app...")
    st.rerun()


# ============================================================
# Public API
# ============================================================

def require_login() -> dict:
    """
    Call at the top of every page. Returns user dict or halts the page.
    Handles login form, password change, session loading, idle timeout.
    """
    if "_pending_pw_change" in st.session_state:
        _render_password_change()
        st.stop()

    user = _load_session()
    if not user:
        _render_login_form()
        st.stop()

    _refresh_session()
    return user


def current_user() -> dict:
    user = st.session_state.get("_auth_user")
    if not user:
        raise RuntimeError("current_user() called before require_login()")
    return user


def require_role(*allowed_roles: str) -> None:
    user = current_user()
    if user["role"] not in allowed_roles:
        st.error(
            f"Your role (`{user['role']}`) does not have access to this view. "
            f"Required: {', '.join(allowed_roles)}."
        )
        st.stop()


def has_role(*allowed_roles: str) -> bool:
    user = current_user()
    return user["role"] in allowed_roles


def logout():
    _clear_session()
    st.rerun()


def render_logout_button():
    user = current_user()
    with st.sidebar:
        st.markdown(f"**{user['name']}**  ·  `{user['role']}`")
        if st.button("Sign out", key="logout_btn", use_container_width=True):
            logout()