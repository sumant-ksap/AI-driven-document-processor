"""Integration API, version 1: /api/v1/...

For other systems (YMS, kiosks, mobile apps). Interactive docs at /docs.
- Scans run in the background: POST /scans returns 202 + scan_id; poll GET /scans/{id}.
- Scans, visits and (optionally) the uploaded documents are stored in the database.
- Master data (carriers, drivers, trailers, appointments) can be read and written.
"""


import asyncio
import csv
import io
import json
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db, documents, scanning
from .pipeline import load_config
from .schemas import LINE_ITEM_COLUMNS, ScanResult

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------- models


class FileInfo(BaseModel):
    index: int
    filename: str
    media_type: str
    size: int
    download_url: Optional[str] = Field(None, description="null when documents are not stored")


class ScanJob(BaseModel):
    scan_id: str
    status: Literal["processing", "ready", "failed", "checked_in"]
    created_at: str
    updated_at: str
    error: Optional[str] = None
    visit_id: Optional[int] = Field(None, description="Set once the scan has been checked in")
    files: list[FileInfo]
    result: Optional[ScanResult] = Field(None, description="Present when status is ready or checked_in")


class ReviewedValues(BaseModel):
    fields: dict[str, Optional[str]] = Field(description="YMS field key -> value")
    line_items: list[dict[str, Optional[str]]] = []


class CheckIn(ReviewedValues):
    scan_id: str
    reviewed_by: str
    override_reason: Optional[str] = Field(None, description="Required only to check in despite error flags")


class ValidationResult(BaseModel):
    flags: list[dict]
    possible_duplicates: list[dict]


class CheckInResult(BaseModel):
    visit_id: int
    corrections: int = Field(description="Fields changed from what was extracted")


class Visit(BaseModel):
    id: int
    created_at: str
    updated_at: Optional[str]
    status: Literal["checked_in", "checked_out", "deleted"]
    reviewed_by: Optional[str]
    scan_id: Optional[str]
    trailer_number: Optional[str]
    tractor_number: Optional[str]
    bol_number: Optional[str]
    seal_number: Optional[str]
    driver_license: Optional[str]
    carrier_scac: Optional[str]
    fields: dict[str, Optional[str]]
    line_items: list[dict[str, Optional[str]]]
    corrections: dict = Field(description="Extracted vs final values, overrides, and later edits (_edits)")


class VisitPage(BaseModel):
    items: list[Visit]
    total: int
    limit: int
    offset: int


class VisitEdit(BaseModel):
    fields: dict[str, Optional[str]] = Field({}, description="Only the fields to change; null clears a field")
    line_items: Optional[list[dict[str, Optional[str]]]] = Field(None, description="Replaces all rows when given")
    reviewed_by: str
    reason: Optional[str] = None


class VisitEditResult(BaseModel):
    visit: Visit
    flags: list[dict] = Field(description="Validation of the edited values (informational; edits are saved)")


# ---------------------------------------------------------------- fields


@router.get("/fields", tags=["fields"])
def list_fields():
    """The visit fields this installation fills (from config/field_mappings.json), with validation rules."""
    rules = load_config("validation_rules.json")
    fields = [{"key": f["key"], "label": f["label"], "group": f.get("group", "Other"),
               "required": f["key"] in rules["required_fields"],
               "recommended": f["key"] in rules.get("recommended_fields", []),
               "format": rules["formats"].get(f["key"])}
              for f in load_config("field_mappings.json")["fields"]]
    return {"fields": fields, "line_item_columns": LINE_ITEM_COLUMNS,
            "line_item_formats": rules.get("line_item_formats", {})}


# ---------------------------------------------------------------- scans


def _scan_job(scan: dict) -> ScanJob:
    files = [FileInfo(index=f["index"], filename=f["filename"], media_type=f["media_type"], size=f["size"],
                      download_url=(f"/api/v1/scans/{scan['id']}/documents/{f['index']}"
                                    if f.get("stored") and documents.path(scan["id"], f["stored"]) else None))
             for f in scan["files"]]
    return ScanJob(scan_id=scan["id"], status=scan["status"], created_at=scan["created_at"],
                   updated_at=scan["updated_at"], error=scan["error"], visit_id=scan["visit_id"],
                   files=files, result=scan["result"])


