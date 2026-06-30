"""Repository helpers for append-only ProcureFlow audit evidence."""
from __future__ import annotations

from typing import Any, Iterable

from core.db import df_query, run_query


def ledger_page(where_sql: str = "", params: Iterable[Any] = (), limit: int = 100, offset: int = 0):
    sql = "SELECT * FROM audit_events"
    if where_sql:
        sql += " WHERE " + where_sql
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    return df_query(sql, tuple(params) + (int(limit), int(offset)))


def ledger_count(where_sql: str = "", params: Iterable[Any] = ()) -> int:
    sql = "SELECT COUNT(*) AS c FROM audit_events"
    if where_sql:
        sql += " WHERE " + where_sql
    rows = run_query(sql, tuple(params), fetch=True)
    return int(rows[0]["c"] if rows else 0)


def event_by_id(event_id: int):
    rows = run_query("SELECT * FROM audit_events WHERE id=?", (int(event_id),), fetch=True)
    return dict(rows[0]) if rows else None
