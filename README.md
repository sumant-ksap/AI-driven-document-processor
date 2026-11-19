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
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8765   # python -m avoids the .exe launchers, which Application Control may block
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

Everything the review screen does goes through a JSON API, so another system (your YMS, a kiosk,
a mobile app, a script) can scan documents and create visits directly.

Interactive docs with a "Try it out" button: open `/docs` on the running server
(e.g. `http://127.0.0.1:8765/docs`; in network mode, enter the PIN in the browser first).
The machine-readable spec is at `/openapi.json`.

### Base URL and login

| Mode | Base URL | Login |
|---|---|---|
| Local (`uvicorn ... --port 8765`) | `http://127.0.0.1:8765` | none |
| Network (`scripts/serve_network.py`) | `https://<PC-IP>:8443` | PIN, see below |

In network mode, log in once by posting the PIN as a form field; the response sets a `gate_session`
cookie (valid 12 hours) that must be sent with every later request. Without it, `/api/*` returns
`401`. Five wrong PINs from one client lock it out for 5 minutes. The certificate is self-signed, so
clients must either trust `certs/cert.pem` or skip verification (`curl -k`, `verify=False`) on a
trusted network only.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/login` | Form field `pin`. Sets the `gate_session` cookie (network mode only). |
| `GET` | `/api/session` | `{"pin_required": true/false}`. Open without login, so clients can check first. |
| `POST` | `/api/scan` | Upload 1-6 documents, get extracted, mapped and validated fields. |
| `POST` | `/api/revalidate` | Re-check edited values without calling the model. |
| `POST` | `/api/visits` | Create the visit (check-in) from reviewed values. |
| `GET` | `/api/visits?limit=50` | Most recent visits. |
| `POST` | `/api/visits/{visit_id}/check-out` | Mark a visit as checked out. |

### Typical flow

1. **Scan**: `POST /api/scan` with the document files. This waits for the model:
   about 100 s per page on a CPU-only PC, so set the client timeout to at least 5 minutes.
2. **Review**: show `fields`, `line_items` and `flags` to a person, or apply your own rules.
   Fields with `needs_review: true` or an `error`/`warning` flag should be checked.
3. **Re-check** (optional): `POST /api/revalidate` with the edited values to see the remaining flags.
4. **Check in**: `POST /api/visits` with the same `scan_id`, the final values and the reviewer's name.

The `scan_id` is kept in server memory until check-in; it is lost if the server restarts, so check in
within the same server session.

### `POST /api/scan`

`multipart/form-data`, one or more `files` parts (JPEG/PNG/WebP photos or PDFs, max 20 MB each,
max 6 per scan; PDFs: first 4 pages). Multiple files are treated as documents for the same truck
(e.g. BOL + IAC form) and merged.

Response (abridged):

```json
{
  "scan_id": "3f9c1a2b7d4e",
  "files": ["bol.jpg"],
  "extractor": "ollama",
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
```

- `fields[].key`: the YMS field names from `config/field_mappings.json` (rename them there to match
  your YMS). Every configured field is returned, with `value: null` when not on the document.
- `confidence`: 0.95 = read normally, 0.5 = the model marked it hard to read, 1.0 = not on the document.
- `matched_record`: set when the value matched master data, e.g. `"carrier:RDWY"`, `"appointment:3"`.
- `flags[].severity`: `error` blocks check-in (unless overridden), `warning` needs review, `info` is informational.
  Common codes: `missing_required`, `missing_recommended`, `low_confidence`, `document_conflict`,
  `bad_format`, `unknown_carrier`, `inactive_carrier`, `no_appointment`, `total_mismatch`,
  `incomplete_address`, `possible_duplicate`, `image_quality`.
- `possible_duplicates`: earlier visits with the same trailer/tractor still on site, or the same BOL or
  seal number within 24 hours.
- Weights are always in lbs (kg converted, original kept in the field note); dates are `YYYY-MM-DD`.

### `POST /api/visits` (and `/api/revalidate`)

JSON body, same for both endpoints:

```json
{
  "scan_id": "3f9c1a2b7d4e",
  "fields": {"bol_number": "BL123456789", "trailer_number": "RDWY53001", "driver_name": "John Smith",
             "carrier_name": "XYZ Shipping"},
  "line_items": [{"package_type": "PKG001", "pieces": "50", "description": "Cartons"}],
  "reviewed_by": "Gate clerk name",
  "override_reason": null
}
```

- `fields`: `key -> value` for the visit. Send back every field you want stored (usually all of them
  from the scan, with corrections applied).
- `line_items`: rows keyed by `quantity, package_type, pieces, description, freight_class, nmfc_number,
  dimensions, weight_lbs, hazmat, marks`.
- `override_reason`: required only to check in despite `error` flags (e.g. a duplicate the clerk confirmed).

`/api/visits` returns `{"visit_id": 12, "corrections": 2}` (`corrections` = fields changed from what
was extracted; the details are stored with the visit). `/api/revalidate` returns
`{"flags": [...], "possible_duplicates": [...]}`.

### Errors

| Status | When |
|---|---|
| `400` | No files, more than 6 files, or `reviewed_by` empty |
| `401` | Network mode and no valid `gate_session` cookie |
| `404` | `scan_id` unknown (already checked in, or server restarted); visit not found for check-out |
| `409` | Check-in has `error` flags and no `override_reason`; body: `{"detail": {"message", "flags"}}` |
| `413` | File larger than 20 MB |
| `415` | File is not an image or PDF |
| `502` | The model could not read the document, or Ollama is not running; `detail` says which |

### Example: curl (Windows)

```powershell
$BASE = "https://192.168.29.135:8443"
# Network mode only: log in and keep the session cookie in a file
curl.exe -k -c cookies.txt -d "pin=YOUR_PIN" "$BASE/login"

# Scan (takes a minute or two)
curl.exe -k -b cookies.txt -F "files=@bol.jpg;type=image/jpeg" "$BASE/api/scan" -o scan.json

# Recent visits
curl.exe -k -b cookies.txt "$BASE/api/visits?limit=5"
```

### Example: Python

```python
import httpx

BASE = "https://192.168.29.135:8443"   # or http://127.0.0.1:8765 (no PIN)
PIN = "YOUR_PIN"

# verify=False because the certificate is self-signed; use only on a trusted network,
# or pass verify="certs/cert.pem" copied from the server PC.
with httpx.Client(base_url=BASE, verify=False, timeout=300) as api:
    if api.get("/api/session").json()["pin_required"]:
        api.post("/login", data={"pin": PIN})          # stores the gate_session cookie
        if "gate_session" not in api.cookies:
            raise SystemExit("Login failed: wrong PIN, or locked out after 5 wrong tries")

    with open("bol.jpg", "rb") as f:
        scan = api.post("/api/scan", files=[("files", ("bol.jpg", f, "image/jpeg"))]).raise_for_status().json()

    fields = {f["key"]: f["value"] for f in scan["fields"]}
    for flag in scan["flags"]:
        print(flag["severity"], flag["message"])

    # Values that came from the gate, not the document
    fields["trailer_number"] = "RDWY53001"
    fields["driver_name"] = "John Smith"

    resp = api.post("/api/visits", json={
        "scan_id": scan["scan_id"],
        "fields": fields,
        "line_items": [li["values"] for li in scan["line_items"]],
        "reviewed_by": "Integration script",
    })
    if resp.status_code == 409:                        # error flags left: fix them or override
        print(resp.json()["detail"])
    else:
        print("Checked in, visit", resp.raise_for_status().json()["visit_id"])
```

### Integration notes

- **One scan at a time per PC**: the model runs on this PC's CPU, so concurrent scans queue up.
  For several gates, run the model on a GPU machine and point `OLLAMA_HOST` at it.
- **Your own master data**: validation reads carriers, drivers, trailers and appointments from
  `app/db.py` (SQLite with sample data). Replace those queries with calls to your YMS.
- **Field names**: change `key`s in `config/field_mappings.json` to your YMS's names; the API returns
  and accepts whatever is configured there.

To try the pipeline and UI without Ollama, set `$env:EXTRACTOR_MODE = "mock"`. The extractor
then returns canned data and ignores the uploaded file, and the UI shows a MOCK MODE badge.

Tests: `.\.venv\Scripts\python -m pytest -q` (no Ollama needed; the model call is tested against a fake client).

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

## Integrating with a real YMS

`app/db.py` is a stand-in. Replace its queries (carriers, drivers, trailers, appointments,
visits) with calls to the YMS API or database; `pipeline.py` only needs rows with the same columns.
Pending scans are held in memory (`_pending` in `main.py`). Move them to the YMS database or Redis
before running more than one server process.

## Model notes

- On this machine `gemma4:e4b` runs on CPU and takes about 65 s per single-page document. A GPU brings this down a lot.
- The model tends to report 100% confidence even on hard-to-read text, so the low-confidence flag rarely fires. Format rules, master-data matching and cross-document conflict checks still catch many errors, but clerks should not treat unflagged fields as verified.

## Open items to define with operations

- Real document samples per carrier/shipper to measure accuracy. The `corrections_json` column on
  each visit records what clerks changed, which gives a per-field error rate.
- Site-specific format rules (trailer number patterns, seal formats, IAC number format).
- Which flags should block check-in (currently: missing required field, inactive carrier, possible duplicate).
- Retention policy for scanned images (currently not stored; only extracted values are saved).
