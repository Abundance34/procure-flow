"""Central audit-service façade.

Legacy workspace code can continue calling ``core.db.log_audit``. New command
services should call this module so that workflow activity, immutable evidence
and human-facing history are recorded consistently.
"""
from __future__ import annotations

from typing import Any

from core.db import (
    add_workflow_event,
    append_audit_event,
    create_activity_log,
    log_audit,
    verify_audit_chain as _verify_audit_chain,
)


def record_workflow_event(entity_type: str, entity_id: int, event: str, status: str | None, note: str | None, user_id: int | None) -> None:
    add_workflow_event(entity_type, entity_id, event, status, note, user_id)


def record_activity(user_id: int | None, role: str | None, action: str, entity_type: str, entity_id: int | None, summary: str, details: str | None = None, scope: str = "workflow") -> None:
    create_activity_log(user_id, role, action, entity_type, entity_id, summary, details, scope)


def record_audit(
    action: str,
    entity_type: str,
    entity_id: int | str | None,
    details: Any = None,
    user_id: int | None = None,
    role: str | None = None,
    before_values: dict | None = None,
    after_values: dict | None = None,
    outcome: str = "Success",
    severity: str = "Normal",
    correlation_id: str | None = None,
) -> None:
    # Keep legacy audit_logs for existing reports and write canonical evidence.
    log_audit(action, entity_type, entity_id, details, user_id, role, before_values=before_values, after_values=after_values)
    # log_audit already writes to audit_events. This extra direct entry is used
    # only for non-success outcomes that require distinct severity/outcome.
    if outcome != "Success" or severity != "Normal":
        append_audit_event(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details,
            user_id=user_id,
            role=role,
            before_values=before_values,
            after_values=after_values,
            outcome=outcome,
            severity=severity,
            source="service",
            correlation_id=correlation_id,
        )


def verify_audit_chain(record_result: bool = True) -> dict[str, Any]:
    return _verify_audit_chain(record_result=record_result)
