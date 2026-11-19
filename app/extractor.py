"""Turns one uploaded document (image or PDF) into a DocumentExtraction.

OllamaExtractor - production path: local Ollama vision model (default gemma4:e4b)
                  constrained to the DocumentExtraction JSON schema.
MockExtractor   - offline demo/testing; returns canned data. Never use at a real gate.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from typing import Protocol

import ollama
from pydantic import ValidationError

from .compact import COMPACT_NAMES, compact_schema_model, expand_compact
from .image_quality import prepare_image
from .normalize import normalize_extraction
from .table import TABLE_INSTRUCTION, TableAnswer, table_to_line_items
from .schemas import DocumentExtraction

log = logging.getLogger(__name__)

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:e4b")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "300"))  # seconds; CPU inference on a 4-page PDF is slow
# Thinking improves reading of ambiguous numbers but several-folds the latency on a small local model.
OLLAMA_THINK = os.environ.get("OLLAMA_THINK", "false").lower() in ("1", "true", "yes")
OLLAMA_NUM_CTX = 16384  # image (~1.3k tokens) + prompt + answer, with headroom for 4-page PDFs
MAX_PDF_PAGES = int(os.environ.get("MAX_PDF_PAGES", "4"))
PDF_RENDER_DPI = 200

SYSTEM_PROMPT = """You read documents presented at a warehouse/distribution-center gate during truck check-in: bills of lading (BOL, including ocean BOLs), Indirect Air Carrier (IAC) / known-shipper forms, packing lists, delivery orders, and driver licenses. Your output auto-fills the Yard Management System, and a gate clerk reviews anything you list in uncertain_fields, so flagging doubtful values matters more than filling every field.

