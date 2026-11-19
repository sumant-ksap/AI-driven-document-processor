"""Keep the uploaded documents (photos/PDFs) for each scan, for audits and disputes.

Files go to data/scans/<scan_id>/ next to the database. BOLs and driver licences hold personal
data, so files older than DOCUMENT_RETENTION_DAYS (default 90) are deleted automatically.
Set STORE_DOCUMENTS=false to keep only the extracted data.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

from . import db

log = logging.getLogger(__name__)


def enabled() -> bool:
    return os.environ.get("STORE_DOCUMENTS", "true").lower() not in ("0", "false", "no")


def retention_days() -> int:
    return int(os.environ.get("DOCUMENT_RETENTION_DAYS", "90"))


def docs_dir() -> Path:
    # Resolved per call so tests that point db.DB_PATH elsewhere also move the documents.
    return Path(os.environ.get("SCAN_DOCS_DIR") or Path(db.DB_PATH).parent / "scans")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "document").name)[:100] or "document"


def save(scan_id: str, payloads: list[tuple[bytes, str, str]]) -> list[dict]:
    """payloads: (data, media_type, filename). Returns per-file metadata for the scans table."""
    files = []
    folder = docs_dir() / scan_id
    for i, (data, media_type, filename) in enumerate(payloads):
        stored = None
        if enabled():
            folder.mkdir(parents=True, exist_ok=True)
            stored = f"{i}_{_safe_name(filename)}"
            (folder / stored).write_bytes(data)
        files.append({"index": i, "filename": filename, "media_type": media_type, "size": len(data), "stored": stored})
    return files


def path(scan_id: str, stored: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{12}", scan_id) or "/" in stored or "\\" in stored:
        return None
    p = docs_dir() / scan_id / stored
    return p if p.is_file() else None


def cleanup() -> int:
    """Delete document folders older than the retention period. Returns how many were removed."""
    root = docs_dir()
    if not root.is_dir():
        return 0
    cutoff = time.time() - retention_days() * 86400
    removed = 0
    for folder in root.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    if removed:
        log.info("Deleted documents of %d scans older than %d days", removed, retention_days())
    return removed
