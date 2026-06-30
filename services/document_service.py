"""Secure document-upload helpers used by existing ProcureFlow screens."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

from core.db import ATTACHMENT_DIR

MAX_UPLOAD_MB = int(os.environ.get("PROCUREFLOW_MAX_UPLOAD_MB", "15"))
ENABLE_AV = os.environ.get("PROCUREFLOW_ENABLE_AV_SCAN", "0") == "1"
MAX_ZIP_MEMBERS = int(os.environ.get("PROCUREFLOW_MAX_ZIP_MEMBERS", "2000"))
MAX_ZIP_UNCOMPRESSED_MB = int(os.environ.get("PROCUREFLOW_MAX_ZIP_UNCOMPRESSED_MB", "100"))

ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".jpg", ".jpeg", ".png", ".zip"}

class DocumentSecurityError(ValueError):
    pass


def _safe_filename(name: str) -> str:
    base = Path(name or "upload").name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return base or "upload"


def _looks_like_allowed_content(data: bytes, suffix: str) -> bool:
    suffix = suffix.lower()
    if suffix == ".pdf": return data.startswith(b"%PDF-")
    if suffix in {".jpg", ".jpeg"}: return data.startswith(b"\xff\xd8\xff")
    if suffix == ".png": return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".docx", ".xlsx", ".zip"}:
        return data.startswith(b"PK\x03\x04") or data.startswith(b"PK\x05\x06")
    return False


def _validate_zip(data: bytes) -> None:
    try:
        from io import BytesIO
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                raise DocumentSecurityError("The uploaded archive contains too many files.")
            total = 0
            for info in infos:
                target = Path(info.filename)
                if target.is_absolute() or ".." in target.parts:
                    raise DocumentSecurityError("The uploaded archive contains an unsafe file path.")
                total += max(0, int(info.file_size or 0))
                if total > MAX_ZIP_UNCOMPRESSED_MB * 1024 * 1024:
                    raise DocumentSecurityError("The uploaded archive expands beyond the allowed safe size.")
    except zipfile.BadZipFile as exc:
        raise DocumentSecurityError("The uploaded Office/archive file is invalid.") from exc


def _scan_if_enabled(path: Path) -> None:
    if not ENABLE_AV:
        return
    scanner = shutil.which("clamscan") or shutil.which("clamdscan")
    if not scanner:
        raise DocumentSecurityError("Antivirus scanning is enabled but no scanner is configured.")
    result = subprocess.run([scanner, "--no-summary", str(path)], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        raise DocumentSecurityError("The uploaded file did not pass malware scanning.")


def secure_save_upload(uploaded_file: Any, subfolder: str) -> tuple[str | None, str | None]:
    if not uploaded_file:
        return None, None
    name = _safe_filename(getattr(uploaded_file, "name", "upload"))
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise DocumentSecurityError("This file type is not permitted.")
    data = uploaded_file.getvalue()
    if not data:
        raise DocumentSecurityError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise DocumentSecurityError(f"The file exceeds the {MAX_UPLOAD_MB} MB upload limit.")
    if not _looks_like_allowed_content(data, suffix):
        raise DocumentSecurityError("The file content does not match its permitted file type.")
    if suffix in {".docx", ".xlsx", ".zip"}:
        _validate_zip(data)
    file_hash = hashlib.sha256(data).hexdigest()
    folder = ATTACHMENT_DIR / re.sub(r"[^A-Za-z0-9_-]+", "_", subfolder or "uploads")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_hash[:16]}_{name}"
    if not path.exists():
        path.write_bytes(data)
        try: os.chmod(path, 0o600)
        except Exception: pass
        _scan_if_enabled(path)
    return str(path), file_hash
