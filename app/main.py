"""FastAPI app: scan documents -> review -> confirm check-in."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, db
from .extractor import ExtractionError, get_extractor
from .pipeline import (find_duplicates, load_config, map_to_yms, merge_line_items, quality_flags,
                       validate, validate_line_items)
from .schemas import LINE_ITEM_COLUMNS, ScanResult, Severity, YmsField, YmsLineItem

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).resolve().parent / "static"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 6

def _warm_up_model():
    """Load the vision model into memory now, so the first truck doesn't wait for it."""
    import ollama
    from .extractor import OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_NUM_CTX
    try:
        # Same num_ctx as scans: a different context size makes Ollama reload the model on the first scan.
        ollama.Client(host=OLLAMA_HOST, timeout=120).generate(
            model=OLLAMA_MODEL, prompt="", keep_alive="30m", options={"num_ctx": OLLAMA_NUM_CTX})
        logging.info("Model %s loaded", OLLAMA_MODEL)
    except Exception as e:  # Ollama down or model missing: scans will report it clearly later
        logging.warning("Could not preload %s: %s", OLLAMA_MODEL, e)


@asynccontextmanager
async def lifespan(_app):
    with db.connect() as conn:
        db.seed_demo_data(conn)
    if os.environ.get("EXTRACTOR_MODE", "ollama").lower() != "mock":
        threading.Thread(target=_warm_up_model, daemon=True).start()
    yield


app = FastAPI(title="Gate Document Scanner", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# PIN login, active only when GATE_PIN is set (network mode: scripts/serve_network.py).
app.middleware("http")(auth.require_pin)
app.get("/login", include_in_schema=False)(lambda: auth.login_page())
app.post("/login", include_in_schema=False)(auth.login_submit)
app.get("/logout", include_in_schema=False)(auth.logout)

# Pending scans awaiting clerk review. In production keep these in the YMS DB/Redis.
_pending: dict[str, ScanResult] = {}


@app.get("/")
def index():
    # no-store: after an update the browser must not keep running the old page script.
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def _media_type(upload: UploadFile) -> str:
    mt = upload.content_type or ""
    if mt in ("", "application/octet-stream"):
        mt = mimetypes.guess_type(upload.filename or "")[0] or ""
    return mt


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
                      fields=fields, line_items=line_items, flags=flags, possible_duplicates=dupes, ready_for_check_in=ready)


@app.post("/api/scan", response_model=ScanResult)
async def scan(files: list[UploadFile] = File(...)):
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, f"Upload 1-{MAX_FILES} documents")
    extractor = get_extractor()

    payloads = []
    for f in files:
        data = await f.read()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"{f.filename} is larger than 20 MB")
        mt = _media_type(f)
        if not (mt.startswith("image/") or mt == "application/pdf"):
            raise HTTPException(415, f"{f.filename}: only images and PDFs are supported")
        payloads.append((data, mt, f.filename or "document"))

    try:
        results = await asyncio.gather(*(asyncio.to_thread(extractor.extract, *p) for p in payloads))
    except ExtractionError as e:
        raise HTTPException(502, str(e)) from e

    scan_id = uuid.uuid4().hex[:12]
    result = build_result(scan_id, [p[2] for p in payloads], extractor.name,
                          [r[0] for r in results], [r[1] for r in results])
    _pending[scan_id] = result
    return result


class CheckInRequest(BaseModel):
    scan_id: str
    fields: dict[str, str | None]
    line_items: list[dict[str, str | None]] = []
    reviewed_by: str
    override_reason: str | None = None


@app.post("/api/revalidate")
def revalidate(req: CheckInRequest):
    """Re-run validation on clerk-edited values (no model call)."""
    _, flags, dupes = _validate_reviewed(req)
    return {"flags": flags, "possible_duplicates": dupes}


def _validate_reviewed(req: CheckInRequest):
    scan = _pending.get(req.scan_id)
    if scan is None:
        raise HTTPException(404, "Scan not found or already checked in")
    rules = load_config("validation_rules.json")
    labels = {f.key: f.label for f in scan.fields}
    # Clerk-reviewed values are treated as certain.
    fields = [YmsField(key=k, label=labels.get(k, k), value=(v or None), confidence=1.0)
              for k, v in req.fields.items()]
    items = [YmsLineItem(values={c: (row.get(c) or None) for c in LINE_ITEM_COLUMNS}, min_confidence=1.0)
             for row in req.line_items if any(row.values())]
    flags = validate_line_items(items, fields, rules)
    with db.connect() as conn:
        flags += validate(fields, conn, rules)
        dupes, dupe_flags = find_duplicates(fields, conn, rules)
    flags += dupe_flags
    flags = [f for f in flags if f.code != "total_from_lines"]  # clerk-entered values are final
    return scan, [f.model_dump() for f in flags], dupes


@app.post("/api/visits")
def check_in(req: CheckInRequest):
    if not req.reviewed_by.strip():
        raise HTTPException(400, "reviewed_by is required")
    scan, flags, dupes = _validate_reviewed(req)
    errors = [f for f in flags if f["severity"] == Severity.ERROR]
    if errors and not (req.override_reason or "").strip():
        raise HTTPException(409, {"message": "Resolve errors or provide an override reason", "flags": errors})

    # Record what the clerk changed: the data needed to measure extraction accuracy over time.
    original = {f.key: f.value for f in scan.fields}
    corrections = {k: {"extracted": original.get(k), "final": v}
                   for k, v in req.fields.items() if (original.get(k) or None) != (v or None)}
    original_items = [li.values for li in scan.line_items]
    final_items = [{c: (row.get(c) or None) for c in LINE_ITEM_COLUMNS} for row in req.line_items if any(row.values())]
    if original_items != final_items:
        corrections["_line_items"] = {"extracted": original_items, "final": final_items}
    if req.override_reason:
        corrections["_override"] = {"reason": req.override_reason, "errors": [e["message"] for e in errors]}

    with db.connect() as conn:
        visit_id = db.create_visit(conn, req.fields, req.reviewed_by, corrections, final_items)
    _pending.pop(req.scan_id, None)
    return {"visit_id": visit_id, "corrections": len([k for k in corrections if not k.startswith("_")])}


@app.get("/api/session")
def session_info():
    return {"pin_required": auth.pin() is not None}


@app.get("/api/visits")
def list_visits(limit: int = 50):
    with db.connect() as conn:
        return db.rows(conn, "SELECT id, created_at, status, trailer_number, bol_number, carrier_scac, reviewed_by "
                             "FROM visits ORDER BY id DESC LIMIT ?", limit)


@app.post("/api/visits/{visit_id}/check-out")
def check_out(visit_id: int):
    with db.connect() as conn:
        cur = conn.execute("UPDATE visits SET status = 'checked_out' WHERE id = ? AND status = 'checked_in'", (visit_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Visit not found or not checked in")
    return {"visit_id": visit_id, "status": "checked_out"}
