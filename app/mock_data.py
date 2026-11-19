"""Canned extraction used by MockExtractor and the tests."""

from .schemas import DocumentExtraction

NULL = {"value": None, "confidence": 1.0, "note": None}


def _f(value, confidence=0.97, note=None):
    return {"value": value, "confidence": confidence, "note": note}


def _party(company=None, contact=None, address=None, phone=None, email=None):
    """address: 'street, city, ST ZIP, country' (any trailing parts may be omitted)."""
    street = city = state = postal = country = None
    if address:
        bits = [b.strip() for b in address.split(",")]
        street, city = bits[0], bits[1] if len(bits) > 1 else None
        if len(bits) > 2:
            state, _, postal = bits[2].partition(" ")
        country = bits[3] if len(bits) > 3 else None
    vals = dict(company=company, contact_name=contact, street=street, city=city, state=state,
                postal_code=postal or None, country=country, phone=phone, email=email)
    return {k: (_f(v) if v else dict(NULL)) for k, v in vals.items()}


def _line(qty, pkg, pcs, desc, cls, nmfc, dims, wt):
    return {"quantity": _f(qty), "package_type": _f(pkg), "pieces": _f(pcs), "description": _f(desc),
            "freight_class": _f(cls), "nmfc_number": _f(nmfc), "dimensions": _f(dims), "weight_lbs": _f(wt),
            "hazmat": dict(NULL)}


def sample_extraction(**overrides) -> DocumentExtraction:
    data = {
        "document_type": "bill_of_lading",
        "document_type_confidence": 0.98,
        "image_quality_issues": [],
        "carrier": {"name": _f("Roadway Freight Lines"), "scac": _f("RDWY"),
                    "usdot_number": _f("1234567"), "mc_number": _f("MC-123456")},
        "ship_from": _party("Acme Manufacturing", "Clark Smith", "100 Industrial Rd, Dallas, TX 75201, United States",
                            "(1) (800) 716-7608"),
        "ship_to": _party("Central DC #4", "Carrie Diaz", "55 Logistics Pkwy, Memphis, TN 38118, United States",
                          "(1) (800) 716-7609"),
        "bill_to": _party("Billing Inc.", None, "123 Channel Dr., Hope, KS 67451"),
        "customs_agent": _party(),
        "shipment": {
            "bol_number": _f("BOL-778812"),
            "pro_number": _f("PRO-55120"),
            "po_numbers": _f("PO-4471, PO-4472"),
            "shipper_name": _f("Acme Manufacturing"),
            "consignee_name": _f("Central DC #4"),
            "ship_date": _f("2026-11-16"),
            "pickup_window": _f("12:00-17:00"),
            "piece_count": _f("24"),
            "weight_lbs": _f("18250"),
            "hazmat": _f("no"),
            "iac_number": dict(NULL),
            "special_instructions": dict(NULL),
        },
        "line_items": [
            _line("20", "Pallet", "20", "Auto parts", "85", "120790-102", "4'x4'x4'", "15000"),
            _line("4", "Crate", "4", "Engine", "85", "120790-102", "4'x4'x4'", "3250"),
        ],
        "truck": {"tractor_number": _f("T-4412"), "license_plate": _f("ABC1234"), "plate_state": _f("TX")},
        "trailer": {
            "trailer_number": _f("RDWY53001"),
            "license_plate": _f("TRL1234"),
            "plate_state": _f("TX"),
            "seal_number": _f("SL-0098812", 0.55, "Handwritten; last digit could be 2 or 7"),
            "trailer_type": _f("dry van"),
        },
        "driver": {"name": _f("John Smith"), "license_number": dict(NULL), "license_state": dict(NULL),
                   "phone": _f("555-201-3344", 0.9)},
        "signatures": {"shipper_printed_name": dict(NULL), "shipper_signed": _f("no"),
                       "carrier_printed_name": dict(NULL), "carrier_signed": _f("no"),
                       "units_received": dict(NULL)},
    }
    for path, value in overrides.items():
        node = data
        *parents, leaf = path.split(".")
        for p in parents:
            node = node[int(p)] if isinstance(node, list) else node[p]
        node[leaf] = value
    return DocumentExtraction.model_validate(_fill_nulls(DocumentExtraction, data))


def _fill_nulls(model, data: dict) -> dict:
    """Give every schema field not set in the sample an empty value, so new fields don't break tests."""
    from typing import get_args, get_origin

    from .schemas import Field_
    for name, info in model.model_fields.items():
        ann = info.annotation
        if ann is Field_:
            data.setdefault(name, dict(NULL))
        elif get_origin(ann) is list and get_args(ann) and hasattr(get_args(ann)[0], "model_fields"):
            for item in data.setdefault(name, []):
                _fill_nulls(get_args(ann)[0], item)
        elif hasattr(ann, "model_fields"):
            _fill_nulls(ann, data.setdefault(name, {}))
    return data
