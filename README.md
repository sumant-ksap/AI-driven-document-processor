# AI Document Scanning & Auto-Fill for Gate Check-In

Scans BOLs, IAC forms and other shipment documents at the gate, extracts truck, trailer,
driver, carrier and shipment details with a local Ollama vision model (default `gemma4:e4b`), fills the YMS visit fields, checks them
against master data, flags missing or uncertain values, and detects possible duplicate
check-ins. A gate clerk reviews the results before the visit is created.

## Flow

```
upload (photo/PDF, 1-6 docs)
  -> local image checks (resolution, darkness, washout, blur; auto-rotate; downscale)
  -> PDFs rendered to page images (first 4 pages)
  -> Ollama vision model, output constrained to the DocumentExtraction JSON schema, with per-field confidence
  -> map to YMS fields (config/field_mappings.json); best value across docs wins, conflicts flagged
  -> validate (config/validation_rules.json): required, formats, confidence threshold
  -> match master data: carrier SCAC/USDOT/active, driver (license or fuzzy name), trailer owner, appointment by BOL
  -> duplicate check: trailer/tractor still on site, same BOL or seal within the window
  -> clerk reviews highlighted fields -> confirm (errors need an override reason)
  -> visit saved with reviewer name and a log of every field the clerk corrected
```

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
ollama pull gemma4:e4b          # once; Ollama must be running (ollama serve / tray app)
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8765 --timeout-keep-alive 65   # python -m avoids the .exe launchers, which Application Control may block
# open http://127.0.0.1:8765   (port 8000 is used by another app on this PC)
```

### Tablets and phones (network mode: HTTPS + PIN)

```powershell
.\.venv\Scripts\python scripts\serve_network.py
```

- Open `https://<this PC's IP>:8443` on a device on the same Wi-Fi (the script prints the address and PIN).
- First visit on each device: "connection not private" → **Advanced → Proceed**. The certificate is
  self-signed; browsers only allow the live camera over HTTPS.
- The PIN is in `data/gate_pin.txt` (random 6 digits on first run). Edit it and restart to change it;
  that logs every device out. Sessions last 12 hours; 5 wrong PINs lock a device out for 5 minutes.
- If the PC's IP changes, recreate the certificate: `.\.venv\Scripts\python scripts\make_cert.py`.
- Without `GATE_PIN` set (the plain uvicorn command above), there is no PIN: keep that mode on localhost.

## REST API

Other systems (your YMS, a kiosk, a mobile or web app, a script) integrate through the versioned
API under **`/api/v1`**. The `/api/...` endpoints without a version are used by the built-in review
screen and are kept as they are, but new integrations should use `/api/v1`.

Interactive docs with a "Try it out" button: open `/docs` on the running server
(e.g. `http://127.0.0.1:8765/docs`; in network mode, enter the PIN in the browser first).
The machine-readable OpenAPI spec is at `/openapi.json`; you can generate a client from it.

### Base URL and authentication

| Mode | Base URL | Authentication |
|---|---|---|
| Local (`uvicorn ... --port 8765`) | `http://127.0.0.1:8765/api/v1` | none (only reachable from this PC) |
| Network (`scripts/serve_network.py`) | `https://<PC-IP>:8443/api/v1` | API key (systems) or PIN session (people) |

**API keys** (for systems). Create one per integration on the server PC:

```powershell
.\.venv\Scripts\python scripts\api_key.py create "YMS integration"   # prints the key once
.\.venv\Scripts\python scripts\api_key.py list
.\.venv\Scripts\python scripts\api_key.py revoke 3
```

Send it on every request as `X-API-Key: <key>` or `Authorization: Bearer <key>`. Only a hash of the
key is stored, so a lost key cannot be recovered: revoke it and create a new one. A missing, wrong or
revoked key gets `401`.

**HTTPS**: the network-mode certificate is self-signed. Clients should trust `certs/cert.pem` copied
from the server PC (`verify="cert.pem"` in Python, `--cacert cert.pem` in curl), or skip verification
on a trusted network only.

