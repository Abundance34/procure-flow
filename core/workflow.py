"""Workflow constants and validation helpers for the command chain."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.permissions import can_approve, can_pay, can_review_procurement


STATUS_DRAFT = "Draft"
STATUS_SENT_REVIEW = "Sent for Procurement Review"
STATUS_RETURNED = "Returned for Correction"
STATUS_REVIEWED = "Reviewed by Procurement"
STATUS_SUBMITTED_APPROVAL = "Submitted for Approval"
STATUS_APPROVED = "Approved"
STATUS_REJECTED = "Rejected"
STATUS_AWAITING_PAYMENT = "Awaiting Payment"
STATUS_PAID = "Paid"
STATUS_RECEIPT_UPLOADED = "Receipt Uploaded"
STATUS_PAYMENT_VERIFICATION = "Payment Submitted for Verification"
STATUS_COMPLETED = "Completed"
STATUS_ARCHIVED = "Archived"

STATUSES = [
    "Draft", "Sent for Procurement Review", "Returned for Correction", "Reviewed by Procurement",
    "Submitted for Approval", "Approved", "Rejected", "Awaiting Payment", "Paid", "Receipt Uploaded",
    "Payment Submitted for Verification", "Completed", "Archived",
]
NEXT_ROLE_BY_STATUS = {
    "Sent for Procurement Review": "procurement_manager",
    "Reviewed by Procurement": "procurement_manager",
    "Submitted for Approval": "approver",
    "Approved": "finance",
    "Awaiting Payment": "finance",
    "Paid": "finance",
    "Receipt Uploaded": "auditor",
    "Completed": "auditor",
}

LEGACY_STATUS_ALIASES = {
    "FM Draft": "Draft",
    "Submitted": "Sent for Procurement Review",
    "Submitted to Procurement Manager": "Sent for Procurement Review",
    "PM Reviewing": "Reviewed by Procurement",
    "Returned to Facility Manager": "Returned for Correction",
    "Accepted by Procurement Manager": "Reviewed by Procurement",
    "Pending Approver/MD Approval": "Submitted for Approval",
    "Pending Approval": "Submitted for Approval",
    "Approved for Payment": "Awaiting Payment",
    "Payment Approved": "Awaiting Payment",
    "Closed": "Completed",
    "Generated": "Completed",
    "Downloaded": "Completed",
}


def normalize_status(status: str | None) -> str:
    return LEGACY_STATUS_ALIASES.get(status or "", status or "")


def next_role_for_status(status: str | None) -> Optional[str]:
    return NEXT_ROLE_BY_STATUS.get(normalize_status(status))


def payment_status_after_approval(payment_required: bool = True) -> str:
    return "Awaiting Payment" if payment_required else "Completed"


def assert_can_approve(role: str | None) -> None:
    if not can_approve(role):
        raise PermissionError("Only Admin and Approver / MD can approve or reject workflow items.")


def assert_can_pay(role: str | None) -> None:
    if not can_pay(role):
        raise PermissionError("Only Finance/Admin can perform payment actions after approval.")


# Backwards-compatible names used by tests/services.
def canonical_status(status: str | None) -> str:
    return normalize_status(status)


def workflow_next_role(status: str | None) -> Optional[str]:
    return next_role_for_status(status)
