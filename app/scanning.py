"""Scan -> review -> check-in logic shared by the review screen's /api routes and /api/v1.

Scans are saved in the database (not server memory), so a scan can be fetched by ID and
survives a server restart. The model reads one document at a time on a CPU, so all scans go
through a single-worker queue.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException, UploadFile

from . import db, documents
from .extractor import ExtractionError, get_extractor
from .pipeline import (find_duplicates, load_config, map_to_yms, merge_line_items, quality_flags,
                       validate, validate_line_items)
from .schemas import LINE_ITEM_COLUMNS, ScanResult, Severity, YmsField, YmsLineItem

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 6

# One worker: concurrent scans on a CPU only slow each other down. Jobs queue in order.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan")


def _media_type(upload: UploadFile) -> str:
    mt = upload.content_type or ""
    if mt in ("", "application/octet-stream"):
        mt = mimetypes.guess_type(upload.filename or "")[0] or ""
    return mt


async def read_uploads(files: list[UploadFile]) -> list[tuple[bytes, str, str]]:
    """Validate and read uploads -> [(data, media_type, filename)]."""
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"Upload 1-{MAX_FILES} documents")
    payloads = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than 20 MB")
        mt = _media_type(f)
        if not (mt.startswith("image/") or mt == "application/pdf"):
            raise HTTPException(415, f"{f.filename}: only images and PDFs are supported")
        payloads.append((data, mt, f.filename or "document"))
    return payloads


def build_result(scan_id, files, extractor_name, extractions, local_issues) -> ScanResult:
    mappings, rules = load_config("field_mappings.json"), load_config("validation_rules.json")
    fields, flags = map_to_yms(extractions, mappings, rules)
    line_items = merge_line_items(extractions, rules)
    flags += validate_line_items(line_items, fields, rules)  # may fill totals; run before format checks
    with db.connect() as conn:
        flags += validate(fields, conn, rules)
        dupes, dupe_flags = find_duplicates(fields, conn, rules)
    flags += dupe_flags + quality_flags(extractions, local_issues, files)
    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    flags.sort(key=lambda f: order[f.severity])
    # Small local models report high confidence even on blurry input, so a poor image alone
    # means the clerk must look at the fields before the scan counts as ready.
    ready = (not any(f.severity == Severity.ERROR or f.code == "image_quality" for f in flags)
             and not any(f.needs_review for f in fields)
             and not any(li.needs_review for li in line_items))
    return ScanResult(scan_id=scan_id, files=files, extractor=extractor_name,
                      document_types=[e.document_type.value for e in extractions],
                      fields=fields, line_items=line_items, flags=flags, possible_duplicates=dupes,
                      ready_for_check_in=ready)


def start_scan(payloads: list[tuple[bytes, str, str]]) -> str:
    """Save the documents and register the scan as 'processing'. Returns the scan ID."""
    scan_id = uuid.uuid4().hex[:12]
    files = documents.save(scan_id, payloads)
    with db.connect() as conn:
        db.create_scan(conn, scan_id, files)
    return scan_id


def run_scan(scan_id: str, payloads: list[tuple[bytes, str, str]]) -> ScanResult | None:
    """Read the documents and store the result (or the error) on the scan. Runs on the scan worker."""
    extractor = get_extractor()
    try:
        results = [extractor.extract(*p) for p in payloads]
        result = build_result(scan_id, [p[2] for p in payloads], extractor.name,
                              [r[0] for r in results], [r[1] for r in results])
    except ExtractionError as e:
        with db.connect() as conn:
            db.finish_scan(conn, scan_id, error=str(e))
        return None
    except Exception as e:  # keep the job record honest instead of leaving it 'processing'
        log.exception("Scan %s failed", scan_id)
        with db.connect() as conn:
            db.finish_scan(conn, scan_id, error=f"Unexpected error: {type(e).__name__}")
        return None
    with db.connect() as conn:
        db.finish_scan(conn, scan_id, result_json=result.model_dump_json())
    return result


def load_ready_scan(scan_id: str) -> ScanResult:
    """The scan's result, if it can still be checked in; otherwise a clear HTTP error."""
    with db.connect() as conn:
        scan = db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "Scan not found")
    if scan["status"] == "processing":
        raise HTTPException(409, "Scan is still being read; try again when its status is 'ready'")
    if scan["status"] == "failed":
        raise HTTPException(409, f"Scan failed: {scan['error']}")
    if scan["status"] == "checked_in":
        raise HTTPException(409, f"Scan already checked in as visit {scan['visit_id']}")
    return ScanResult.model_validate(scan["result"])


def clean_line_items(rows: list[dict]) -> list[dict]:
    return [{c: (row.get(c) or None) for c in LINE_ITEM_COLUMNS} for row in rows if any(row.values())]


def validate_values(fields_in: dict[str, str | None], line_items_in: list[dict],
                    labels: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """Validate reviewed values (treated as certain). Returns (flags, possible_duplicates)."""
    rules = load_config("validation_rules.json")
    labels = labels or {f["key"]: f["label"] for f in load_config("field_mappings.json")["fields"]}
    fields = [YmsField(key=k, label=labels.get(k, k), value=(v or None), confidence=1.0) for k, v in fields_in.items()]
    items = [YmsLineItem(values=row, min_confidence=1.0) for row in clean_line_items(line_items_in)]
    flags = validate_line_items(items, fields, rules)
    with db.connect() as conn:
        flags += validate(fields, conn, rules)
        dupes, dupe_flags = find_duplicates(fields, conn, rules)
    flags += dupe_flags
    flags = [f for f in flags if f.code != "total_from_lines"]  # reviewed values are final
    return [f.model_dump(mode="json") for f in flags], dupes


def check_in(scan_id: str, fields: dict, line_items: list[dict], reviewed_by: str,
             override_reason: str | None) -> dict:
    if not (reviewed_by or "").strip():
        raise HTTPException(400, "reviewed_by is required")
    scan = load_ready_scan(scan_id)
    flags, _ = validate_values(fields, line_items, {f.key: f.label for f in scan.fields})
    errors = [f for f in flags if f["severity"] == Severity.ERROR.value]
    if errors and not (override_reason or "").strip():
        raise HTTPException(409, {"message": "Resolve errors or provide an override reason", "flags": errors})

    # Record what the reviewer changed: the data needed to measure extraction accuracy over time.
    original = {f.key: f.value for f in scan.fields}
    corrections = {k: {"extracted": original.get(k), "final": v}
                   for k, v in fields.items() if (original.get(k) or None) != (v or None)}
    original_items = [li.values for li in scan.line_items]
    final_items = clean_line_items(line_items)
    if original_items != final_items:
        corrections["_line_items"] = {"extracted": original_items, "final": final_items}
    if override_reason:
        corrections["_override"] = {"reason": override_reason, "errors": [e["message"] for e in errors]}

    with db.connect() as conn:
        visit_id = db.create_visit(conn, fields, reviewed_by, corrections, final_items, scan_id=scan_id)
        db.mark_scan_checked_in(conn, scan_id, visit_id)
    return {"visit_id": visit_id, "corrections": len([k for k in corrections if not k.startswith("_")])}