def _get_scan(scan_id: str) -> dict:
    with db.connect() as conn:
        scan = db.get_scan(conn, scan_id)
    if scan is None:
        raise HTTPException(404, "Scan not found")
    return scan


@router.post("/scans", response_model=ScanJob, status_code=202, tags=["scans"],
             responses={200: {"description": "With wait=true: the finished scan"}})
async def create_scan(response: Response, files: list[UploadFile] = File(...),
                      wait: bool = Query(False, description="Wait for the result instead of polling (can take minutes)")):
    """Upload 1-6 documents for one truck (photos or PDFs). Returns at once with status 'processing';
    poll GET /api/v1/scans/{scan_id} until it is 'ready' or 'failed'. Scans queue one at a time."""
    payloads = await scanning.read_uploads(files)
    scan_id = scanning.start_scan(payloads)
    response.headers["Location"] = f"/api/v1/scans/{scan_id}"
    if wait:
        await asyncio.get_running_loop().run_in_executor(scanning.executor, scanning.run_scan, scan_id, payloads)
        response.status_code = 200
    else:
        scanning.executor.submit(scanning.run_scan, scan_id, payloads)
    return _scan_job(_get_scan(scan_id))


@router.get("/scans/{scan_id}", response_model=ScanJob, tags=["scans"])
def get_scan(scan_id: str):
    """Status and, once ready, the extracted and validated result. Works after a server restart."""
    return _scan_job(_get_scan(scan_id))


@router.get("/scans/{scan_id}/documents/{index}", tags=["scans"], response_class=FileResponse)
def get_document(scan_id: str, index: int):
    """Download an uploaded document (kept DOCUMENT_RETENTION_DAYS days, default 90)."""
    scan = _get_scan(scan_id)
    info = next((f for f in scan["files"] if f["index"] == index), None)
    p = documents.path(scan_id, info["stored"]) if info and info.get("stored") else None
    if p is None:
        raise HTTPException(404, "Document not stored (storage off, or deleted after the retention period)")
    return FileResponse(p, media_type=info["media_type"], filename=info["filename"])


@router.post("/scans/{scan_id}/revalidate", response_model=ValidationResult, tags=["scans"])
def revalidate(scan_id: str, body: ReviewedValues):
    """Check edited values against the rules, master data and existing visits (no model call)."""
    scan = scanning.load_ready_scan(scan_id)
    flags, dupes = scanning.validate_values(body.fields, body.line_items, {f.key: f.label for f in scan.fields})
    return {"flags": flags, "possible_duplicates": dupes}


# ---------------------------------------------------------------- visits


@router.post("/visits", response_model=CheckInResult, status_code=201, tags=["visits"],
             responses={409: {"description": "Error flags and no override_reason, or scan not ready"}})
def create_visit(body: CheckIn, response: Response):
    """Check in: create the visit from a ready scan and the reviewed values."""
    result = scanning.check_in(body.scan_id, body.fields, body.line_items, body.reviewed_by, body.override_reason)
    response.headers["Location"] = f"/api/v1/visits/{result['visit_id']}"
    return result


def _visit_filters(status, date_from, date_to, trailer_number, bol_number, carrier_scac, include_deleted):
    return dict(status=status, date_from=date_from.isoformat() if date_from else None,
                date_to=date_to.isoformat() if date_to else None, trailer_number=trailer_number,
                bol_number=bol_number, carrier_scac=carrier_scac, include_deleted=include_deleted)


VisitStatus = Optional[Literal["checked_in", "checked_out", "deleted"]]


