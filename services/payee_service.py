"""Encrypted payment-payee details for purchase-request drafts.

This module deliberately contains no Streamlit UI. It can be used by the
Procurement Manager and Facility/Utility draft forms without creating a new
Finance/Approver/Logistics page.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from core.db import _append_audit_event_to_conn, get_conn, json_dump, now_iso
from services.security_service import (
    decrypt_text,
    encrypt_text,
    fingerprint,
    mask_account_number,
    mask_name,
    redact_value,
)


class PayeeValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PayeeSaveResult:
    payee_detail_id: int
    verification_status: str
    payment_readiness_status: str
    duplicate_warning: bool


def _clean(value: Any) -> str:
    return str(value or "").strip()


def validate_payee_payload(payload: dict[str, Any]) -> dict[str, Any]:
    known = bool(payload.get("recipient_known"))
    currency = _clean(payload.get("currency") or "NGN").upper()
    data = {
        "recipient_known": known,
        "payee_type": _clean(payload.get("payee_type")) or "Vendor",
        "payee_name": _clean(payload.get("payee_name")),
        "account_name": _clean(payload.get("account_name")),
        "bank_name": _clean(payload.get("bank_name")),
        "account_number": re.sub(r"\s+", "", _clean(payload.get("account_number"))),
        "currency": currency,
        "payment_reference": _clean(payload.get("payment_reference")),
        "contact_email": _clean(payload.get("contact_email")),
        "contact_phone": _clean(payload.get("contact_phone")),
        "confirmation": bool(payload.get("confirmation")),
        "delayed_reason": _clean(payload.get("delayed_reason")),
        "source_attachment_path": payload.get("source_attachment_path"),
        "source_attachment_hash": payload.get("source_attachment_hash"),
    }
    if known:
        missing = [
            label for label, value in {
                "Payee Full / Legal Name": data["payee_name"],
                "Account Name": data["account_name"],
                "Bank Name": data["bank_name"],
                "Account Number": data["account_number"],
            }.items() if not value
        ]
        if missing:
            raise PayeeValidationError("Complete the following fields: " + ", ".join(missing) + ".")
        if currency == "NGN" and not re.fullmatch(r"\d{10}", data["account_number"]):
            raise PayeeValidationError("NGN account numbers must contain exactly 10 numeric digits.")
        if not data["confirmation"]:
            raise PayeeValidationError("Confirm that the payment details came from an authorized source.")
    else:
        if not data["delayed_reason"]:
            raise PayeeValidationError("Give a reason for delayed payee details.")
    return data


def _masked_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "recipient_known": int(row.get("recipient_known") or 0),
        "payee_type": row.get("payee_type"),
        "payee_name": row.get("payee_name_masked"),
        "account_name": row.get("account_name_masked"),
        "bank_name": row.get("bank_name_masked"),
        "account_number": "******" + str(row.get("account_number_last4") or ""),
        "currency": row.get("currency"),
        "payment_readiness_status": row.get("payment_readiness_status"),
        "verification_status": row.get("verification_status"),
    }


def _insert_version(conn, payee_id: int, action: str, snapshot: dict[str, Any], actor_user_id: int | None, reason: str | None = None) -> None:
    row = conn.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version FROM payment_payee_detail_versions WHERE payee_detail_id=?",
        (payee_id,),
    ).fetchone()
    version = int(row["next_version"] if row else 1)
    conn.execute(
        """
        INSERT INTO payment_payee_detail_versions
        (payee_detail_id, version_no, action, values_redacted_json, changed_by_user_id, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (payee_id, version, action, json_dump(redact_value(snapshot)), actor_user_id, reason, now_iso()),
    )