**Browser apps on another website** (e.g. `https://yms.example.com` calling this API from
JavaScript) must be allowed explicitly: start the server with
`$env:CORS_ORIGINS = "https://yms.example.com"` (comma-separate several). Use an API key from the
browser app; the PIN cookie is not sent across sites.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/fields` | The visit fields, groups, required/recommended flags and formats; build forms from this. |
| `POST` | `/api/v1/scans` | Upload 1-6 documents. Returns at once (`202`) with a `scan_id`; `?wait=true` waits for the result. |
| `GET` | `/api/v1/scans/{scan_id}` | Scan status (`processing`, `ready`, `failed`, `checked_in`) and, when ready, the result. |
| `GET` | `/api/v1/scans/{scan_id}/documents/{index}` | Download an uploaded document. |
| `POST` | `/api/v1/scans/{scan_id}/revalidate` | Check edited values without calling the model. |
| `POST` | `/api/v1/visits` | Check in: create the visit (`201`, `Location` header). |
| `GET` | `/api/v1/visits` | Visits, newest first. Filters: `status`, `from`, `to` (YYYY-MM-DD), `trailer_number`, `bol_number`, `carrier_scac`, `include_deleted`; paging: `limit` (max 500), `offset`. |
| `GET` | `/api/v1/visits/export.csv` | Same filters, as CSV (one column per field, line items as JSON). |
| `GET` | `/api/v1/visits/{visit_id}` | Full visit: all fields, line items, corrections and edit history. |
| `PATCH` | `/api/v1/visits/{visit_id}` | Correct a visit after check-in (recorded with who, when, why). |
| `DELETE` | `/api/v1/visits/{visit_id}?reviewed_by=...&reason=...` | Mark as deleted (kept for audit, hidden from lists and duplicate checks). |
| `POST` | `/api/v1/visits/{visit_id}/check-out` | Check the truck out. |
| `GET` / `POST` | `/api/v1/carriers`, `/drivers`, `/trailers`, `/appointments` | List, or create/update master data (one object or a list). |
| `DELETE` | `/api/v1/carriers/{scac}`, `/drivers/{id}`, `/trailers/{trailer_number}`, `/appointments/{id}` | Delete a master-data record. |

### Typical flow

1. **Scan**: `POST /api/v1/scans` with the files. You get `202` and a `scan_id` immediately.
2. **Poll**: `GET /api/v1/scans/{scan_id}` every 2-3 seconds until `status` is `ready` (or `failed`,
   with the reason in `error`). Reading takes about 1.5 minutes per page on a CPU-only PC; scans queue
   and run one at a time. Retry a poll that fails with a dropped connection.
3. **Review**: show `result.fields`, `result.line_items` and `result.flags` to a person, or apply
   your own rules. Fields with `needs_review: true` or an `error`/`warning` flag should be checked.
4. **Re-check** (optional): `POST /api/v1/scans/{scan_id}/revalidate` with the edited values.
5. **Check in**: `POST /api/v1/visits` with the `scan_id`, the final values and the reviewer's name.
6. Later: `GET`/`PATCH` the visit, `POST .../check-out` when the truck leaves.

Scans, results and visits are stored in the database, so a scan can be fetched or checked in after a
server restart. A scan that was still being read when the server stopped is marked `failed`; scan again.

### Scan: `POST /api/v1/scans` and `GET /api/v1/scans/{scan_id}`

Upload: `multipart/form-data` with one or more `files` parts (JPEG/PNG/WebP photos or PDFs, max
20 MB each, max 6 per scan; PDFs: first 4 pages). Several files are treated as documents for the
same truck (e.g. BOL + IAC form) and merged.

Response (abridged; `result` is `null` until `status` is `ready`):

```json
{
  "scan_id": "1d04902394dc",
  "status": "ready",
  "created_at": "2026-11-19T11:47:02+00:00",
  "updated_at": "2026-11-19T11:48:20+00:00",
  "error": null,
  "visit_id": null,
  "files": [{"index": 0, "filename": "bol.jpg", "media_type": "image/jpeg", "size": 412233,
             "download_url": "/api/v1/scans/1d04902394dc/documents/0"}],
  "result": {
    "document_types": ["bill_of_lading"],
    "fields": [
      {"key": "bol_number", "label": "BOL #", "group": "Shipment", "value": "BL123456789",
       "confidence": 0.95, "source": "shipment.bol_number", "needs_review": false, "matched_record": null},
      {"key": "trailer_number", "label": "Trailer #", "group": "Equipment", "value": null,
       "confidence": 1.0, "source": null, "needs_review": true, "matched_record": null}
    ],
    "line_items": [
      {"values": {"quantity": null, "package_type": "PKG001", "pieces": "50", "description": "Cartons",
                  "freight_class": null, "nmfc_number": null, "dimensions": null, "weight_lbs": null,
                  "hazmat": null, "marks": null},
       "min_confidence": 0.95, "needs_review": false}
    ],
    "flags": [
      {"field": "trailer_number", "severity": "error", "code": "missing_required",
       "message": "Trailer # is required but was not found"}
    ],
    "possible_duplicates": [],
    "ready_for_check_in": false
  }
}
```

- `fields[].key`: the field names from `config/field_mappings.json` (rename them there to match your
  YMS; `GET /api/v1/fields` lists them). Every configured field is returned, `value: null` when absent.
- `confidence`: 0.95 = read normally, 0.5 = the model marked it hard to read, 1.0 = not on the document.
- `matched_record`: set when the value matched master data, e.g. `"carrier:RDWY"`, `"appointment:3"`.
- `flags[].severity`: `error` blocks check-in (unless overridden), `warning` needs review, `info` is
  informational. Common codes: `missing_required`, `missing_recommended`, `low_confidence`,
  `document_conflict`, `bad_format`, `unknown_carrier`, `inactive_carrier`, `carrier_name_matched`,
  `no_appointment`, `total_mismatch`, `incomplete_address`, `possible_duplicate`, `image_quality`.
- `possible_duplicates`: visits with the same trailer/tractor still on site, or the same BOL or seal
  number within 24 hours.
- Weights are in lbs (kg converted, original kept in the field note); dates are `YYYY-MM-DD`.
- `download_url` is `null` when documents are not stored or were removed after the retention period.

### Check in: `POST /api/v1/visits` (and `.../revalidate`)

```json
{
  "scan_id": "1d04902394dc",
  "fields": {"bol_number": "BL123456789", "trailer_number": "XYZU1234567", "driver_name": "Sam Driver",
             "carrier_name": "XYZ Shipping", "carrier_scac": "XYZS"},
  "line_items": [{"package_type": "PKG001", "pieces": "50", "description": "Cartons"}],
  "reviewed_by": "YMS integration",
  "override_reason": null
}
```

- `fields`: send every field you want stored (usually all fields from the scan, with corrections).
- `line_items`: rows keyed by `quantity, package_type, pieces, description, freight_class,
  nmfc_number, dimensions, weight_lbs, hazmat, marks`.
- `override_reason`: needed only to check in despite `error` flags (e.g. a confirmed duplicate).
- Returns `201` `{"visit_id": 12, "corrections": 2}`. The revalidate endpoint takes only
  `fields` and `line_items` and returns `{"flags": [...], "possible_duplicates": [...]}`.

### Edit: `PATCH /api/v1/visits/{visit_id}`

```json
{"fields": {"seal_number": "SL-0098812"}, "reviewed_by": "Dock supervisor", "reason": "seal re-read at dock"}
```

Only the listed fields change (`null` clears one); `line_items`, if given, replaces all rows. Every
edit is appended to `corrections._edits` with who, when, why and old → new values. The response
contains the updated visit and validation `flags` for the new values (informational: the edit is saved).

### Master data

Validation matches scans against carriers, drivers, trailers and appointments. Keep them in sync
from your YMS with bulk upserts:

```json
POST /api/v1/carriers
[{"scac": "XYZS", "name": "XYZ Shipping", "usdot_number": "1234567", "active": true},
 {"scac": "OLDX", "name": "Old Express Inc", "active": false}]
