"""Security primitives used by ProcureFlow's audit and payee services.

Keys are always sourced from the environment in production. Local/demo runs
create a private key file under ``data/`` on first boot so that bank detail
records remain encrypted across restarts without embedding a secret in code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

try:  # Installed through requirements.txt for production deployments.
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover - clear error returned when feature is used
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
LOCAL_KEY_FILE = DATA_DIR / ".procureflow_local_encryption.key"
PRODUCTION_MODE = os.environ.get("PROCUREFLOW_PRODUCTION", "0") == "1"
PAYEE_KEY_ENV = "PROCUREFLOW_PAYEE_ENCRYPTION_KEY"
AUDIT_KEY_ENV = "PROCUREFLOW_AUDIT_SIGNING_KEY"

SENSITIVE_KEY_PARTS = {
    "password", "password_hash", "token", "secret", "api_key", "authorization",
    "session", "account_number", "account_no", "bank_account", "raw_private_message",
    "message_text", "private_details", "payment_reference", "contact_email", "contact_phone",
    "payee_name_encrypted", "account_name_encrypted", "bank_name_encrypted",
    "account_number_encrypted", "rejected_reason_encrypted",
}


def _normalise_key_material(value: str) -> bytes:
    """Return a valid Fernet key from either a Fernet key or secret text."""
    raw = value.encode("utf-8")
    try:
        # A valid Fernet key must decode to exactly 32 bytes.
        decoded = base64.urlsafe_b64decode(raw)
        if len(decoded) == 32:
            return raw
    except Exception:
        pass
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def _local_key() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCAL_KEY_FILE.exists():
        return LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
    key = Fernet.generate_key().decode("ascii") if Fernet else base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    LOCAL_KEY_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(LOCAL_KEY_FILE, 0o600)
    except Exception:
        pass
    return key


def encryption_key() -> bytes:
    configured = os.environ.get(PAYEE_KEY_ENV, "").strip()
    if configured:
        return _normalise_key_material(configured)
    if PRODUCTION_MODE:
        raise RuntimeError(f"{PAYEE_KEY_ENV} must be configured in production.")
    return _normalise_key_material(_local_key())


def audit_signing_key() -> bytes:
    configured = os.environ.get(AUDIT_KEY_ENV, "").strip()
    if configured:
        return configured.encode("utf-8")
    if PRODUCTION_MODE:
        raise RuntimeError(f"{AUDIT_KEY_ENV} must be configured in production.")
    # A local generated encryption key can safely derive the local audit key.
    return hashlib.sha256(encryption_key() + b":audit-chain").digest()


def encrypt_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    if Fernet is None:
        raise RuntimeError("cryptography is required for encrypted payee details.")
    return Fernet(encryption_key()).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(value: str | None) -> str | None:
    if not value:
        return None
    if Fernet is None:
        raise RuntimeError("cryptography is required for encrypted payee details.")
    try:
        return Fernet(encryption_key()).decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("The encrypted record cannot be decrypted with the active key.") from exc


def fingerprint(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", "", str(value)).upper().encode("utf-8")
    return hmac.new(audit_signing_key(), b"fingerprint:" + normalized, hashlib.sha256).hexdigest()


def mask_name(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        return "—"
    words = value.split()
    masked = []
    for word in words:
        if len(word) <= 1:
            masked.append("*")
        else:
            masked.append(word[0] + "*" * max(2, len(word) - 1))
    return " ".join(masked)


def mask_account_number(value: str | None) -> str:
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return "—"
    return "*" * max(0, len(digits) - 4) + digits[-4:]


def redact_value(value: Any, key_hint: str | None = None) -> Any:
    """Recursively redact sensitive values for audit/export/error contexts."""
    key_lower = (key_hint or "").lower()
    if "account_number" in key_lower or "account_no" in key_lower:
        # A deliberately masked value is safe to retain in Auditor evidence.
        if isinstance(value, str) and "*" in value:
            return value
        if key_lower in {"account_number_last4", "last4"}:
            return str(value or "")[-4:]
        return "[REDACTED]"
    if any(token in key_lower for token in SENSITIVE_KEY_PARTS):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [redact_value(v, key_hint) for v in value]
    if isinstance(value, bytes):
        return "[BINARY REDACTED]"
    if isinstance(value, str):
        # Mask likely account-number patterns even when field labels were lost.
        if re.fullmatch(r"\d{10,18}", value.strip()):
            return mask_account_number(value)
        return value
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(redact_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def safe_error_id() -> str:
    return f"PF-{uuid.uuid4().hex[:12].upper()}"