def save_payee_details(
    purchase_request_id: int,
    payload: dict[str, Any],
    actor_user_id: int,
    actor_role: str,
    *,
    reason: str | None = None,
) -> PayeeSaveResult:
    """Create or version secure payee data in one database transaction."""
    data = validate_payee_payload(payload)
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM payment_payee_details WHERE purchase_request_id=? ORDER BY id DESC LIMIT 1",
            (int(purchase_request_id),),
        ).fetchone()
        account_fp = fingerprint(data["account_number"]) if data["recipient_known"] else None
        duplicate = False
        if account_fp:
            dupe = conn.execute(
                "SELECT id FROM payment_payee_details WHERE account_number_fingerprint=? AND purchase_request_id<>? LIMIT 1",
                (account_fp, int(purchase_request_id)),
            ).fetchone()
            duplicate = bool(dupe)
        verification = "Requester Confirmed" if data["recipient_known"] else "Pending"
        readiness = "Pending Finance Verification" if data["recipient_known"] else "Pending Payee Details"
        values = {
            "payee_type": data["payee_type"],
            "payee_name_encrypted": encrypt_text(data["payee_name"]) if data["recipient_known"] else None,
            "payee_name_masked": mask_name(data["payee_name"]) if data["recipient_known"] else "Pending",
            "account_name_encrypted": encrypt_text(data["account_name"]) if data["recipient_known"] else None,
            "account_name_masked": mask_name(data["account_name"]) if data["recipient_known"] else "Pending",
            "bank_name_encrypted": encrypt_text(data["bank_name"]) if data["recipient_known"] else None,
            "bank_name_masked": mask_name(data["bank_name"]) if data["recipient_known"] else "Pending",
            "account_number_encrypted": encrypt_text(data["account_number"]) if data["recipient_known"] else None,
            "account_number_last4": data["account_number"][-4:] if data["recipient_known"] else None,
            "account_number_fingerprint": account_fp,
            "currency": data["currency"],
            "payment_reference_encrypted": encrypt_text(data["payment_reference"]) if data["payment_reference"] else None,
            "contact_email_encrypted": encrypt_text(data["contact_email"]) if data["contact_email"] else None,
            "contact_phone_encrypted": encrypt_text(data["contact_phone"]) if data["contact_phone"] else None,
            "recipient_known": int(data["recipient_known"]),
            "payment_readiness_status": readiness,
            "verification_status": verification,
            "confirmed_by_user_id": actor_user_id if data["recipient_known"] else None,
            "confirmed_at": now_iso() if data["recipient_known"] else None,
            "rejected_reason_encrypted": encrypt_text(data["delayed_reason"]) if not data["recipient_known"] else None,
            "source_attachment_path": data["source_attachment_path"],
            "source_attachment_hash": data["source_attachment_hash"],
            "updated_by_user_id": actor_user_id,
            "updated_at": now_iso(),
        }
        before = _masked_snapshot(dict(existing)) if existing else {}
        if existing:
            assignments = ", ".join(f"{column}=?" for column in values)
            conn.execute(
                f"UPDATE payment_payee_details SET {assignments} WHERE id=?",
                tuple(values.values()) + (int(existing["id"]),),
            )
            payee_id = int(existing["id"])
            action = "PAYEE_DETAILS_UPDATED"
        else:
            columns = ["purchase_request_id", *values.keys(), "created_by_user_id", "created_at"]
            placeholders = ", ".join(["?"] * len(columns))
            conn.execute(
                f"INSERT INTO payment_payee_details ({', '.join(columns)}) VALUES ({placeholders})",
                (int(purchase_request_id), *values.values(), actor_user_id, now_iso()),
            )
            payee_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            action = "PAYEE_DETAILS_CREATED"
        latest = dict(conn.execute("SELECT * FROM payment_payee_details WHERE id=?", (payee_id,)).fetchone())
        snapshot = _masked_snapshot(latest)
        _insert_version(conn, payee_id, action, snapshot, actor_user_id, reason)
        _append_audit_event_to_conn(
            conn,
            action=action,
            entity_type="Payment Payee Details",
            entity_id=payee_id,
            parent_entity_type="Purchase Request",
            parent_entity_id=int(purchase_request_id),
            user_id=actor_user_id,
            role=actor_role,
            before_values=before,
            after_values=snapshot,
            details={"purchase_request_id": purchase_request_id, "duplicate_warning": duplicate},
            outcome="Success",
            severity="High" if duplicate else "Normal",
            source="payee_service",
            reason_or_comment=reason,
        )
        if data["recipient_known"]:
            _append_audit_event_to_conn(
                conn,
                action="PAYEE_DETAILS_REQUESTER_CONFIRMED",
                entity_type="Payment Payee Details",
                entity_id=payee_id,
                parent_entity_type="Purchase Request",
                parent_entity_id=int(purchase_request_id),
                user_id=actor_user_id,
                role=actor_role,
                details={"verification_status": verification},
                source="payee_service",
            )
        if duplicate:
            _append_audit_event_to_conn(
                conn,
                action="PAYEE_DETAILS_DUPLICATE_WARNING",
                entity_type="Payment Payee Details",
                entity_id=payee_id,
                parent_entity_type="Purchase Request",
                parent_entity_id=int(purchase_request_id),
                user_id=actor_user_id,
                role=actor_role,
                details={"duplicate_fingerprint_detected": True},
                outcome="Warning",
                severity="High",
                source="payee_service",
            )
        conn.commit()
        return PayeeSaveResult(payee_id, verification, readiness, duplicate)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_masked_payee_for_request(purchase_request_id: int) -> dict[str, Any] | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM payment_payee_details WHERE purchase_request_id=? ORDER BY id DESC LIMIT 1",
            (int(purchase_request_id),),
        ).fetchone()
        return _masked_snapshot(dict(row)) | {"id": int(row["id"])} if row else None
    finally:
        conn.close()


