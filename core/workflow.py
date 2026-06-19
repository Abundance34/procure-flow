"""Authoritative workflow constants and routing helpers for ProcureFlow.

Every UI action and service should ask this module where a record moves next.
Keeping routing here prevents the old problem where Admin, Procurement,
Finance, and Gateway screens each carried their own slightly different map.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.permissions import can_approve, can_pay, can_review_procurement


STATUS_DRAFT = "Draft"
STATUS_SENT_REVIEW = "Sent for Procurement Review"
STATUS_RETURNED = "Returned for Correction"
STATUS_REVIEWED = "Reviewed by Procurement"
STATUS_REQUIRES_SOURCING = "Requires Sourcing"
STATUS_VENDOR_QUOTE_COLLECTION = "Vendor Quote Collection"
STATUS_VENDOR_RECOMMENDATION = "Vendor Recommendation"
STATUS_SUBMITTED_APPROVAL = "Submitted for Approval"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_AWAITING_PAYMENT = "Awaiting Payment"
STATUS_PAID = "Paid"
STATUS_RECEIPT_UPLOADED = "Receipt Uploaded"
STATUS_PAYMENT_VERIFICATION = "Payment Submitted for Verification"
STATUS_COMPLETED = "Completed"
STATUS_CLOSED = "Closed"
STATUS_ARCHIVED = "Archived"

STATUSES = [
    STATUS_DRAFT,
    STATUS_SENT_REVIEW,
    STATUS_RETURNED,
    STATUS_REVIEWED,
    STATUS_REQUIRES_SOURCING,
    STATUS_VENDOR_QUOTE_COLLECTION,
    STATUS_VENDOR_RECOMMENDATION,
    STATUS_SUBMITTED_APPROVAL,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_AWAITING_PAYMENT,
    STATUS_PAID,
    STATUS_RECEIPT_UPLOADED,
    STATUS_PAYMENT_VERIFICATION,
    STATUS_COMPLETED,
    STATUS_CLOSED,
    STATUS_ARCHIVED,
]

# Legacy/status aliases are normalized before routing.  The database can keep
# old labels, but the command chain resolves them to the modern vocabulary.
LEGACY_STATUS_ALIASES = {
    "FM Draft": STATUS_DRAFT,
    "Submitted": STATUS_SENT_REVIEW,
    "Submitted to Procurement Manager": STATUS_SENT_REVIEW,
    "Procurement Review": STATUS_SENT_REVIEW,
    "PM Reviewing": STATUS_REVIEWED,
    "Accepted by Procurement Manager": STATUS_REVIEWED,
    # Sourcing statuses are not aliases; they are actionable procurement queues.
    "Returned": STATUS_RETURNED,
    "Returned to Facility Manager": STATUS_RETURNED,
    "Pending Approver/MD Approval": STATUS_SUBMITTED_APPROVAL,
    "Pending Approval": STATUS_SUBMITTED_APPROVAL,
    "Approved for Payment": STATUS_AWAITING_PAYMENT,
    "Finance Review": STATUS_AWAITING_PAYMENT,
    "Payment Approved": STATUS_AWAITING_PAYMENT,
    "Closed": STATUS_CLOSED,
    "Generated": STATUS_COMPLETED,
    "Downloaded": STATUS_COMPLETED,
}

# Purchase request routing.  Procurement owns operational closure after Finance
# records payment/receipt.  Auditor receives the record once it is Closed or
# Archived for compliance/history review.
REQUEST_NEXT_ROLE_BY_STATUS = {
    STATUS_SENT_REVIEW: "procurement_manager",
    STATUS_REVIEWED: "procurement_manager",
    STATUS_REQUIRES_SOURCING: "procurement_manager",
    STATUS_VENDOR_QUOTE_COLLECTION: "procurement_manager",
    STATUS_VENDOR_RECOMMENDATION: "procurement_manager",
    STATUS_SUBMITTED_APPROVAL: "approver",
    STATUS_APPROVED: "finance",
    STATUS_AWAITING_PAYMENT: "finance",
    STATUS_PAID: "procurement_manager",
    STATUS_RECEIPT_UPLOADED: "procurement_manager",
    STATUS_PAYMENT_VERIFICATION: "procurement_manager",
    STATUS_COMPLETED: "procurement_manager",
    STATUS_CLOSED: "auditor",
    STATUS_ARCHIVED: "auditor",
}

# Gateway pass routing.  Procurement Manager reviews; Approver/Admin gives the
# final approval; Utility/Facility user generates/downloads after approval.
GATEWAY_NEXT_ROLE_BY_STATUS = {
    STATUS_DRAFT: "facility_manager",
    STATUS_SENT_REVIEW: "procurement_manager",
    STATUS_REVIEWED: "procurement_manager",
    STATUS_REQUIRES_SOURCING: "procurement_manager",
    STATUS_VENDOR_QUOTE_COLLECTION: "procurement_manager",
    STATUS_VENDOR_RECOMMENDATION: "procurement_manager",
    STATUS_SUBMITTED_APPROVAL: "approver",
    STATUS_APPROVED: "facility_manager",
    STATUS_RETURNED: "facility_manager",
    STATUS_REJECTED: None,
    "Generated": None,
    "Downloaded": None,
    STATUS_COMPLETED: None,
    STATUS_ARCHIVED: "auditor",
}


@dataclass(frozen=True)
class WorkflowRouting:
    """Small value object used by services/UI for safe status updates."""

    canonical_status: str
    next_role: Optional[str]
    payment_status: Optional[str] = None


def normalize_status(status: str | None) -> str:
    return LEGACY_STATUS_ALIASES.get(status or "", status or "")


def next_role_for_status(status: str | None) -> Optional[str]:
    return REQUEST_NEXT_ROLE_BY_STATUS.get(normalize_status(status))


def gateway_next_role_for_status(status: str | None) -> Optional[str]:
    return GATEWAY_NEXT_ROLE_BY_STATUS.get(normalize_status(status))


def payment_status_for_request_status(status: str | None) -> Optional[str]:
    canonical = normalize_status(status)
    if canonical in {STATUS_APPROVED, STATUS_AWAITING_PAYMENT}:
        return "Approved for Payment"
    if canonical in {STATUS_PAID, STATUS_RECEIPT_UPLOADED, STATUS_PAYMENT_VERIFICATION, STATUS_COMPLETED, STATUS_CLOSED, STATUS_ARCHIVED}:
        return "Paid"
    return None


def request_routing_for_status(status: str | None) -> WorkflowRouting:
    canonical = normalize_status(status)
    return WorkflowRouting(
        canonical_status=canonical,
        next_role=next_role_for_status(canonical),
        payment_status=payment_status_for_request_status(canonical),
    )


def gateway_routing_for_status(status: str | None) -> WorkflowRouting:
    canonical = normalize_status(status)
    return WorkflowRouting(
        canonical_status=canonical,
        next_role=gateway_next_role_for_status(canonical),
        payment_status=None,
    )


def payment_status_after_approval(payment_required: bool = True) -> str:
    return STATUS_AWAITING_PAYMENT if payment_required else STATUS_COMPLETED


def assert_can_approve(role: str | None) -> None:
    if not can_approve(role):
        raise PermissionError("Only Admin and Approver / MD can approve or reject workflow items.")


def assert_can_pay(role: str | None) -> None:
    if not can_pay(role):
        raise PermissionError("Only Finance/Admin can perform payment actions after approval.")


def assert_can_review_procurement(role: str | None) -> None:
    if not can_review_procurement(role):
        raise PermissionError("Only Procurement Manager/Admin can perform procurement review actions.")


# Backwards-compatible names used by tests/services.
def canonical_status(status: str | None) -> str:
    return normalize_status(status)


def workflow_next_role(status: str | None) -> Optional[str]:
    return next_role_for_status(status)
