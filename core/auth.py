from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

import streamlit as st

from core.db import run_query, now_iso, log_audit, df_query

SESSION_TIMEOUT_MINUTES = int(os.environ.get("PROCUREFLOW_SESSION_TIMEOUT_MINUTES", "60"))
PRODUCTION_MODE = os.environ.get("PROCUREFLOW_PRODUCTION", "0") == "1"

# Stdlib PBKDF2 password hashing. This avoids the passlib/bcrypt 72-byte password
# error that can occur with newer bcrypt releases while still being much stronger
# than the original MVP's plain SHA256 hashing.
PBKDF2_ITERATIONS = 260_000

from core.permissions import safe_role_permissions

ROLE_PERMISSIONS = {
    role: safe_role_permissions(role)
    for role in ["Admin", "Procurement Manager", "Facility Manager", "Finance", "Approver", "Auditor"]
}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def hash_password(password: str) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256.

    Format: pbkdf2_sha256$iterations$salt_b64$hash_b64
    """
    if password is None:
        password = ""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def _sha256_hash(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _verify_pbkdf2(password: str, stored_hash: str) -> bool:
    try:
        _scheme, iterations, salt_b64, hash_b64 = stored_hash.split("$", 3)
        iterations = int(iterations)
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify current PBKDF2 hashes and legacy ProcureFlow SHA256 hashes."""
    if not stored_hash:
        return False

    if stored_hash.startswith("pbkdf2_sha256$"):
        return _verify_pbkdf2(password, stored_hash)

    # Compatibility with original MVP SHA256 hashes.
    if len(stored_hash) == 64 and stored_hash == _sha256_hash(password):
        return True
    if stored_hash.startswith("sha256$") and stored_hash.split("$", 1)[1] == _sha256_hash(password):
        return True

    return False