def get_full_payee_details(payee_id: int) -> dict[str, Any] | None:
    """Backend-only retrieval for authorized payment processing services."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM payment_payee_details WHERE id=?", (int(payee_id),)).fetchone()
        if not row:
            return None
        data = dict(row)
        for field in ["payee_name", "account_name", "bank_name", "account_number", "payment_reference", "contact_email", "contact_phone"]:
            encrypted = data.get(f"{field}_encrypted")
            data[field] = decrypt_text(encrypted) if encrypted else None
        return data
    finally:
        conn.close()


def audit_payee_reveal(payee_id: int, actor_user_id: int, actor_role: str, reason: str) -> None:
    if not reason.strip():
        raise PayeeValidationError("A reason is required before sensitive payment details can be revealed.")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _append_audit_event_to_conn(
            conn,
            action="PAYEE_DETAILS_REVEALED",
            entity_type="Payment Payee Details",
            entity_id=int(payee_id),
            user_id=actor_user_id,
            role=actor_role,
            details={"reveal": "time-limited"},
            outcome="Success",
            severity="High",
            source="payee_service",
            reason_or_comment=reason,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

class PaymentPayeeNotReadyError(RuntimeError):
    """Raised when a draft deliberately marked as payee-pending reaches payment."""


def _record_payment_blocked(
    purchase_request_id: int,
    payee_id: int | None,
    actor_user_id: int,
    actor_role: str,
    reason: str,
) -> None:
    """Persist an immutable denial event without exposing payment details."""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _append_audit_event_to_conn(
            conn,
            action="PAYEE_DETAILS_PAYMENT_BLOCKED",
            entity_type="Payment Payee Details",
            entity_id=payee_id,
            parent_entity_type="Purchase Request",
            parent_entity_id=int(purchase_request_id),
            user_id=actor_user_id,
            role=actor_role,
            details={"payment_readiness": "blocked"},
            outcome="Denied",
            severity="High",
            source="payee_service",
            reason_or_comment=reason,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_payee_details(
    purchase_request_id: int,
    actor_user_id: int,
    actor_role: str,
    *,
    reason: str = "Verified during authorized Finance payment processing.",
) -> dict[str, Any]:
    """Mark a requester-confirmed recipient as Finance verified.

    This backend command intentionally does not add a Finance page/tab. It is
    called by the existing authorized payment action and writes its own version
    history and evidence event.
    """
    if actor_role not in {"Finance", "Admin"}:
        raise PaymentPayeeNotReadyError("Only authorized Finance personnel can verify payment recipient details.")
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM payment_payee_details WHERE purchase_request_id=? ORDER BY id DESC LIMIT 1",
            (int(purchase_request_id),),
        ).fetchone()
        if not row:
            conn.commit()
            return {"status": "No payee record"}
        payee = dict(row)
        if not int(payee.get("recipient_known") or 0):
            conn.rollback()
            _record_payment_blocked(purchase_request_id, int(payee["id"]), actor_user_id, actor_role, "Recipient details are still pending.")
            raise PaymentPayeeNotReadyError("Payment is blocked because payment recipient details are still pending.")
        if str(payee.get("verification_status") or "") == "Rejected":
            conn.rollback()
            _record_payment_blocked(purchase_request_id, int(payee["id"]), actor_user_id, actor_role, "Recipient details were rejected and must be corrected.")
            raise PaymentPayeeNotReadyError("Payment is blocked because payment recipient details were rejected.")
        if str(payee.get("verification_status") or "") == "Finance Verified":
            conn.commit()
            return _masked_snapshot(payee)
        before = _masked_snapshot(payee)
        ts = now_iso()
        conn.execute(
            """
            UPDATE payment_payee_details
            SET verification_status='Finance Verified', payment_readiness_status='Payment Ready',
                verified_by_user_id=?, verified_at=?, updated_by_user_id=?, updated_at=?
            WHERE id=?
            """,
            (int(actor_user_id), ts, int(actor_user_id), ts, int(payee["id"])),
        )
        latest = dict(conn.execute("SELECT * FROM payment_payee_details WHERE id=?", (int(payee["id"]),)).fetchone())
        snapshot = _masked_snapshot(latest)
        _insert_version(conn, int(payee["id"]), "PAYEE_DETAILS_FINANCE_VERIFIED", snapshot, actor_user_id, reason)
        _append_audit_event_to_conn(
            conn,
            action="PAYEE_DETAILS_FINANCE_VERIFIED",
            entity_type="Payment Payee Details",
            entity_id=int(payee["id"]),
            parent_entity_type="Purchase Request",
            parent_entity_id=int(purchase_request_id),
            user_id=actor_user_id,
            role=actor_role,
            before_values=before,
            after_values=snapshot,
            details={"payment_readiness_status": "Payment Ready"},
            source="payee_service",
            reason_or_comment=reason,
        )
        conn.commit()
        return snapshot
    except PaymentPayeeNotReadyError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def assert_request_payee_payment_ready(
    purchase_request_id: int | None,
    actor_user_id: int,
    actor_role: str,
) -> None:
    """Block payment only for a request that has an explicitly incomplete payee record.

    Historic records without a payee-details row remain compatible. New drafts
    always create a row, so a requester-selected 'unknown recipient' cannot be
    paid until authorized Finance processing verifies the supplied details.
    """
    if not purchase_request_id:
        return
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM payment_payee_details WHERE purchase_request_id=? ORDER BY id DESC LIMIT 1",
            (int(purchase_request_id),),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return
    payee = dict(rows[0])
    if not int(payee.get("recipient_known") or 0):
        _record_payment_blocked(int(purchase_request_id), int(payee["id"]), actor_user_id, actor_role, "Recipient details are pending.")
        raise PaymentPayeeNotReadyError("Payment is blocked until payment recipient details are completed and verified.")
    if str(payee.get("verification_status") or "") == "Rejected":
        _record_payment_blocked(int(purchase_request_id), int(payee["id"]), actor_user_id, actor_role, "Recipient details were rejected.")
        raise PaymentPayeeNotReadyError("Payment is blocked until rejected payment recipient details are corrected.")
    if str(payee.get("verification_status") or "") != "Finance Verified":
        verify_payee_details(int(purchase_request_id), actor_user_id, actor_role)