```

Records with an existing key (`scac`, `trailer_number`, or `id` for drivers/appointments) are
updated; others are created. Omit `id` to create a driver or appointment. Appointments take
`bol_number`, `po_numbers`, `carrier_scac`, `expected_date` (YYYY-MM-DD) and `status`
(`scheduled`, `arrived`, `cancelled`).

### Errors

| Status | When |
|---|---|
| `400` | No files, more than 6 files, or `reviewed_by` empty |
| `401` | Network mode without a valid API key or PIN session |
| `404` | Scan, visit, document or master-data record not found |
| `409` | Check-in with `error` flags and no `override_reason` (body: `{"detail": {"message", "flags"}}`); scan still processing, failed or already checked in; visit deleted or not checked in |
| `413` | File larger than 20 MB |
| `415` | File is not an image or PDF |
| `422` | Request body or query parameter invalid (e.g. SCAC not 2-4 uppercase letters, bad date) |
| `502` | Review-screen endpoint `/api/scan` only: the model could not read the document (v1 reports this as `status: failed`) |

### Example: curl (Windows)

```powershell
$BASE = "https://192.168.29.135:8443/api/v1"
$KEY  = "gsk_..."          # from scripts\api_key.py create

# Start a scan, then poll it
curl.exe -k -H "X-API-Key: $KEY" -F "files=@bol.jpg;type=image/jpeg" "$BASE/scans"
curl.exe -k -H "X-API-Key: $KEY" "$BASE/scans/1d04902394dc"

# Today's visits as CSV
curl.exe -k -H "X-API-Key: $KEY" "$BASE/visits/export.csv?from=2026-11-19" -o visits.csv
```

### Example: Python

```python
import time
import httpx

BASE = "https://192.168.29.135:8443"   # or http://127.0.0.1:8765 (local mode, no key needed)
KEY = "gsk_..."                        # from scripts\api_key.py create

