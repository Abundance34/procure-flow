"""Finance command service for post-approval payment actions."""
from __future__ import annotations

from core.permissions import can_pay
from core.db import create_notification, transition_request_status
from core.workflow import request_routing_for_status


def mark_request_paid(request_id: int, actor_user_id: int, actor_role: str, note: str | None = None) -> None:
    if not can_pay(actor_role):
        raise PermissionError("Only Finance/Admin can mark approved requests as paid.")
    routing = request_routing_for_status("Paid")
    transition_request_status(request_id, "Paid", "Payment Completed", note or "Finance marked the request paid.", actor_user_id, actor_role, payment_status=routing.payment_status)
    create_notification(None, "Procurement Manager", "Paid request ready for closure", "A request has been paid and is ready for operational closure.", "Purchase Request", request_id, "High", ["in_app", "browser_push"], action_label="Post-Payment Closure")
