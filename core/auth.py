from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

import streamlit as st

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHashError
except Exception:  # pragma: no cover
    PasswordHasher = None  # type: ignore
    VerifyMismatchError = InvalidHashError = Exception  # type: ignore

from core.db import run_query, now_iso, log_audit, df_query

SESSION_TIMEOUT_MINUTES = int(os.environ.get("PROCUREFLOW_SESSION_TIMEOUT_MINUTES", "60"))
PRODUCTION_MODE = os.environ.get("PROCUREFLOW_PRODUCTION", "0") == "1"

# Stdlib PBKDF2 password hashing. This avoids the passlib/bcrypt 72-byte password
# error that can occur with newer bcrypt releases while still being much stronger
# than the original MVP's plain SHA256 hashing.
PBKDF2_ITERATIONS = 260_000
LOGIN_LOCKOUT_ATTEMPTS = int(os.environ.get("PROCUREFLOW_LOGIN_LOCKOUT_ATTEMPTS", "5"))
PASSWORD_HISTORY_COUNT = int(os.environ.get("PROCUREFLOW_PASSWORD_HISTORY_COUNT", "5"))
PASSWORD_MIN_LENGTH = 12 if PRODUCTION_MODE else 8
_ARGON2 = PasswordHasher() if PasswordHasher else None

from core.permissions import safe_role_permissions

ROLE_PERMISSIONS = {
    role: safe_role_permissions(role)
    for role in ["Admin", "Procurement Manager", "Facility Manager", "Logistics Officer", "Finance", "Approver", "Auditor"]
}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"))


def hash_password(password: str) -> str:
    """Use Argon2id for new/changed passwords with PBKDF2 legacy support."""
    if password is None:
        password = ""
    if _ARGON2 is not None:
        return _ARGON2.hash(password)
    # Safe fallback for constrained local environments. requirements pins argon2-cffi.
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
    """Verify Argon2id, PBKDF2 and original legacy SHA256 passwords."""
    if not stored_hash:
        return False
    if stored_hash.startswith("$argon2") and _ARGON2 is not None:
        try:
            return bool(_ARGON2.verify(stored_hash, password))
        except (VerifyMismatchError, InvalidHashError, ValueError):
            return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        return _verify_pbkdf2(password, stored_hash)
    if len(stored_hash) == 64 and stored_hash == _sha256_hash(password):
        return True
    if stored_hash.startswith("sha256$") and stored_hash.split("$", 1)[1] == _sha256_hash(password):
        return True
    return False


def _password_used_recently(user_id: int, candidate: str, current_hash: str) -> bool:
    hashes = [current_hash]
    try:
        rows = run_query(
            "SELECT password_hash FROM password_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (int(user_id), PASSWORD_HISTORY_COUNT), fetch=True,
        )
        hashes.extend(str(row["password_hash"]) for row in rows)
    except Exception:
        pass
    return any(verify_password(candidate, stored) for stored in hashes if stored)

def login_user(username: str, password: str):
    rows = run_query("SELECT * FROM users WHERE username = ? AND is_active = 1", (username.strip(),), fetch=True)
    if not rows:
        log_audit("LOGIN_FAILED", "User", None, {"username": username.strip()[:64]}, None, "System", after_values={"outcome": "unknown_user"})
        return None
    user = dict(rows[0])
    if int(user.get("account_locked") or 0):
        log_audit("LOGIN_DENIED_ACCOUNT_LOCKED", "User", user["id"], "Locked account login attempt", user["id"], user.get("role"), after_values={"outcome": "denied"})
        return None
    if verify_password(password, user["password_hash"]):
        # Rehash PBKDF2/SHA256 upon successful login when Argon2id is available.
        if not str(user["password_hash"]).startswith("$argon2") and _ARGON2 is not None:
            run_query("UPDATE users SET password_hash = ?, updated_at=? WHERE id = ?", (hash_password(password), now_iso(), user["id"]))
        seen = now_iso()
        run_query("UPDATE users SET last_login_at = ?, failed_login_count=0 WHERE id = ?", (seen, user["id"]))
        log_audit("LOGIN_SUCCESS", "User", user["id"], "Authenticated session created", user["id"], user.get("role"), after_values={"outcome": "success"})
        user["last_login_at"] = seen
        return user
    attempts = int(user.get("failed_login_count") or 0) + 1
    locked = attempts >= LOGIN_LOCKOUT_ATTEMPTS
    try:
        run_query(
            "UPDATE users SET failed_login_count=?, account_locked=?, updated_at=? WHERE id=?",
            (attempts, 1 if locked else 0, now_iso(), user["id"]),
        )
    except Exception:
        pass
    log_audit(
        "ACCOUNT_LOCKED" if locked else "LOGIN_FAILED",
        "User", user["id"],
        {"attempt": attempts, "lockout_threshold": LOGIN_LOCKOUT_ATTEMPTS},
        user["id"], user.get("role"),
        after_values={"outcome": "locked" if locked else "failed"},
    )
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


