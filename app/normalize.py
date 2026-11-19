"""Deterministic clean-up of model output before mapping.

Small local models follow formatting instructions loosely (dates left as
printed, a field's label copied as its value, the string "null"). Fixing
these in code is cheaper and more reliable than more prompt rules.
"""

from __future__ import annotations

import re
from datetime import date

from .schemas import DocumentExtraction, Field_

NULL_STRINGS = {"null", "none", "n/a", "na", "-", "--", "nil", "blank", "empty", "not provided", "not present"}

DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b|\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b")
TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?", re.IGNORECASE)


def _iter_fields(model, path=""):
    for name in type(model).model_fields:
        value = getattr(model, name)
        p = f"{path}.{name}" if path else name
        if isinstance(value, Field_):
            yield p, value
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if hasattr(type(item), "model_fields"):
                    yield from _iter_fields(item, f"{p}.{i}")
        elif hasattr(type(value), "model_fields"):
            yield from _iter_fields(value, p)


def looks_like_label(value: str) -> bool:
    """'PO/REFERENCE #', 'Name:', 'SHIPPER SIGNATURE' copied from an empty box."""
    v = value.strip()
    if re.search(r"\d", v):
        return False
    if v.endswith((":", "#")):
        return True
    return bool(re.fullmatch(r"[A-Z /&#().-]+", v)) and any(
        w in v for w in ("NAME", "SIGNATURE", "REFERENCE", "NUMBER", "ADDRESS", "PHONE", "EMAIL", "INSTRUCTIONS"))


def parse_date(text: str) -> str | None:
    m = DATE_RE.search(text or "")
    if not m:
        return None
    try:
        if m.group(1):
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        else:  # US documents: month first
            mo, d, y = int(m.group(4)), int(m.group(5)), int(m.group(6))
            if y < 100:
                y += 2000
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_window(text: str) -> str | None:
    """'12pm - 5pm' / '12:00-17:00' / '8:30 AM to 2 PM' -> 'HH:MM-HH:MM'."""
    text = DATE_RE.sub(" ", text or "")
    times = []
    for h, mnt, ampm in TIME_RE.findall(text):
        h, mnt = int(h), int(mnt or 0)
        ap = ampm.lower().replace(".", "")
        if not ap and not mnt and h > 24:
            continue
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if h < 24 and mnt < 60:
            times.append((h, mnt, ap))
    if len(times) < 2:
        return None
    (h1, m1, a1), (h2, m2, a2) = times[:2]
    if not a1 and a2 == "pm" and h1 + 12 <= h2 and h1 < 12:  # '1-5pm' -> 13:00-17:00
        h1 += 12
    return f"{h1:02d}:{m1:02d}-{h2:02d}:{m2:02d}"


def _number(text: str) -> str | None:
    m = re.search(r"\d[\d,]*(\.\d+)?", text or "")
    return m.group(0).replace(",", "") if m else None


def normalize_extraction(ext: DocumentExtraction) -> DocumentExtraction:
    ext = ext.model_copy(deep=True)
    ext.image_quality_issues = [i.strip() for i in ext.image_quality_issues
                                if i and i.strip().lower() not in NULL_STRINGS]
    fields = dict(_iter_fields(ext))

    for path, f in fields.items():
        if f.value is None:
            continue
        v = re.sub(r"\s*\n\s*", ", ", f.value.strip())  # multi-line addresses -> one line
        v = re.sub(r"\s{2,}", " ", v).strip(" ,")
        if v.lower() in NULL_STRINGS or looks_like_label(v):
            f.value, f.confidence, f.note = None, 1.0, None
            continue
        f.value = v

    # Issuer that only repeats the carrier name (no letterhead address) adds nothing.
    iss = ext.issuer
    if (iss.company.value and ext.carrier.name.value
            and iss.company.value.strip().lower() == ext.carrier.name.value.strip().lower()
            and not any(getattr(iss, k).value for k in ("street", "city", "postal_code", "phone", "email"))):
        iss.company.value, iss.company.confidence, iss.company.note = None, 1.0, None

    # The Carmack liability note (49 U.S.C. 14706) is preprinted on most BOLs and often sits
    # right under Special Instructions; it is never an instruction.
    si = ext.shipment.special_instructions
    if si.value and re.search(r"14706|liability\s+limitation", si.value, re.IGNORECASE):
        si.value, si.confidence, si.note = None, 1.0, None

    # Dates and pick-up windows: models often leave them as printed or merge them.
    sd, pw = ext.shipment.ship_date, ext.shipment.pickup_window
    d = parse_date(sd.value or "") or parse_date(pw.value or "")
    w = parse_window(pw.value or "") or parse_window(sd.value or "")
    if d and d != sd.value:
        if sd.value is None:
            sd.confidence = pw.confidence
        sd.note = f"normalized from '{sd.value or pw.value}'"
        sd.value = d
    if w and w != pw.value:
        if pw.value is None:
            pw.confidence = sd.confidence
        pw.note = f"normalized from '{pw.value or sd.value}'"
        pw.value = w
    ed = ext.shipment.estimated_delivery_date
    if ed.value and (d2 := parse_date(ed.value)) and d2 != ed.value:
        ed.note, ed.value = f"normalized from '{ed.value}'", d2

    for path, f in fields.items():
        if f.value and path.endswith("weight_lbs"):
            # The model copies weights with their unit; convert here, where the arithmetic is exact,
            # and keep the original in the note so the clerk can see it was converted.
            n = _number(f.value)
            if n and re.search(r"\bkgs?\b|kilo", f.value, re.IGNORECASE):
                f.note = f"converted from {f.value}"
                f.value = f"{float(n) * 2.20462:.0f}"
            elif n and re.search(r"\b(t|tonnes?|mt)\b", f.value, re.IGNORECASE):
                f.note = f"converted from {f.value}"
                f.value = f"{float(n) * 2204.62:.0f}"
            elif n:
                f.value = n
        elif f.value and (path.endswith("piece_count") or path.endswith(".quantity") or path.endswith(".pieces")):
            n = _number(f.value)
            if n:
                f.value = n
        if f.value and (path.endswith("hazmat") or path.endswith("_signed")):
            low = f.value.lower()
            f.value = "yes" if low in ("yes", "y", "x", "true") else "no" if low in ("no", "n", "false") else f.value
        if f.value and (path.endswith("scac") or path.endswith("_state")):
            f.value = f.value.upper()
    return ext
