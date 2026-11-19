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
