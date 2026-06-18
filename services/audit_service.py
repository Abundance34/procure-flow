"""Audit service wrappers used by command services and future UI refactors."""
from __future__ import annotations

from core.db import add_workflow_event, create_activity_log, log_audit


def record_workflow_event(entity_type: str, entity_id: int, event: str, status: str | None, note: str | None, user_id: int | None) -> None:
    add_workflow_event(entity_type, entity_id, event, status, note, user_id)


def record_activity(user_id: int | None, role: str | None, action: str, entity_type: str, entity_id: int | None, summary: str, details: str | None = None, scope: str = "workflow") -> None:
    create_activity_log(user_id, role, action, entity_type, entity_id, summary, details, scope)


def record_audit(action: str, entity_type: str, entity_id: int | str | None, details=None, user_id: int | None = None, role: str | None = None, before_values=None, after_values=None) -> None:
    log_audit(action, entity_type, entity_id, details, user_id, role, before_values=before_values, after_values=after_values)