@router.get("/visits", response_model=VisitPage, tags=["visits"])
def list_visits(status: VisitStatus = None,
                date_from: Optional[date] = Query(None, alias="from", description="Created on/after (YYYY-MM-DD, UTC)"),
                date_to: Optional[date] = Query(None, alias="to", description="Created on/before (YYYY-MM-DD, UTC)"),
                trailer_number: Optional[str] = None, bol_number: Optional[str] = None,
                carrier_scac: Optional[str] = None, include_deleted: bool = False,
                limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    """Visits, newest first, with filters and paging. Deleted visits are hidden unless asked for."""
    with db.connect() as conn:
        items, total = db.search_visits(conn, limit=limit, offset=offset, **_visit_filters(
            status, date_from, date_to, trailer_number, bol_number, carrier_scac, include_deleted))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _csv_cell(v) -> str:
    s = "" if v is None else str(v)
    # Stop spreadsheet apps from running a value as a formula.
    return "'" + s if s[:1] in ("=", "+", "@", "\t", "\r") or (s[:1] == "-" and not s[1:2].isdigit()) else s


@router.get("/visits/export.csv", tags=["visits"], response_class=Response,
            responses={200: {"content": {"text/csv": {}}, "description": "One row per visit"}})
def export_visits(status: VisitStatus = None,
                  date_from: Optional[date] = Query(None, alias="from"), date_to: Optional[date] = Query(None, alias="to"),
                  trailer_number: Optional[str] = None, bol_number: Optional[str] = None,
                  carrier_scac: Optional[str] = None, include_deleted: bool = False):
    """CSV export (same filters as GET /visits, up to 10,000 rows): one column per visit field,
    line items as JSON. UTF-8 with BOM so Excel opens it correctly."""
    keys = [f["key"] for f in load_config("field_mappings.json")["fields"]]
    with db.connect() as conn:
        items, _ = db.search_visits(conn, limit=10000, offset=0, **_visit_filters(
            status, date_from, date_to, trailer_number, bol_number, carrier_scac, include_deleted))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["visit_id", "created_at", "updated_at", "status", "reviewed_by", "scan_id", *keys,
                "line_item_count", "line_items_json", "corrected_fields"])
    for v in items:
        corrected = len([k for k in v["corrections"] if not k.startswith("_")])
        w.writerow([_csv_cell(x) for x in (v["id"], v["created_at"], v["updated_at"], v["status"], v["reviewed_by"],
                                           v["scan_id"], *(v["fields"].get(k) for k in keys), len(v["line_items"]),
                                           json.dumps(v["line_items"]), corrected)])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="visits_{date.today().isoformat()}.csv"'})


def _get_visit(visit_id: int) -> dict:
    with db.connect() as conn:
        v = db.get_visit(conn, visit_id)
    if v is None:
        raise HTTPException(404, "Visit not found")
    return v


@router.get("/visits/{visit_id}", response_model=Visit, tags=["visits"])
def get_visit(visit_id: int):
    """Full visit: all fields, line items, and the record of corrections, overrides and edits."""
    return _get_visit(visit_id)


@router.patch("/visits/{visit_id}", response_model=VisitEditResult, tags=["visits"])
def edit_visit(visit_id: int, body: VisitEdit):
    """Correct a visit after check-in. Each edit is recorded (who, when, why, old -> new) in corrections._edits."""
    if not body.reviewed_by.strip():
        raise HTTPException(400, "reviewed_by is required")
    v = _get_visit(visit_id)
    if v["status"] == "deleted":
        raise HTTPException(409, "Visit is deleted")
    fields = {**v["fields"], **body.fields}
    changes = {k: {"from": v["fields"].get(k), "to": val} for k, val in body.fields.items()
               if (v["fields"].get(k) or None) != (val or None)}
    line_items = v["line_items"]
    if body.line_items is not None:
        new_items = scanning.clean_line_items(body.line_items)
        if new_items != line_items:
            changes["_line_items"] = {"from": line_items, "to": new_items}
        line_items = new_items
    corrections = v["corrections"]
    if changes:
        corrections.setdefault("_edits", []).append(
            {"at": db.now(), "by": body.reviewed_by, "reason": body.reason, "changes": changes})
        with db.connect() as conn:
            db.update_visit(conn, visit_id, fields, line_items, corrections)
    flags, _ = scanning.validate_values(fields, line_items)
    # The visit itself would show up as its own duplicate; drop that.
    flags = [f for f in flags if not (f["code"] == "possible_duplicate" and f"visit #{visit_id} " in f["message"])]
    return {"visit": _get_visit(visit_id), "flags": flags}


