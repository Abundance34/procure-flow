"""Authoritative workflow constants and routing helpers for ProcureFlow.

Every UI action and service should ask this module where a record moves next.
Keeping routing here prevents the old problem where Admin, Procurement,
Finance, and Gateway screens each carried their own slightly different map.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.permissions import can_approve, can_pay, can_review_procurement


# Monetary approval authority. Procurement Manager has delegated-by-policy
# authority for low-value transactions only; higher values remain with the
# Approver / MD. The value is deliberately central so request, PO and payment
# screens cannot drift into different threshold rules.
PROCUREMENT_MANAGER_APPROVAL_THRESHOLD = 100_000.0
LOW_VALUE_APPROVAL_MODE = "Low-Value Approval — Procurement Manager (≤ ₦100,000)"


def approval_amount(value: object | None) -> float:
    """Return a safe monetary amount for threshold routing.

    UI values and legacy SQLite rows may be ``None`` or text; invalid values
    become zero rather than breaking a workflow screen.
    """
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def is_low_value_approval(value: object | None) -> bool:
    """True when a transaction is within the PM approval limit.

    The policy treats exactly ₦100,000 as low value so there is no un-routed
    gap at the boundary.
    """
    return approval_amount(value) <= PROCUREMENT_MANAGER_APPROVAL_THRESHOLD


def required_approval_role_for_amount(value: object | None) -> str:
    """Return the role queue that owns an approval for this amount.

    Uses the database role key rather than a display label.
    """
    return "procurement_manager" if is_low_value_approval(value) else "approver"


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


def request_routing_for_status(status: str | None, amount: object | None = None) -> WorkflowRouting:
    """Return the canonical request routing for a status and optional amount.

    Purchase requests sent for final approval are value-routed centrally: the
    Procurement Manager owns amounts up to and including ₦100,000, while the
    Approver / MD owns higher values.  Callers that do not provide an amount
    retain the legacy/default final-approval route for backward compatibility.
    """
    canonical = normalize_status(status)
    next_role = next_role_for_status(canonical)
    if canonical == STATUS_SUBMITTED_APPROVAL and amount is not None:
        next_role = required_approval_role_for_amount(amount)
    return WorkflowRouting(
        canonical_status=canonical,
        next_role=next_role,
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


# Purchase-order fulfilment routing. Procurement owns commercial PO work up to
# release; Logistics owns delivery coordination and receiving afterwards.
PO_STATUS_DRAFT = "Draft"
PO_STATUS_PENDING_APPROVAL = "Pending Approval"
PO_STATUS_APPROVED = "Approved"
PO_STATUS_RELEASED_TO_LOGISTICS = "Released to Logistics"
PO_STATUS_SCHEDULED = "Scheduled"
PO_STATUS_DISPATCHED = "Dispatched"
PO_STATUS_IN_TRANSIT = "In Transit"
PO_STATUS_DELAYED = "Delayed"
PO_STATUS_ARRIVED = "Arrived"
PO_STATUS_PARTIALLY_RECEIVED = "Partially Received"
PO_STATUS_FULLY_RECEIVED = "Fully Received"
PO_STATUS_DISPUTED = "Disputed"
PO_STATUS_RETURNED = "Returned"

PO_NEXT_ROLE_BY_STATUS = {
    PO_STATUS_DRAFT: "procurement_manager",
    PO_STATUS_PENDING_APPROVAL: "approver",
    PO_STATUS_APPROVED: "procurement_manager",
    PO_STATUS_RELEASED_TO_LOGISTICS: "logistics_officer",
    PO_STATUS_SCHEDULED: "logistics_officer",
    PO_STATUS_DISPATCHED: "logistics_officer",
    PO_STATUS_IN_TRANSIT: "logistics_officer",
    PO_STATUS_DELAYED: "logistics_officer",
    PO_STATUS_ARRIVED: "logistics_officer",
    PO_STATUS_PARTIALLY_RECEIVED: "logistics_officer",
    PO_STATUS_FULLY_RECEIVED: "finance",
    PO_STATUS_DISPUTED: "procurement_manager",
    PO_STATUS_RETURNED: "procurement_manager",
}

def po_next_role_for_status(status: str | None) -> Optional[str]:
    return PO_NEXT_ROLE_BY_STATUS.get(status or "")