# verify="cert.pem": the server's certs/cert.pem copied to this machine (self-signed certificate).
with httpx.Client(base_url=BASE, headers={"X-API-Key": KEY}, verify="cert.pem", timeout=60) as api:
    # 1. Start the scan (returns immediately)
    with open("bol.jpg", "rb") as f:
        job = api.post("/api/v1/scans", files=[("files", ("bol.jpg", f, "image/jpeg"))]).raise_for_status().json()

    # 2. Poll until it has been read
    while job["status"] == "processing":
        time.sleep(3)
        job = api.get(f"/api/v1/scans/{job['scan_id']}").raise_for_status().json()
    if job["status"] == "failed":
        raise SystemExit(f"Scan failed: {job['error']}")

    result = job["result"]
    fields = {f["key"]: f["value"] for f in result["fields"]}
    for flag in result["flags"]:
        print(flag["severity"], flag["message"])

    # 3. Add what came from the gate, not the document, and check in
    fields.update(trailer_number="XYZU1234567", driver_name="Sam Driver")
    resp = api.post("/api/v1/visits", json={
        "scan_id": job["scan_id"],
        "fields": fields,
        "line_items": [li["values"] for li in result["line_items"]],
        "reviewed_by": "YMS integration",
    })
    if resp.status_code == 409:                 # error flags left: fix them or send override_reason
        print(resp.json()["detail"])
    else:
        visit_id = resp.raise_for_status().json()["visit_id"]
        print("Checked in, visit", visit_id, api.get(f"/api/v1/visits/{visit_id}").json()["status"])
```

### Integration notes

- **One scan at a time per PC**: scans queue on the CPU. For several gates, run the model on a GPU
  machine and point `OLLAMA_HOST` at it.
- **Stored documents**: uploads are kept in `data/scans/` for `DOCUMENT_RETENTION_DAYS` (default 90)
  and then deleted; `STORE_DOCUMENTS=false` keeps only the extracted data. BOLs and licences contain
  personal data: set the retention to your policy.
- **Field names**: change the `key`s in `config/field_mappings.json` to your YMS's names; the API
  returns and accepts whatever is configured there.
- **Versioning**: breaking changes will go to `/api/v2`; `/api/v1` keeps working.

## Configuration

| File / env var | Purpose |
|---|---|
| `config/field_mappings.json` | Extraction path to YMS field key/label. Rename keys to match your YMS. |
| `config/validation_rules.json` | Required fields, format regexes, review confidence threshold (0.80), duplicate window, fuzzy-match threshold |
| `OLLAMA_MODEL` | Vision model, default `gemma4:e4b` |
| `OLLAMA_HOST` | Default `http://127.0.0.1:11434` |
| `OLLAMA_THINK` | `true` enables model thinking: may read ambiguous fields better, much slower. Default `false` |
| `OLLAMA_TIMEOUT` | Seconds per document, default 300 |
| `MAX_PDF_PAGES` | Pages read per PDF, default 4 |
| `YMS_DB_PATH` | SQLite file (default `data/yms.db`), seeded with demo carriers/drivers/trailers/appointments |
| `CORS_ORIGINS` | Comma-separated websites allowed to call the API from a browser, e.g. `https://yms.example.com`. Default: none |
| `STORE_DOCUMENTS` | `false` to keep only extracted data, not the uploaded files. Default `true` |
| `DOCUMENT_RETENTION_DAYS` | Uploaded files older than this are deleted at startup. Default `90` |
| `SCAN_DOCS_DIR` | Where uploaded files are kept. Default `data/scans` next to the database |
| `GATE_PIN` | Turns on login (PIN for people, API keys for systems). Set by `scripts/serve_network.py` |

## Integrating with a real YMS

Two ways to connect:

- **Push master data in** through the API (`POST /api/v1/carriers`, `/drivers`, `/trailers`,
  `/appointments`), e.g. a nightly sync plus updates when appointments are booked. No code changes.
- **Read the YMS directly**: `app/db.py` is a stand-in SQLite store. Replace its queries with calls
  to the YMS API or database; `pipeline.py` only needs rows with the same columns.

Scans and visits are stored in SQLite. It suits one server process; for several, move `app/db.py`
to a shared database server.

## Model notes

- On this machine `gemma4:e4b` runs on CPU and takes about 65 s per single-page document. A GPU brings this down a lot.
- The model tends to report 100% confidence even on hard-to-read text, so the low-confidence flag rarely fires. Format rules, master-data matching and cross-document conflict checks still catch many errors, but clerks should not treat unflagged fields as verified.

## Open items to define with operations

- Real document samples per carrier/shipper to measure accuracy. The `corrections_json` column on
  each visit records what clerks changed, which gives a per-field error rate.
- Site-specific format rules (trailer number patterns, seal formats, IAC number format).
- Which flags should block check-in (currently: missing required field, inactive carrier, possible duplicate).
- Retention policy for scanned images (currently not stored; only extracted values are saved).
