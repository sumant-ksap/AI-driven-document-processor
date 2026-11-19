import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import db
from app.mock_data import _f, sample_extraction
from app.pipeline import find_duplicates, load_config, map_to_yms, validate
from app.schemas import Severity

MAPPINGS = load_config("field_mappings.json")
RULES = load_config("validation_rules.json")


@pytest.fixture
def conn(tmp_path):
    with db.connect(tmp_path / "t.db") as c:
        db.seed_demo_data(c)
        yield c


def codes(flags):
    return {f.code for f in flags}


def run(conn, *exts):
    fields, flags = map_to_yms(list(exts), MAPPINGS, RULES)
    flags += validate(fields, conn, RULES)
    return {f.key: f for f in fields}, flags


def test_clean_document_matches_master_data(conn):
    fields, flags = run(conn, sample_extraction(**{"trailer.seal_number": _f("SL-1")}))
    assert fields["carrier_scac"].matched_record == "carrier:RDWY"
    assert fields["trailer_number"].matched_record == "trailer:RDWY53001"
    assert fields["bol_number"].matched_record.startswith("appointment:")
    assert fields["driver_name"].matched_record  # fuzzy: "John Smith" ~ "John A. Smith"
    assert not [f for f in flags if f.severity != Severity.INFO]


def test_low_confidence_value_needs_review(conn):
    fields, flags = run(conn, sample_extraction())  # seal number is 0.55 in the sample
    assert fields["seal_number"].needs_review
    assert "low_confidence" in codes(flags)


def test_missing_required_and_bad_format(conn):
    fields, flags = run(conn, sample_extraction(**{"trailer.trailer_number": _f(None, 1.0),
                                                   "shipment.bol_number": _f(None, 1.0), "carrier.scac": _f("RD1")}))
    assert any(f.code == "missing_required" and f.field == "trailer_number" and f.severity == Severity.ERROR for f in flags)
    assert any(f.code == "missing_recommended" and f.field == "bol_number" and f.severity == Severity.WARNING for f in flags)
    assert any(f.code == "bad_format" and f.field == "carrier_scac" for f in flags)


def test_carrier_matched_by_name_when_no_scac(conn):
    _, flags = run(conn, sample_extraction(**{"carrier.scac": _f(None, 1.0), "carrier.name": _f("Roadway Freight Lines Inc")}))
    assert any(f.code == "carrier_name_matched" and "RDWY" in f.message for f in flags)
    _, flags = run(conn, sample_extraction(**{"carrier.scac": _f(None, 1.0), "carrier.name": _f("TRUCKING INC.")}))
    assert "unknown_carrier" in codes(flags)


# ------------------------------------------------------------------ line items

from app.pipeline import merge_line_items, validate_line_items  # noqa: E402


def lines_and_flags(ext):
    fields, _ = map_to_yms([ext], MAPPINGS, RULES)
    items = merge_line_items([ext], RULES)
    return items, validate_line_items(items, fields, RULES), {f.key: f for f in fields}


def test_line_items_merged_and_totals_agree():
    items, flags, _ = lines_and_flags(sample_extraction())
    assert len(items) == 2 and items[1].values["description"] == "Engine"
    assert not [f for f in flags if f.severity != Severity.INFO]


def test_total_mismatch_and_bad_class_flagged():
    items, flags, fields = lines_and_flags(sample_extraction(**{
        "line_items.0.weight_lbs": _f("14000"), "line_items.1.freight_class": _f("86")}))
    assert any(f.code == "total_mismatch" and f.field == "weight_lbs" for f in flags)
    assert fields["weight_lbs"].needs_review
    assert any(f.code == "bad_format" and f.field == "line_items.2" for f in flags) and items[1].needs_review


def test_missing_total_filled_from_lines():
    _, flags, fields = lines_and_flags(sample_extraction(**{"shipment.weight_lbs": _f(None, 1.0)}))
    assert fields["weight_lbs"].value == "18250"
    assert "total_from_lines" in codes(flags)


