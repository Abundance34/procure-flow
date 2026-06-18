"""Notification and section-attention helpers.

Opening a sidebar section should clear only the red attention badge, never the
underlying activity log or notification history.  Explicit "mark all as read"
changes notification read state but still leaves audit/activity records intact.
"""
from __future__ import annotations

from core.db import now_iso, run_query


def mark_section_seen(user_id: int, role: str, section: str) -> None:
    ts = now_iso()
    run_query(
        """
        INSERT INTO section_attention_reads (user_id, role, section, last_seen_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, role, section)
        DO UPDATE SET last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at
        """,
        (user_id, role, section, ts, ts, ts),
    )


def mark_notifications_read(user_id: int, role: str) -> None:
    run_query("UPDATE notifications SET is_read=1 WHERE is_read=0 AND (user_id=? OR role=? OR role='All')", (user_id, role))