@router.delete("/visits/{visit_id}", status_code=204, tags=["visits"])
def delete_visit(visit_id: int, reviewed_by: str = Query(...), reason: Optional[str] = None):
    """Mark a visit as deleted (kept for the audit trail, hidden from lists and duplicate checks)."""
    v = _get_visit(visit_id)
    if v["status"] != "deleted":
        corrections = v["corrections"]
        corrections.setdefault("_edits", []).append(
            {"at": db.now(), "by": reviewed_by, "reason": reason, "changes": {"status": {"from": v["status"], "to": "deleted"}}})
        with db.connect() as conn:
            db.update_visit(conn, visit_id, v["fields"], v["line_items"], corrections)
            conn.execute("UPDATE visits SET status = 'deleted' WHERE id = ?", (visit_id,))
    return Response(status_code=204)


@router.post("/visits/{visit_id}/check-out", response_model=Visit, tags=["visits"])
def check_out(visit_id: int):
    v = _get_visit(visit_id)
    if v["status"] != "checked_in":
        raise HTTPException(409, f"Visit is {v['status'].replace('_', ' ')}, not checked in")
    with db.connect() as conn:
        conn.execute("UPDATE visits SET status = 'checked_out', updated_at = ? WHERE id = ?", (db.now(), visit_id))
    return _get_visit(visit_id)


# ---------------------------------------------------------------- master data


class Carrier(BaseModel):
    scac: str = Field(pattern=r"^[A-Z]{2,4}$", description="2-4 uppercase letters")
    name: str
    usdot_number: Optional[str] = None
    mc_number: Optional[str] = None
    active: bool = True


class Driver(BaseModel):
    id: Optional[int] = Field(None, description="Omit to create; give an existing id to update")
    name: str
    license_number: Optional[str] = None
    license_state: Optional[str] = None
    carrier_scac: Optional[str] = None


class Trailer(BaseModel):
    trailer_number: str
    carrier_scac: Optional[str] = None
    license_plate: Optional[str] = None


class Appointment(BaseModel):
    id: Optional[int] = Field(None, description="Omit to create; give an existing id to update")
    bol_number: Optional[str] = None
    po_numbers: Optional[str] = Field(None, description="Comma-separated")
    carrier_scac: Optional[str] = None
    expected_date: Optional[date] = None
    status: Literal["scheduled", "arrived", "cancelled"] = "scheduled"


def _master_routes(table: str, model: type[BaseModel], key_type: type):
    key = db.MASTER_TABLES[table][0]
    tag = f"master data: {table}"

    def list_(limit: int = Query(500, ge=1, le=5000), offset: int = Query(0, ge=0)):
        with db.connect() as conn:
            items, total = db.list_master(conn, table, limit, offset)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def upsert(body: model | list[model]):  # type: ignore[valid-type]
        records = body if isinstance(body, list) else [body]
        with db.connect() as conn:
            items = [db.upsert_master(conn, table, r.model_dump(mode="json", exclude_unset=False)) for r in records]
        return {"items": items}

    def delete(key_value: key_type):  # type: ignore[valid-type]
        with db.connect() as conn:
            if not db.delete_master(conn, table, key_value):
                raise HTTPException(404, f"No {table[:-1]} with {key} {key_value}")
        return Response(status_code=204)

    list_.__doc__ = f"List {table}."
    upsert.__doc__ = (f"Create or update {table}: send one object or a list (bulk sync from your YMS). "
                      f"Records with an existing {key} are updated.")
    delete.__doc__ = f"Delete one of the {table} by {key}."
    router.add_api_route(f"/{table}", list_, methods=["GET"], tags=[tag], name=f"list_{table}")
    router.add_api_route(f"/{table}", upsert, methods=["POST"], tags=[tag], name=f"upsert_{table}")
    router.add_api_route(f"/{table}/{{key_value}}", delete, methods=["DELETE"], status_code=204, tags=[tag],
                         name=f"delete_{table}")


_master_routes("carriers", Carrier, str)
_master_routes("drivers", Driver, int)
_master_routes("trailers", Trailer, str)
_master_routes("appointments", Appointment, int)
