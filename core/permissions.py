"""Centralized role/permission rules for ProcureFlow.

The UI and services must use these helpers instead of independently deciding
approval, payment, delete, or read-only behavior.
"""
from __future__ import annotations

from typing import Mapping, Any

APPROVAL_ROLES = {"Admin", "Approver"}
PAYMENT_ROLES = {"Finance", "Admin"}
READ_ONLY_ROLES = {"Auditor"}
UTILITY_ROLE = "Facility Manager"  # legacy DB role name; visible label is below
UTILITY_VISIBLE_LABEL = "Utility Head / Facility Head"
ROLE_LABELS = {
    "Facility Manager": UTILITY_VISIBLE_LABEL,
    "Approver": "Approver / MD",
}


def display_role(role: str | None) -> str:
    return ROLE_LABELS.get(role or "", role or "")


def can_approve(role: str | None) -> bool:
    return role in APPROVAL_ROLES


def can_approve_gateway_pass(role: str | None) -> bool:
    # Gateway pass final approval follows the same final-authority rule as
    # normal approvals: only Admin and Approver / MD can approve.
    # Procurement Manager reviews and submits gateway passes to Approver / MD.
    return role in APPROVAL_ROLES


def can_reject(role: str | None) -> bool:
    return can_approve(role)


def can_pay(role: str | None) -> bool:
    return role in PAYMENT_ROLES


def can_create_payment_request(role: str | None) -> bool:
    # Finance acts after approval; it must not create approval-bound payment requests.
    return role in {"Admin", "Approver"}


def is_read_only(role: str | None) -> bool:
    return role in READ_ONLY_ROLES


def can_delete_draft(role: str | None, owner_id: int | None, actor_id: int | None, status: str | None) -> bool:
    if role == "Admin":
        return True
    if role not in {"Facility Manager", "Procurement Manager"}:
        return False
    if status not in {"Draft", "FM Draft"}:
        return False
    return owner_id is not None and actor_id is not None and int(owner_id) == int(actor_id)


def can_edit_own_draft(role: str | None, owner_id: int | None, actor_id: int | None, status: str | None) -> bool:
    if role == "Admin":
        return True
    if role not in {"Facility Manager", "Procurement Manager"}:
        return False
    if status not in {"Draft", "FM Draft", "Returned for Correction", "Returned to Facility Manager"}:
        return False
    return owner_id is not None and actor_id is not None and int(owner_id) == int(actor_id)


def can_review_procurement(role: str | None) -> bool:
    return role in {"Procurement Manager", "Admin"}


def safe_role_permissions(role: str | None) -> set[str]:
    """Authoritative baseline permissions used by tests and migration cleanup."""
    base = {"change_password", "manage_notification_preferences", "browser_push_setup"}
    if role == "Admin":
        return base | {
            "admin", "create_user", "manage_roles", "create_request", "edit_request", "edit_own_request",
            "submit_request", "submit_to_procurement_manager", "procurement_review", "create_sourcing",
            "manage_quotes", "recommend_vendor", "approve_request", "reject_request", "create_po", "approve_po",
            "receive_goods", "record_expense", "review_invoice", "manage_payments", "manage_vendor",
            "manage_budget", "manage_income", "import_documents", "view_reports", "audit", "read_only_all",
            "approved_for_payment", "return_for_clarification", "create_gateway_pass", "edit_own_gateway_pass",
            "submit_gateway_pass", "review_gateway_pass", "approve_gateway_pass", "generate_gateway_pass",
            "download_gateway_pass", "audit_gateway_pass", "view_all_activity_logs", "view_notifications_monitor",
            "manage_approval_delegation",
        }
    if role == "Approver":
        return base | {"approve_request", "reject_request", "approve_po", "approve_payment", "approve_gateway_pass", "view_reports", "review_gateway_pass"}
    if role == "Procurement Manager":
        return base | {
            "create_request", "edit_request", "edit_own_request", "submit_request", "procurement_review",
            "create_sourcing", "manage_quotes", "recommend_vendor", "create_po", "receive_goods",
            "manage_vendor", "import_documents", "view_reports", "return_for_clarification",
            "communicate_with_procurement_manager", "review_gateway_pass", "submit_for_approval",
        }
    if role == "Facility Manager":
        return base | {
            "create_request", "edit_own_request", "submit_to_procurement_manager", "import_documents_limited",
            "upload_supporting_documents", "view_own_requests", "view_own_activity_history",
            "communicate_with_procurement_manager", "create_gateway_pass", "edit_own_gateway_pass",
            "submit_gateway_pass", "generate_gateway_pass", "download_gateway_pass", "view_reports",
        }
    if role == "Finance":
        return base | {"review_invoice", "manage_payments", "record_expense", "manage_budget", "manage_income", "view_reports", "approved_for_payment", "upload_receipt"}
    if role == "Auditor":
        return base | {"view_reports", "audit", "read_only_all", "audit_gateway_pass", "download_gateway_pass"}
    return base