def login_user(username: str, password: str):
    rows = run_query("SELECT * FROM users WHERE username = ? AND is_active = 1", (username.strip(),), fetch=True)
    if not rows:
        return None
    user = dict(rows[0])
    if int(user.get("account_locked") or 0):
        return None
    if verify_password(password, user["password_hash"]):
        # Upgrade legacy SHA256 hashes automatically after successful login.
        if not str(user["password_hash"]).startswith("pbkdf2_sha256$"):
            run_query("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
        seen = now_iso()
        run_query("UPDATE users SET last_login_at = ?, failed_login_count=0 WHERE id = ?", (seen, user["id"]))
        user["last_login_at"] = seen
        return user
    try:
        run_query("UPDATE users SET failed_login_count=COALESCE(failed_login_count,0)+1 WHERE id=?", (user["id"],))
    except Exception:
        pass
    return None



def _get_query_param(name: str):
    try:
        value = st.query_params.get(name)
        if isinstance(value, list):
            return value[0] if value else None
        return value
    except Exception:
        try:
            params = st.experimental_get_query_params()
            return params.get(name, [None])[0]
        except Exception:
            return None


def _set_query_param(name: str, value: str | None):
    try:
        if value is None:
            if name in st.query_params:
                del st.query_params[name]
        else:
            st.query_params[name] = value
    except Exception:
        try:
            params = st.experimental_get_query_params()
            if value is None:
                params.pop(name, None)
            else:
                params[name] = value
            st.experimental_set_query_params(**params)
        except Exception:
            pass


def create_persistent_session(user: dict) -> str:
    """Create a lightweight DB-backed session token and keep it in the URL.

    This allows browser refresh to restore the logged-in user and the current
    sidebar section. Logout revokes the token.
    """
    token = secrets.token_urlsafe(32)
    ts = now_iso()
    try:
        sid = run_query(
            "INSERT INTO user_sessions (session_token, user_id, login_at, last_seen_at, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'Active', ?, ?)",
            (token, int(user["id"]), ts, ts, ts, ts),
        )
    except Exception:
        # Older DBs will be migrated on app boot; keep login functional even if session table is unavailable.
        return ""
    _set_query_param("pf_session", token)
    return token


def restore_user_from_session() -> bool:
    token = _get_query_param("pf_session")
    if not token:
        return False
    rows = run_query(
        """
        SELECT s.*, u.* FROM user_sessions s
        JOIN users u ON u.id=s.user_id
        WHERE s.session_token=? AND s.status='Active' AND u.is_active=1 AND (s.logout_at IS NULL OR s.logout_at='')
        ORDER BY s.id DESC LIMIT 1
        """,
        (token,), fetch=True,
    )
    if not rows:
        return False
    row = dict(rows[0])
    last_seen = row.get("last_seen_at") or row.get("login_at")
    try:
        last = datetime.fromisoformat(last_seen)
        if datetime.now() - last > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            run_query("UPDATE user_sessions SET status='Expired', logout_at=?, updated_at=? WHERE session_token=?", (now_iso(), now_iso(), token))
            _set_query_param("pf_session", None)
            return False
    except Exception:
        pass
    user = {k: row[k] for k in row.keys() if k in {"id", "username", "full_name", "role", "password_hash", "must_change_password", "is_active", "last_login_at", "account_locked", "failed_login_count", "created_at", "updated_at"}}
    st.session_state["user"] = user
    st.session_state["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
    st.session_state["pf_session_token"] = token
    run_query("UPDATE user_sessions SET last_seen_at=?, updated_at=? WHERE session_token=?", (now_iso(), now_iso(), token))
    return True


def close_persistent_session():
    token = st.session_state.get("pf_session_token") or _get_query_param("pf_session")
    if token:
        try:
            run_query("UPDATE user_sessions SET logout_at=?, last_seen_at=?, status='Logged Out', updated_at=? WHERE session_token=?", (now_iso(), now_iso(), now_iso(), token))
        except Exception:
            pass
    _set_query_param("pf_session", None)
    _set_query_param("pf_section", None)
    _set_query_param("pf_role", None)


def has_permission(permission: str) -> bool:
    user = st.session_state.get("user")
    if not user:
        return False
    if permission in ROLE_PERMISSIONS.get(user["role"], set()):
        return True
    try:
        rows = run_query(
            "SELECT 1 FROM role_permissions WHERE role_name=? AND permission_name=? LIMIT 1",
            (user["role"], permission),
            fetch=True,
        )
        return bool(rows)
    except Exception:
        return False


def require_permission(permission: str) -> bool:
    if has_permission(permission):
        return True
    st.warning("You do not have permission to perform this action.")
    return False


def session_expired() -> bool:
    last_seen = st.session_state.get("last_seen_at")
    if not last_seen:
        return False
    try:
        last = datetime.fromisoformat(last_seen)
    except ValueError:
        return False
    return datetime.now() - last > timedelta(minutes=SESSION_TIMEOUT_MINUTES)


def require_user() -> bool:
    if "user" not in st.session_state:
        if not restore_user_from_session():
            return False
    if session_expired():
        close_persistent_session()
        st.session_state.clear()
        st.warning("Your session expired. Please log in again.")
        return False

    # Streamlit reruns on every click. Writing last_seen_at to SQLite on every
    # rerun made navigation feel sluggish after the app gained many badges and
    # dashboard panels. Keep the in-memory timestamp current, but persist the
    # DB heartbeat at most every 30 seconds. Login/logout audit times still
    # remain exact, and refresh restore still works through the session token.
    now_dt = datetime.now()
    st.session_state["last_seen_at"] = now_dt.isoformat(timespec="seconds")
    token = st.session_state.get("pf_session_token") or _get_query_param("pf_session")
    last_db_touch = st.session_state.get("pf_last_session_db_touch")
    should_touch = True
    if last_db_touch:
        try:
            should_touch = (now_dt - datetime.fromisoformat(last_db_touch)).total_seconds() >= 30
        except Exception:
            should_touch = True
    if token and should_touch:
        try:
            ts = now_iso()
            run_query("UPDATE user_sessions SET last_seen_at=?, updated_at=? WHERE session_token=?", (ts, ts, token))
            st.session_state["pf_last_session_db_touch"] = now_dt.isoformat(timespec="seconds")
        except Exception:
            pass
    return True


def login_panel():
    st.markdown("# ProcureFlow Procurement Workspace")
    st.caption("ServiceNow-inspired procurement command center for requests, sourcing, POs, receiving, invoices, expenses, cash advances, budgets, and audits.")
    with st.container(border=True):
        st.subheader("Login")
        username = st.text_input("Username", value="" if PRODUCTION_MODE else "admin")
        password = st.text_input("Password", type="password", value="" if PRODUCTION_MODE else "admin123")
        if st.button("Login", type="primary", use_container_width=True):
            user = login_user(username, password)
            if user:
                st.session_state["user"] = user
                st.session_state["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
                token = create_persistent_session(user)
                if token:
                    st.session_state["pf_session_token"] = token
                log_audit("LOGIN", "User", user["id"], "User logged in", user["id"], user.get("role"))
                st.rerun()
            else:
                st.error("Invalid username or password.")
    if not PRODUCTION_MODE:
        with st.expander("Local demo credentials"):
            st.markdown("""
            | Role | Username | Password |
            |---|---|---|
            | Admin | `admin` | `admin123` |
            | Procurement Manager | `procurement` | `procure123` |
            | Finance | `finance` | `finance123` |
            | Approver/MD | `approver` | `approve123` |
            | Auditor | `auditor` | `audit123` |
            | Utility Head / Facility Head | `facility` | `facility123` |
            """)


def logout_button():
    if st.button("Logout", use_container_width=True):
        current = st.session_state.get("user")
        if current:
            log_audit("LOGOUT", "User", current["id"], "User logged out", current["id"], current.get("role"))
        close_persistent_session()
        st.session_state.clear()
        st.rerun()


def change_password_panel():
    user = st.session_state.get("user")
    if not user:
        return
    with st.form("change_password_form"):
        current = st.text_input("Current password", type="password")
        new = st.text_input("New password", type="password")
        confirm = st.text_input("Confirm new password", type="password")
        submitted = st.form_submit_button("Change password")
    if submitted:
        rows = run_query("SELECT password_hash FROM users WHERE id = ?", (user["id"],), fetch=True)
        if not rows or not verify_password(current, rows[0]["password_hash"]):
            st.error("Current password is incorrect.")
        elif len(new) < 8:
            st.error("Use at least 8 characters.")
        elif new != confirm:
            st.error("Passwords do not match.")
        else:
            run_query("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?", (hash_password(new), user["id"]))
            log_audit("PASSWORD_CHANGE", "User", user["id"], "Password changed", user["id"])
            st.success("Password changed.")