def test_unknown_and_inactive_carriers(conn):
    _, flags = run(conn, sample_extraction(**{"carrier.scac": _f("ZZZZ")}))
    assert "unknown_carrier" in codes(flags)
    _, flags = run(conn, sample_extraction(**{"carrier.scac": _f("OLDX"), "carrier.name": _f("Old Express Inc")}))
    assert any(f.code == "inactive_carrier" and f.severity == Severity.ERROR for f in flags)


def test_trailer_registered_to_other_carrier(conn):
    _, flags = run(conn, sample_extraction(**{"trailer.trailer_number": _f("SWFT48812")}))
    assert "trailer_carrier_mismatch" in codes(flags)


def test_documents_disagree(conn):
    bol = sample_extraction()
    iac = sample_extraction(**{"document_type": "iac_form", "trailer.trailer_number": _f("RDWY53009")})
    fields, flags = run(conn, bol, iac)
    assert "document_conflict" in codes(flags)
    assert fields["trailer_number"].needs_review


def test_best_value_wins_across_documents(conn):
    blurry = sample_extraction(**{"trailer.seal_number": _f("SL-0098817", 0.4)})
    clear = sample_extraction(**{"trailer.seal_number": _f("SL-0098812", 0.95)})
    fields, _ = run(conn, blurry, clear)
    assert fields["seal_number"].value == "SL-0098812"


def test_duplicate_check_in_detected(conn):
    fields, _ = run(conn, sample_extraction())
    assert find_duplicates(list(fields.values()), conn, RULES)[0] == []
    db.create_visit(conn, {"trailer_number": "rdwy-53001", "bol_number": "BOL-000"}, "tester", None)
    dupes, flags = find_duplicates(list(fields.values()), conn, RULES)
    assert len(dupes) == 1 and "trailer already checked in" in dupes[0]["reasons"][0]
    assert flags[0].severity == Severity.ERROR


# ------------------------------------------------------------------ API


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACTOR_MODE", "mock")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.db")
    from app.main import app
    with TestClient(app) as c:
        yield c


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (1000, 800), "white").save(buf, "PNG")
    return ("bol.png", buf.getvalue(), "image/png")


def test_scan_review_checkin_flow(client):
    r = client.post("/api/scan", files=[("files", _png())])
    assert r.status_code == 200, r.text
    scan = r.json()
    assert scan["extractor"] == "mock" and not scan["ready_for_check_in"]  # seal needs review
    fields = {f["key"]: f["value"] for f in scan["fields"]}
    fields["seal_number"] = "SL-0098812"
    lines = [li["values"] for li in scan["line_items"]]
    assert len(lines) == 2

    r = client.post("/api/visits", json={"scan_id": scan["scan_id"], "fields": fields, "line_items": lines,
                                         "reviewed_by": "clerk"})
    assert r.status_code == 200, r.text
    assert r.json()["corrections"] == 0  # value kept as extracted
    with db.connect() as c:
        import json
        stored = json.loads(c.execute("SELECT line_items_json FROM visits").fetchone()[0])
    assert stored[1]["description"] == "Engine"

    # Same trailer again -> duplicate error blocks check-in unless overridden
    scan2 = client.post("/api/scan", files=[("files", _png())]).json()
    assert scan2["possible_duplicates"]
    body = {"scan_id": scan2["scan_id"], "fields": fields, "reviewed_by": "clerk"}
    assert client.post("/api/visits", json=body).status_code == 409
    body["override_reason"] = "Previous visit never checked out"
    assert client.post("/api/visits", json=body).status_code == 200


def test_poor_image_blocks_ready(client, monkeypatch):
    from app.extractor import MockExtractor
    from app.mock_data import _f, sample_extraction

    clean = sample_extraction(**{"trailer.seal_number": _f("SL-1")})
    monkeypatch.setattr(MockExtractor, "extract", lambda self, *a: (clean, []))
    assert client.post("/api/scan", files=[("files", _png())]).json()["ready_for_check_in"]
    monkeypatch.setattr(MockExtractor, "extract", lambda self, *a: (clean, ["image appears blurry"]))
    assert not client.post("/api/scan", files=[("files", _png())]).json()["ready_for_check_in"]


def test_rejects_unsupported_file(client):
    r = client.post("/api/scan", files=[("files", ("notes.txt", b"hi", "text/plain"))])
    assert r.status_code == 415