def _session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_persistent_session(user: dict) -> str:
    """Create a DB-backed server session without placing a token in the URL.

    Streamlit session state owns the opaque browser-session token. Production
    reverse proxies may add HttpOnly/Secure/SameSite cookies around this server
    session; the application never writes session credentials to query params.
    """
    token = secrets.token_urlsafe(32)
    token_hash = _session_token_hash(token)
    ts = now_iso()
    try:
        run_query(
            "INSERT INTO user_sessions (session_token, user_id, login_at, last_seen_at, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'Active', ?, ?)",
            (token_hash, int(user["id"]), ts, ts, ts, ts),
        )
    except Exception:
        return ""
    return token

def restore_user_from_session() -> bool:
    # Deliberately no URL token fallback. A browser refresh starts a new Streamlit
    # session and requires authentication unless deployed behind an approved
    # Secure/HttpOnly cookie session proxy.
    token = st.session_state.get("pf_session_token")
    if not token:
        return False
    token_hash = _session_token_hash(str(token))
    rows = run_query(
        """
        SELECT s.*, u.* FROM user_sessions s
        JOIN users u ON u.id=s.user_id
        WHERE s.session_token=? AND s.status='Active' AND u.is_active=1 AND (s.logout_at IS NULL OR s.logout_at='')
        ORDER BY s.id DESC LIMIT 1
        """,
        (token_hash,), fetch=True,
    )
    if not rows:
        return False
    row = dict(rows[0])
    last_seen = row.get("last_seen_at") or row.get("login_at")
    try:
        last = datetime.fromisoformat(last_seen)
        if datetime.now() - last > timedelta(minutes=SESSION_TIMEOUT_MINUTES):
            run_query("UPDATE user_sessions SET status='Expired', logout_at=?, updated_at=? WHERE session_token=?", (now_iso(), now_iso(), token_hash))
            log_audit("SESSION_EXPIRED", "User", row.get("user_id"), "Session expired", row.get("user_id"), row.get("role"))
            return False
    except Exception:
        pass
    user = {k: row[k] for k in row.keys() if k in {"id", "username", "full_name", "role", "password_hash", "must_change_password", "is_active", "last_login_at", "account_locked", "failed_login_count", "created_at", "updated_at"}}
    st.session_state["user"] = user
    st.session_state["last_seen_at"] = datetime.now().isoformat(timespec="seconds")
    run_query("UPDATE user_sessions SET last_seen_at=?, updated_at=? WHERE session_token=?", (now_iso(), now_iso(), token_hash))
    return True


def close_persistent_session():
    token = st.session_state.get("pf_session_token")
    if token:
        token_hash = _session_token_hash(str(token))
        try:
            run_query("UPDATE user_sessions SET logout_at=?, last_seen_at=?, status='Logged Out', updated_at=? WHERE session_token=?", (now_iso(), now_iso(), now_iso(), token_hash))
        except Exception:
            pass
    st.session_state.pop("pf_session_token", None)
    # pf_section/pf_role are non-sensitive navigation hints only.

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
    token = st.session_state.get("pf_session_token")
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
            run_query("UPDATE user_sessions SET last_seen_at=?, updated_at=? WHERE session_token=?", (ts, ts, _session_token_hash(str(token))))
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
            | Logistics Officer | `logistics` | `logistics123` |
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
        elif len(new) < PASSWORD_MIN_LENGTH:
            st.error(f"Use at least {PASSWORD_MIN_LENGTH} characters.")
        elif new != confirm:
            st.error("Passwords do not match.")
        elif _password_used_recently(int(user["id"]), new, str(rows[0]["password_hash"])):
            st.error("Choose a password that has not been used recently.")
        else:
            prior_hash = str(rows[0]["password_hash"])
            try:
                run_query("INSERT INTO password_history (user_id, password_hash, created_at) VALUES (?, ?, ?)", (user["id"], prior_hash, now_iso()))
                run_query("DELETE FROM password_history WHERE id NOT IN (SELECT id FROM password_history WHERE user_id=? ORDER BY created_at DESC LIMIT ? ) AND user_id=?", (user["id"], PASSWORD_HISTORY_COUNT, user["id"]))
            except Exception:
                pass
            run_query("UPDATE users SET password_hash = ?, must_change_password = 0, updated_at=? WHERE id = ?", (hash_password(new), now_iso(), user["id"]))
            log_audit("PASSWORD_CHANGE", "User", user["id"], "Password changed", user["id"])
            st.success("Password changed.")
