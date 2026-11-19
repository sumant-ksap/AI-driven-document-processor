"""Checks the request OllamaExtractor sends and how it handles responses, without a running Ollama."""

import base64
import io
import json
from types import SimpleNamespace

import ollama
import pytest
from PIL import Image, ImageDraw

from app.extractor import ExtractionError, OllamaExtractor, pdf_to_images
from app.image_quality import prepare_image
from app.mock_data import sample_extraction


# Request 1: header fields in the compact format (model-facing names: shipper, weight, ...).
COMPACT_ANSWER = {
    "document_type": "bill_of_lading",
    "carrier": {"name": "Roadway Freight Lines", "scac": "RDWY"},
    "shipper": {"company": "CNC Pros", "street": "1846 N Hwy 8", "city": "Helper", "state": "UT",
                "postal_code": "84526", "country": "United States"},
    "shipment": {"ship_date": "1/1/23", "pickup_window": "12pm - 5pm", "weight": "5000 kg"},
    "trailer": {"seal_number": "SL-0098812"},
    "uncertain_fields": ["trailer.seal_number"],
    "image_quality_issues": [],
}
# Request 2: the freight table copied as printed; columns are mapped to fields in code.
TABLE_ANSWER = {
    "headers": ["QTY", "PKG", "PCS", "DESCRIPTION", "CLASS", "NMFC", "DIMS", "WEIGHT"],
    "rows": [["1", "Pallet", "4", "Engine", "85", "120790-102", "4'x4'x4'", "300lbs"]],
}


class FakeClient:
    """Answers each chat() call with the next item of `contents` (the last one repeats)."""

    def __init__(self, content=None, done_reason="stop", error=None, contents=None):
        self.calls = []
        if contents is None:
            contents = [content] if content is not None else [json.dumps(COMPACT_ANSWER), json.dumps(TABLE_ANSWER)]
        self.contents = contents
        self.done_reason = done_reason
        self.error = error

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        content = self.contents[min(len(self.calls), len(self.contents)) - 1]
        return SimpleNamespace(done_reason=self.done_reason, message=SimpleNamespace(content=content))


def _png(size=(3000, 4000)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


def _pdf(pages=2):
    imgs = [Image.new("RGB", (850, 1100), "white") for _ in range(pages)]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def test_request_shape_and_parse():
    client = FakeClient()
    ext, issues = OllamaExtractor(client, model="gemma4:e4b").extract(_png(), "image/png", "bol.png")
    call = client.calls[0]
    assert call["model"] == "gemma4:e4b"
    assert call["format"]["title"] == "CompactExtraction"
    assert call["options"]["temperature"] == 0
    assert call["think"] is False
    user = call["messages"][1]
    assert len(user["images"]) == 1
    img = Image.open(io.BytesIO(base64.b64decode(user["images"][0])))
    assert img.format == "JPEG" and max(img.size) <= 2400  # downscaled before sending
    assert ext.carrier.scac.value == "RDWY"
    # Compact answer expanded and normalized
    assert ext.ship_from.postal_code.value == "84526" and ext.carrier.usdot_number.value is None
    assert ext.shipment.ship_date.value == "2023-01-01" and ext.shipment.pickup_window.value == "12:00-17:00"
    assert ext.shipment.weight_lbs.value == "11023"     # 'weight' (model name) -> weight_lbs, kg converted
    # Table request: continues the same conversation, columns mapped by header
    table_call = client.calls[1]
    assert table_call["format"]["title"] == "TableAnswer"
    assert table_call["messages"][:2] == call["messages"] and table_call["messages"][2]["role"] == "assistant"
    li = ext.line_items[0]
    assert (li.quantity.value, li.pieces.value, li.description.value, li.freight_class.value,
            li.nmfc_number.value, li.weight_lbs.value) == ("1", "4", "Engine", "85", "120790-102", "300")
    assert ext.trailer.seal_number.confidence < 0.8      # listed in uncertain_fields -> needs review
    assert ext.carrier.name.confidence >= 0.8
    assert "image appears blurry" in issues and "image overexposed / washed out" in issues


def test_pdf_pages_rendered_as_images():
    client = FakeClient()
    OllamaExtractor(client).extract(_pdf(2), "application/pdf", "bol.pdf")
    assert len(client.calls[0]["messages"][1]["images"]) == 2


def test_long_pdf_truncated_with_note():
    pages, notes = pdf_to_images(_pdf(3), max_pages=1)
    assert len(pages) == 1 and notes == ["only first 1 of 3 PDF pages were read"]


def test_bad_pdf_raises():
    with pytest.raises(ExtractionError, match="Could not open PDF"):
        OllamaExtractor(FakeClient()).extract(b"%PDF-1.4 not really", "application/pdf", "x.pdf")


@pytest.mark.parametrize("client, match", [
    (FakeClient(content='{"document_type"', done_reason="length"), "cut off"),
    (FakeClient(content='{"document_type": "invoice", "carrier": 5}'), "could not be read"),
    (FakeClient(error=ollama.ResponseError("model not found", 404)), "ollama pull"),
    (FakeClient(error=ConnectionError("refused")), "Is `ollama serve` running"),
])
def test_failures_become_extraction_errors(client, match):
    with pytest.raises(ExtractionError, match=match):
        OllamaExtractor(client).extract(_png((1000, 800)), "image/png", "x.png")


def test_sharp_document_not_flagged():
    img = Image.new("RGB", (1700, 2200), "white")
    d = ImageDraw.Draw(img)
    for y in range(100, 2100, 40):
        d.text((100, y), "BILL OF LADING  BOL-778812  SCAC RDWY  TRAILER RDWY53001  " * 2, fill="black")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert prepare_image(buf.getvalue())[2] == []


def test_table_failure_keeps_header_fields():
    client = FakeClient(contents=[json.dumps(COMPACT_ANSWER), "not json"])
    ext, _ = OllamaExtractor(client).extract(_png((1000, 800)), "image/png", "x.png")
    assert ext.carrier.scac.value == "RDWY" and ext.line_items == []


def test_ocean_table_headers_mapped():
    from app.table import table_to_line_items
    rows = table_to_line_items({"headers": ["TYPE", "PIECES", "DESCRIPTION OF ARTICLES / SPECIAL MARKS", "CLASS", "NMFC", "MARK"],
                                "rows": [["PKG001", "50", "Cartons", None, None, None], [None, None, None, None, None, None]]})
    assert rows == [{"package_type": "PKG001", "pieces": "50", "description": "Cartons"}]
