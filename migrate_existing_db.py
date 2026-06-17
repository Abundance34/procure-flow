"""Apply ProcureFlow command-chain migrations to an existing SQLite database.

Usage:
    python migrate_existing_db.py

The script is non-destructive. It creates missing tables/columns/indexes, rebuilds
role permissions so only Admin and Approver can approve, and backfills workflow
routing fields such as next_role.
"""
from __future__ import annotations

from core.db import init_db, ensure_command_chain_schema, DB_PATH


if __name__ == "__main__":
    init_db()
    ensure_command_chain_schema()
    print(f"Migration complete: {DB_PATH}")
