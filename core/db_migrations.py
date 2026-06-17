"""Migration helpers for existing SQLite deployments."""
from core.db import init_db


def run_migrations() -> None:
    init_db()
