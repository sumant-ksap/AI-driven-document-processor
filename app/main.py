"""FastAPI app: scan documents -> review -> confirm check-in.

/api/...     endpoints used by the built-in review screen (kept stable).
/api/v1/...  the integration API for other systems (app/api_v1.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import api_v1, auth, db, documents, scanning
from .schemas import ScanResult

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).resolve().parent / "static"


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
        db.fail_interrupted_scans(conn)
    documents.cleanup()
    if os.environ.get("EXTRACTOR_MODE", "ollama").lower() != "mock":
        threading.Thread(target=_warm_up_model, daemon=True).start()
    yield


app = FastAPI(
    title="Gate Document Scanner",
    description="Scan BOLs, IAC forms and shipment documents, extract and validate check-in data, "
                "and create yard visits. Integrations should use the /api/v1 endpoints.",
    version="1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
# Login: PIN session cookie (people) or API key header (systems). Active only when GATE_PIN is set
# (network mode: scripts/serve_network.py); local mode on 127.0.0.1 stays open.
app.middleware("http")(auth.require_auth)
app.get("/login", include_in_schema=False)(lambda: auth.login_page())
app.post("/login", include_in_schema=False)(auth.login_submit)
app.get("/logout", include_in_schema=False)(auth.logout)
app.include_router(api_v1.router)

# Browser apps on other origins (e.g. https://yms.example.com) may call the API only if listed in
# CORS_ORIGINS (comma-separated). Added last so it runs first and answers preflight requests.
_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
                       allow_headers=["Authorization", "X-API-Key", "Content-Type"],
                       expose_headers=["Location", "Content-Disposition"], max_age=600)


@app.get("/", include_in_schema=False)
def index():
    # no-store: after an update the browser must not keep running the old page script.
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- review-screen endpoints


@app.post("/api/scan", response_model=ScanResult, tags=["review screen"])
async def scan(files: list[UploadFile] = File(...)):
    """Scan and wait for the result (used by the review screen). Integrations: POST /api/v1/scans."""
    payloads = await scanning.read_uploads(files)
    scan_id = scanning.start_scan(payloads)
    result = await asyncio.get_running_loop().run_in_executor(scanning.executor, scanning.run_scan, scan_id, payloads)
    if result is None:
        with db.connect() as conn:
            raise HTTPException(502, db.get_scan(conn, scan_id)["error"])
    return result


class CheckInRequest(BaseModel):
    scan_id: str
    fields: dict[str, str | None]
    line_items: list[dict[str, str | None]] = []
    reviewed_by: str
    override_reason: str | None = None


@app.post("/api/revalidate", tags=["review screen"])
def revalidate(req: CheckInRequest):
    """Re-run validation on edited values (no model call)."""
    scan = scanning.load_ready_scan(req.scan_id)
    flags, dupes = scanning.validate_values(req.fields, req.line_items, {f.key: f.label for f in scan.fields})
    return {"flags": flags, "possible_duplicates": dupes}


@app.post("/api/visits", tags=["review screen"])
def check_in(req: CheckInRequest):
    return scanning.check_in(req.scan_id, req.fields, req.line_items, req.reviewed_by, req.override_reason)


@app.get("/api/session", tags=["review screen"])
def session_info():
    return {"pin_required": auth.pin() is not None}


@app.get("/api/visits", tags=["review screen"])
def list_visits(limit: int = 50):
    with db.connect() as conn:
        return db.rows(conn, "SELECT id, created_at, status, trailer_number, bol_number, carrier_scac, reviewed_by "
                             "FROM visits WHERE status != 'deleted' ORDER BY id DESC LIMIT ?", limit)


@app.post("/api/visits/{visit_id}/check-out", tags=["review screen"])
def check_out(visit_id: int):
    with db.connect() as conn:
        cur = conn.execute("UPDATE visits SET status = 'checked_out', updated_at = ? "
                           "WHERE id = ? AND status = 'checked_in'", (db.now(), visit_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Visit not found or not checked in")
    return {"visit_id": visit_id, "status": "checked_out"}
