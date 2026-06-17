"""Environment configuration helpers."""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("PROCUREFLOW_DATA_DIR", BASE_DIR / "data"))
SMTP_HOST = os.environ.get("PROCUREFLOW_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("PROCUREFLOW_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("PROCUREFLOW_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("PROCUREFLOW_SMTP_PASSWORD", "")