Rules:
- Read the whole page, every box and table, before answering. Fill every field that appears anywhere on the document; use null only for fields that are truly not there.
- Copy values exactly as printed. Do not correct spelling or guess missing characters.
- If a value is hard to read (blur, glare, handwriting, cut off, stamp overlap) or you inferred it rather than read it, give your best reading and add its dotted path (e.g. 'trailer.seal_number', 'line_items.0.weight') to uncertain_fields.
- Distinguish tractor (power unit) numbers from trailer numbers; distinguish seal numbers from PRO/BOL numbers. When a number's role is ambiguous, add it to uncertain_fields.
- Copy dates, pick-up times and weights exactly as printed, including units (e.g. '1/1/23', '12pm - 5pm', '5000 kg'); do not reformat or convert them. SCAC and state codes uppercase; hazmat and signed fields as 'yes' or 'no'.
- A labeled box that is empty on the form (e.g. a blank Customs Agent, PO/Reference or PRO label box) is null. Never copy a label, or a neighboring box's value, into an empty field. Fine print, legal notes and certification text are never field values.
- Labels and values are not always aligned: a value can sit beside a different label in the same box. Assign it by its meaning: a number like 'BL123456789' is the BOL number even if printed next to 'CARRIER:'.
- issuer is the company in the letterhead/header that issued the document (logo, address block at the top). A name printed under a 'CARRIER' or 'CARRIER NAME' label is carrier.name, wherever it appears on the page.
- Party blocks: shipper is the Shipper / Ship From box (not the letterhead), consignee is the Consignee / Ship To box, bill_to is Bill Freight To / third-party billing, carrier is the Carrier box. When a party is a person rather than a company (e.g. 'Shipper: John Smith'), put the name in contact_name. Split each address into street, city, state, postal_code and country, reading every line of the address block; the ZIP/postal code is often on the last line next to the country (e.g. 'United States, 84526').
- ship_date is the pick-up / ship / loading date; estimated_delivery_date is the estimated delivery / discharge / arrival date. ship_date holds only the date and pickup_window only the times, even when the form prints them together.
- Ocean BOLs: put port of loading, port of discharge, vessel and voyage number in ocean.
- shipment.height, length and width are only for overall dimensions printed as separate Height/Length/Width labels; per-row DIMS stay in line_items. Copy DIMS in full (e.g. 4'x4'x4').
- shipment.goods_description, freight_charges (with currency), height, length, width (with units) and weight (gross weight, with unit) come from the summary box, not the table.
- line_items: one entry per row of the freight table, in order. QTY and PIECES/PCS are different columns: a PIECES or PCS column always goes to pieces, and quantity is filled only from a QTY column. Copy each column into its matching field (QTY -> quantity, PKG or TYPE -> package_type, PCS or PIECES -> pieces, DESCRIPTION -> description, CLASS -> freight_class, NMFC -> nmfc_number, DIMS -> dimensions, WEIGHT -> weight, MARK -> marks). Include every non-empty cell; empty cells are null. Skip empty rows. A value may wrap onto two lines inside one cell (e.g. NMFC '120790-' above '102' is '120790-102').
- shipper_signed / carrier_signed: 'yes' only if handwriting or a signature mark is visible in the box; null if the document has no signature boxes.
- List anything that hurts readability in image_quality_issues.
- Respond only with JSON matching the required schema."""

USER_INSTRUCTION = "Extract the check-in fields from this document. Multiple images are pages of the same document."


class Extractor(Protocol):
    name: str

    def extract(self, data: bytes, media_type: str, filename: str) -> tuple[DocumentExtraction, list[str]]:
        """Return the extraction plus locally detected quality issues."""


class ExtractionError(RuntimeError):
    pass


def pdf_to_images(data: bytes, max_pages: int = MAX_PDF_PAGES) -> tuple[list[bytes], list[str]]:
    """Render PDF pages to PNG; Ollama vision models take images, not PDFs."""
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ExtractionError("Could not open PDF (corrupt or password-protected).") from e
    notes = []
    if len(pdf) > max_pages:
        notes.append(f"only first {max_pages} of {len(pdf)} PDF pages were read")
    pages = []
    for i in range(min(len(pdf), max_pages)):
        bitmap = pdf[i].render(scale=PDF_RENDER_DPI / 72)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        pages.append(buf.getvalue())
    pdf.close()
    return pages, notes


class OllamaExtractor:
    name = "ollama"

    def __init__(self, client: ollama.Client | None = None, model: str = OLLAMA_MODEL, think: bool = OLLAMA_THINK):
        self.client = client or ollama.Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)
        self.model = model
        self.think = think

    def _images(self, data: bytes, media_type: str) -> tuple[list[str], list[str]]:
        if media_type == "application/pdf":
            raw_pages, issues = pdf_to_images(data)
        elif media_type.startswith("image/"):
            raw_pages, issues = [data], []
        else:
            raise ExtractionError(f"Unsupported file type: {media_type}")

        images = []
        for page in raw_pages:
            jpeg, _, page_issues = prepare_image(page)
            issues += page_issues
            images.append(base64.b64encode(jpeg).decode())
        return images, sorted(set(issues))

    def _chat(self, messages: list[dict], schema: dict, filename: str):
        try:
            response = self.client.chat(
                model=self.model,
                messages=messages,
                format=schema,  # grammar-constrained JSON output
                think=self.think,
                options={"temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
                keep_alive="30m",  # keep the model loaded between trucks
            )
        except ollama.ResponseError as e:
            log.error("Ollama error for %s: %s", filename, e)
            if e.status_code == 404:
                raise ExtractionError(f"Model {self.model} is not installed. Run: ollama pull {self.model}") from e
            raise ExtractionError(f"Extraction failed: {e.error}") from e
        except (ConnectionError, OSError) as e:  # httpx connect/timeout errors subclass neither; caught below
            raise ExtractionError(f"Could not reach Ollama at {OLLAMA_HOST}. Is `ollama serve` running?") from e
        except Exception as e:
            if type(e).__module__.startswith("httpx"):
                raise ExtractionError(f"Could not reach Ollama at {OLLAMA_HOST} ({type(e).__name__}).") from e
            raise
        if response.done_reason == "length":
            raise ExtractionError("Extraction was cut off; rescan or enter manually.")
        return response

    def extract(self, data, media_type, filename):
        images, local_issues = self._images(data, media_type)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_INSTRUCTION, "images": images},
        ]
        # 1) Header fields, parties, dates... (compact schema: values only, no line items).
        response = self._chat(messages, compact_schema_model().model_json_schema(), filename)
        try:
            # Lenient: a missing key is treated as empty; wrong types still fail validation below.
            answer = json.loads(response.message.content)
            if not isinstance(answer, dict):
                raise ValueError("answer is not a JSON object")
        except ValueError as e:  # json.JSONDecodeError is a ValueError
            log.warning("Unparseable extraction for %s: %s | %s", filename, e, response.message.content[:500])
            raise ExtractionError("The document could not be read automatically; rescan or enter manually.") from e

        # 2) Freight table, copied as printed and mapped to columns in code. Continuing the same
        #    conversation lets Ollama reuse the cached image and first answer, so this is quick.
        answer["line_items"] = self._read_table(messages, response.message.content, filename)

        try:
            ext = expand_compact(answer)
        except (ValidationError, ValueError) as e:
            log.warning("Unparseable extraction for %s: %s | %s", filename, e, response.message.content[:500])
            raise ExtractionError("The document could not be read automatically; rescan or enter manually.") from e
        return normalize_extraction(ext), local_issues

    def _read_table(self, messages: list[dict], first_answer: str, filename: str) -> list[dict]:
        follow_up = messages + [
            {"role": "assistant", "content": first_answer},
            {"role": "user", "content": TABLE_INSTRUCTION},
        ]
        try:
            response = self._chat(follow_up, TableAnswer.model_json_schema(), filename)
            table = json.loads(response.message.content)
            items = table_to_line_items(table if isinstance(table, dict) else {})
        except (ExtractionError, ValueError) as e:
            # The header fields are still useful; the clerk can type the table rows.
            log.warning("Could not read the freight table in %s: %s", filename, e)
            return []
        # expand_compact expects the model-facing names (e.g. 'weight' for weight_lbs).
        return [{COMPACT_NAMES.get(k, k): v for k, v in item.items()} for item in items]


class MockExtractor:
    """Canned output so the UI and validation can be exercised without a model."""

    name = "mock"

    def extract(self, data, media_type, filename):
        from .mock_data import sample_extraction

        return sample_extraction(), []


def get_extractor() -> Extractor:
    mode = os.environ.get("EXTRACTOR_MODE", "ollama").lower()
    if mode == "mock":
        log.warning("EXTRACTOR_MODE=mock: returning canned data, not reading documents.")
        return MockExtractor()
    return OllamaExtractor()
