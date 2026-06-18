"""Gateway pass command service.

Gateway passes follow a separate but centralized chain:
Utility/Facility -> Procurement Manager review -> Approver/Admin final decision
-> Utility/Facility generation/download -> audit/history.
"""
from __future__ import annotations

from core.permissions import can_approve, can_review_procurement
from core.db import create_notification, run_query, transition_gateway_pass_status, now_iso


def submit_for_procurement_review(gateway_pass_id: int, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    transition_gateway_pass_status(gateway_pass_id, "Sent for Procurement Review", "Sent for Procurement Review", note or "Submitted to Procurement Manager", actor_user_id, actor_role)
    create_notification(None, "Procurement Manager", "Gateway pass sent for review", "A gateway pass requires Procurement Manager review before final approval.", "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")


def mark_reviewed(gateway_pass_id: int, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    if not can_review_procurement(actor_role):
        raise PermissionError("Only Procurement Manager/Admin can review gateway passes.")
    transition_gateway_pass_status(gateway_pass_id, "Reviewed by Procurement", "Reviewed by Procurement", note or "Reviewed by Procurement Manager", actor_user_id, actor_role)


def submit_to_approver(gateway_pass_id: int, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    if not can_review_procurement(actor_role):
        raise PermissionError("Only Procurement Manager/Admin can submit gateway passes for final approval.")
    transition_gateway_pass_status(gateway_pass_id, "Submitted for Approval", "Submitted for Approval", note or "Submitted for final approval", actor_user_id, actor_role)
    create_notification(None, "Approver", "Gateway pass pending final approval", "A gateway pass requires final approval from Approver / MD.", "Gateway Pass", gateway_pass_id, "High", ["in_app", "browser_push"], action_label="Review Gateway Pass")
    create_notification(None, "Admin", "Gateway pass pending final approval", "A gateway pass requires approval/oversight.", "Gateway Pass", gateway_pass_id, "Important", ["in_app"], action_label="Gateway Pass Management")


def final_decision(gateway_pass_id: int, decision: str, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    if actor_role == "Procurement Manager" or not can_approve(actor_role):
        raise PermissionError("Only Admin and Approver / MD can approve, return, or reject gateway passes.")
    if decision not in {"Approved", "Rejected", "Returned for Correction"}:
        raise ValueError("Decision must be Approved, Rejected, or Returned for Correction.")
    if decision in {"Rejected", "Returned for Correction"} and not (note or "").strip():
        raise ValueError("A reason is required when rejecting or returning a gateway pass.")
    transition_gateway_pass_status(gateway_pass_id, decision, f"Gateway Pass {decision}", note, actor_user_id, actor_role)
    run_query(
        "INSERT INTO gateway_pass_approvals (gateway_pass_id, approver_user_id, approver_role, decision, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (gateway_pass_id, actor_user_id, actor_role, decision, note, now_iso()),
    )
