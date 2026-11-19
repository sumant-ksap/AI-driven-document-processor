import pytest

from app.mock_data import NULL, _f, sample_extraction
from app.normalize import looks_like_label, normalize_extraction, parse_date, parse_window


@pytest.mark.parametrize("text, expected", [
    ("1/1/23", "2023-01-01"), ("12/31/2026", "2026-12-31"), ("2026-11-16", "2026-11-16"),
    ("1/1/23 - 12pm - 5pm", "2023-01-01"), ("13/40/23", None), ("no date", None),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("12pm - 5pm", "12:00-17:00"), ("1/1/23 - 12pm - 5pm", "12:00-17:00"), ("12:00-17:00", "12:00-17:00"),
    ("8:30 AM to 2 PM", "08:30-14:00"), ("1-5pm", "13:00-17:00"), ("12am-6am", "00:00-06:00"), ("noon", None),
])
def test_parse_window(text, expected):
    assert parse_window(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("PO/REFERENCE #", True), ("Name:", True), ("SHIPPER SIGNATURE", True),
    ("PO-4471", False), ("TRUCKING INC.", False), ("Auto Shop", False),
])
def test_looks_like_label(text, expected):
    assert looks_like_label(text) is expected


def test_normalize_matches_sample_bol_errors():
    """The misreads gemma4:e4b made on tests/fixtures/sample_bol.png."""
    ext = normalize_extraction(sample_extraction(**{
        "shipment.po_numbers": _f("PO/REFERENCE #", 1.0),
        "shipment.ship_date": _f("null", 1.0),
        "shipment.pickup_window": _f("1/1/23 - 12:00-17:00", 1.0),
        "ship_from.street": _f("1846 N Hwy 8\nSuite 2"),
        "line_items.0.weight_lbs": _f("300lbs"),
        "carrier.scac": _f("rdwy"),
        "signatures.carrier_signed": _f("No"),
    }))
    assert ext.shipment.po_numbers.value is None
    assert ext.shipment.ship_date.value == "2023-01-01"
    assert ext.shipment.pickup_window.value == "12:00-17:00"
    assert ext.ship_from.street.value == "1846 N Hwy 8, Suite 2"
    assert ext.line_items[0].weight_lbs.value == "300"
    assert ext.carrier.scac.value == "RDWY"
    assert ext.signatures.carrier_signed.value == "no"


def test_liability_boilerplate_dropped():
    note = "Note: Liability Limitation for loss or damage in this shipment may be applicable. See 49 U.S.C. 14706(c)(1)(A) and (B)."
    ext = normalize_extraction(sample_extraction(**{"shipment.special_instructions": _f(note)}))
    assert ext.shipment.special_instructions.value is None
    ext = normalize_extraction(sample_extraction(**{"shipment.special_instructions": _f("Call 30 min before arrival")}))
    assert ext.shipment.special_instructions.value == "Call 30 min before arrival"


def test_address_without_zip_flagged(tmp_path):
    from app import db
    from app.pipeline import load_config, map_to_yms, validate
    rules = load_config("validation_rules.json")
    ext = sample_extraction(**{"ship_from.postal_code": NULL})
    fields, _ = map_to_yms([ext], load_config("field_mappings.json"), rules)
    addr = {f.key: f.value for f in fields}
    assert addr["consignee_address"] == "55 Logistics Pkwy, Memphis, TN 38118, United States"
    assert addr["shipper_address"] == "100 Industrial Rd, Dallas, TX, United States"
    with db.connect(tmp_path / "t.db") as conn:
        flags = validate(fields, conn, rules)
    assert [f.field for f in flags if f.code == "incomplete_address"] == ["shipper_address"]


def test_kg_converted_with_note():
    ext = normalize_extraction(sample_extraction(**{"shipment.weight_lbs": _f("5000 kg"),
                                                    "line_items.0.weight_lbs": _f("136 KGS"),
                                                    "line_items.1.weight_lbs": _f("300lbs"),
                                                    "shipment.estimated_delivery_date": _f("6/30/2023")}))
    assert ext.shipment.weight_lbs.value == "11023" and ext.shipment.weight_lbs.note == "converted from 5000 kg"
    assert ext.line_items[0].weight_lbs.value == "300"
    assert ext.line_items[1].weight_lbs.value == "300" and ext.line_items[1].weight_lbs.note is None
    assert ext.shipment.estimated_delivery_date.value == "2023-06-30"
    assert NULL["value"] is None
