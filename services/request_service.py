"""Purchase request command service.

UI pages should call these command functions instead of issuing ad-hoc UPDATEs.
The service validates role authority, delegates routing to core.workflow, and
persists through core.db.transition_request_status so notifications, audit logs,
activity history, and next_role stay aligned.
"""
from __future__ import annotations

from core.permissions import can_approve, can_pay, can_review_procurement
from core.workflow import request_routing_for_status
from core.db import create_notification, transition_request_status


PROCUREMENT_REVIEW_STATUSES = {
    "Sent for Procurement Review",
    "Reviewed by Procurement",
    "Requires Sourcing",
    "Vendor Quote Collection",
    "Vendor Recommendation",
}
FINAL_DECISION_STATUSES = {"Approved", "Rejected", "Returned for Correction"}
FINANCE_STATUSES = {"Paid", "Receipt Uploaded", "Payment Submitted for Verification"}


def move_request(
    request_id: int,
    new_status: str,
    event: str,
    actor_user_id: int | None,
    actor_role: str | None,
    note: str | None = None,
) -> None:
    """Safely move a purchase request to a new workflow state."""
    routing = request_routing_for_status(new_status)
    canonical = routing.canonical_status

    if canonical in FINAL_DECISION_STATUSES and canonical in {"Approved", "Rejected"} and not can_approve(actor_role):
        raise PermissionError("Only Admin and Approver / MD can approve or reject purchase requests.")
    if canonical in PROCUREMENT_REVIEW_STATUSES and not can_review_procurement(actor_role):
        raise PermissionError("Only Procurement Manager/Admin can perform procurement review actions.")
    if canonical in FINANCE_STATUSES and not can_pay(actor_role):
        raise PermissionError("Only Finance/Admin can perform payment/receipt actions.")

    transition_request_status(
        request_id,
        canonical,
        event,
        note,
        actor_user_id,
        actor_role,
        payment_status=routing.payment_status,
    )


def submit_to_procurement(request_id: int, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    move_request(request_id, "Sent for Procurement Review", "Submitted to Procurement Manager", actor_user_id, actor_role, note)


def submit_to_approval(request_id: int, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    if not can_review_procurement(actor_role):
        raise PermissionError("Only Procurement Manager/Admin can submit reviewed requests for final approval.")
    move_request(request_id, "Submitted for Approval", "Submitted for Approval", actor_user_id, actor_role, note)
    create_notification(None, "Approver", "Request pending approval", "A reviewed request requires final approval.", "Purchase Request", request_id, "High", ["in_app", "browser_push"], action_label="Open Pending Approvals")


def final_decision(request_id: int, decision: str, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    if decision not in {"Approved", "Rejected", "Returned for Correction"}:
        raise ValueError("Decision must be Approved, Rejected, or Returned for Correction.")
    if decision in {"Approved", "Rejected"} and not can_approve(actor_role):
        raise PermissionError("Only Admin and Approver / MD can approve or reject purchase requests.")
    move_request(request_id, decision, f"Request {decision}", actor_user_id, actor_role, note)
    if decision == "Approved":
        create_notification(None, "Finance", "Approved item ready for Finance", "A request has been approved and is ready for payment.", "Purchase Request", request_id, "Important", ["in_app", "browser_push"], action_label="Open Approved for Payment")
