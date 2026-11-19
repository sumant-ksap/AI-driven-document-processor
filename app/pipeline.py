"""Mapping -> validation -> duplicate detection.

Pure functions over a sqlite connection so they are easy to test and to
re-run on clerk-corrected values at confirmation time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .schemas import LINE_ITEM_COLUMNS, DocumentExtraction, Flag, Severity, YmsField, YmsLineItem

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def load_config(name: str) -> dict:
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8-sig"))


def norm_id(value: str | None) -> str:
    """Identifier comparison key: 'rdwy 53-001' == 'RDWY53001'."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _get_path(obj, path: str):
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


# ---------------------------------------------------------------- mapping


def _join_parts(ext, paths: list[str]):
    """Combine e.g. street/city/state/postal_code/country into one display value.

    'UT' and '84526' are joined with a space ('Helper, UT 84526'), everything else with ', '.
    Confidence is the weakest part's, so one unclear part puts the whole address up for review.
    """
    parts = []
    for path in paths:
        f = _get_path(ext, path)
        if f.value not in (None, ""):
            parts.append((path.rsplit(".", 1)[-1], f.value.strip(), f.confidence, f.note))
    if not parts:
        return None
    text = ""
    for i, (name, value, _, _) in enumerate(parts):
        sep = "" if i == 0 else (" " if name == "postal_code" and parts[i - 1][0] == "state" else ", ")
        text += sep + value
    notes = "; ".join(n for *_, n in parts if n)
    return text, min(p[2] for p in parts), " + ".join(paths), notes or None


def map_to_yms(extractions: list[DocumentExtraction], mappings: dict, rules: dict) -> tuple[list[YmsField], list[Flag]]:
    """Merge one or more documents into a single set of YMS fields.

    Highest-confidence non-null value wins. Confident disagreement between
    documents (e.g. BOL and IAC show different trailer numbers) is flagged.
    """
    threshold = rules["review_confidence_threshold"]
    fields: list[YmsField] = []
    flags: list[Flag] = []

    for spec in mappings["fields"]:
        candidates = []  # (value, confidence, source, note)
        for ext in extractions:
            if "join" in spec:
                c = _join_parts(ext, spec["join"])
                if c:
                    candidates.append(c)
                continue
            # Sources are fallbacks: per document, take the first one with a value. Only values from
            # different documents are compared (e.g. company vs contact name is not a conflict).
            for src in spec["sources"]:
                f = _get_path(ext, src)
                if f.value not in (None, ""):
                    candidates.append((f.value.strip(), f.confidence, src, f.note))
                    break

        group = spec.get("group", "Other")
        if not candidates:
            fields.append(YmsField(key=spec["key"], label=spec["label"], group=group, value=None, confidence=1.0))
            continue

        candidates.sort(key=lambda c: c[1], reverse=True)
        value, conf, src, note = candidates[0]
        needs_review = conf < threshold
        if needs_review:
            flags.append(Flag(field=spec["key"], severity=Severity.WARNING, code="low_confidence",
                              message=f"{spec['label']}: uncertain read ({conf:.0%})" + (f" - {note}" if note else "")))

        distinct = {norm_id(c[0]) for c in candidates if c[1] >= threshold}
        if len(distinct) > 1:
            needs_review = True
            shown = ", ".join(sorted({c[0] for c in candidates}))
            flags.append(Flag(field=spec["key"], severity=Severity.WARNING, code="document_conflict",
                              message=f"{spec['label']}: documents disagree ({shown})"))

        fields.append(YmsField(key=spec["key"], label=spec["label"], group=group, value=value,
                               confidence=conf, source=src, needs_review=needs_review))
    return fields, flags


def merge_line_items(extractions: list[DocumentExtraction], rules: dict) -> list[YmsLineItem]:
    """Take the freight table from the document that has the fullest one.

    Rows are not merged across documents: a BOL and a packing list often
    group the same freight differently, so mixing rows would invent totals.
    """
    threshold = rules["review_confidence_threshold"]
    best: list = []
    best_score = (-1, -1.0)
    for ext in extractions:
        if not ext.line_items:
            continue
        confs = [getattr(li, c).confidence for li in ext.line_items for c in LINE_ITEM_COLUMNS]
        score = (len(ext.line_items), sum(confs) / len(confs))
        if score > best_score:
            best, best_score = ext.line_items, score

    items = []
    for li in best:
        cells = {c: getattr(li, c) for c in LINE_ITEM_COLUMNS}
        values = {c: (f.value.strip() if f.value else None) for c, f in cells.items()}
        if not any(values.values()):
            continue  # blank table row
        min_conf = min(f.confidence for f in cells.values() if f.value) if any(values.values()) else 1.0
        items.append(YmsLineItem(values=values, min_confidence=min_conf, needs_review=min_conf < threshold))
    return items


def _num(v: str | None) -> float | None:
    try:
        return float(re.sub(r"[^0-9.]", "", v)) if v and re.search(r"\d", v) else None
    except ValueError:
        return None


def validate_line_items(items: list[YmsLineItem], fields: list[YmsField], rules: dict) -> list[Flag]:
    flags: list[Flag] = []
    by_key = {f.key: f for f in fields}
    for i, item in enumerate(items, 1):
        if item.needs_review:
            flags.append(Flag(field=f"line_items.{i}", severity=Severity.WARNING, code="low_confidence",
                              message=f"Line {i}: uncertain read ({item.min_confidence:.0%})"))
        for col, fmt in rules.get("line_item_formats", {}).items():
            v = item.values.get(col)
            if v and not re.match(fmt["pattern"], v.strip()):
                item.needs_review = True
                flags.append(Flag(field=f"line_items.{i}", severity=Severity.WARNING, code="bad_format",
                                  message=f"Line {i} {col.replace('_', ' ')}: '{v}' - {fmt['message']}"))

    # Totals printed on the document should agree with the table.
    for total_key, col, label in (("weight_lbs", "weight_lbs", "weight"), ("piece_count", "pieces", "pieces")):
        line_vals = [_num(it.values.get(col)) for it in items]
        if not items or None in line_vals or total_key not in by_key:
            continue
        line_sum = sum(line_vals)
        total = _num(by_key[total_key].value)
        if total is None:
            # Fill a missing total from the table, but make the clerk look at it.
            f = by_key[total_key]
            f.value = f"{line_sum:g}"
            f.confidence, f.source, f.needs_review = 0.9, "line_items (sum)", False
            flags.append(Flag(field=total_key, severity=Severity.INFO, code="total_from_lines",
                              message=f"{f.label} calculated from line items: {f.value}"))
        elif abs(total - line_sum) > 0.5:
            by_key[total_key].needs_review = True
            flags.append(Flag(field=total_key, severity=Severity.WARNING, code="total_mismatch",
                              message=f"Total {label} {total:g} does not match line items ({line_sum:g})"))
    return flags


# ---------------------------------------------------------------- validation


def validate(fields: list[YmsField], conn, rules: dict) -> list[Flag]:
    by_key = {f.key: f for f in fields}
    val = lambda k: by_key[k].value if k in by_key else None  # noqa: E731
    flags: list[Flag] = []

    def review(key: str, flag: Flag):
        flags.append(flag)
        if key in by_key and flag.severity != Severity.INFO:
            by_key[key].needs_review = True

    for key in rules["required_fields"]:
        if not val(key):
            label = by_key[key].label if key in by_key else key
            review(key, Flag(field=key, severity=Severity.ERROR, code="missing_required",
                             message=f"{label} is required but was not found"))

    for key in rules.get("recommended_fields", []):
        if key in by_key and not val(key):
            review(key, Flag(field=key, severity=Severity.WARNING, code="missing_recommended",
                             message=f"{by_key[key].label} not found on the documents; enter it if available"))

    for key, fmt in rules["formats"].items():
        v = val(key)
        if v and not re.match(fmt["pattern"], v.strip(), flags=re.IGNORECASE if key == "hazmat" else 0):
            review(key, Flag(field=key, severity=Severity.WARNING, code="bad_format",
                             message=f"{by_key[key].label}: '{v}' - {fmt['message']}"))

    for key in rules.get("address_fields", []):
        v = val(key)
        if v and not re.search(r"\b\d{5}(-\d{4})?\b|\b[A-Z]\d[A-Z] ?\d[A-Z]\d\b", v, re.IGNORECASE):
            review(key, Flag(field=key, severity=Severity.WARNING, code="incomplete_address",
                             message=f"{by_key[key].group} address has no ZIP/postal code - check it against the document"))

    flags += _check_master_data(by_key, conn, rules, review)
    return flags


def _check_master_data(by_key, conn, rules, review) -> list[Flag]:
    extra: list[Flag] = []
    fuzzy = rules["fuzzy_match_threshold"]
    v = lambda k: by_key[k].value if k in by_key else None  # noqa: E731

    # Carrier
    scac = (v("carrier_scac") or "").upper().strip()
    carrier = None
    if scac:
        carrier = conn.execute("SELECT * FROM carriers WHERE scac = ?", (scac,)).fetchone()
        if carrier is None:
            all_carriers = conn.execute("SELECT * FROM carriers").fetchall()
            guess = max(all_carriers, key=lambda c: similarity(c["name"], v("carrier_name")), default=None)
            hint = ""
            if guess is not None and similarity(guess["name"], v("carrier_name")) >= fuzzy:
                hint = f" Carrier name matches {guess['name']} ({guess['scac']})."
            review("carrier_scac", Flag(field="carrier_scac", severity=Severity.WARNING, code="unknown_carrier",
                                        message=f"SCAC {scac} is not in carrier master data.{hint}"))
        else:
            by_key["carrier_scac"].matched_record = f"carrier:{scac}"
            if not carrier["active"]:
                review("carrier_scac", Flag(field="carrier_scac", severity=Severity.ERROR, code="inactive_carrier",
                                            message=f"Carrier {carrier['name']} ({scac}) is inactive"))
            if v("carrier_name") and similarity(carrier["name"], v("carrier_name")) < 0.6:
                review("carrier_name", Flag(field="carrier_name", severity=Severity.WARNING, code="carrier_mismatch",
                                            message=f"Document says '{v('carrier_name')}' but SCAC {scac} is {carrier['name']}"))
            if v("usdot_number") and carrier["usdot_number"] and norm_id(v("usdot_number")) != norm_id(carrier["usdot_number"]):
                review("usdot_number", Flag(field="usdot_number", severity=Severity.WARNING, code="usdot_mismatch",
                                            message=f"USDOT {v('usdot_number')} does not match {carrier['usdot_number']} on file for {scac}"))
    elif v("carrier_name"):
        # Many BOLs print only the carrier name. Suggest the SCAC, don't fill it silently.
        all_carriers = conn.execute("SELECT * FROM carriers").fetchall()
        guess = max(all_carriers, key=lambda c: similarity(c["name"], v("carrier_name")), default=None)
        if guess is not None and similarity(guess["name"], v("carrier_name")) >= fuzzy:
            by_key["carrier_name"].matched_record = f"carrier:{guess['scac']}"
            extra.append(Flag(field="carrier_scac", severity=Severity.INFO, code="carrier_name_matched",
                              message=f"Carrier name matches {guess['name']} - SCAC {guess['scac']}"))
        else:
            review("carrier_name", Flag(field="carrier_name", severity=Severity.WARNING, code="unknown_carrier",
                                        message=f"Carrier '{v('carrier_name')}' is not in carrier master data"))

    # Driver
    lic = norm_id(v("driver_license"))
    driver = None
    if lic:
        for d in conn.execute("SELECT * FROM drivers").fetchall():
            if norm_id(d["license_number"]) == lic:
                driver = d
                break
    if driver is None and v("driver_name"):
        pool = conn.execute("SELECT * FROM drivers WHERE carrier_scac = ? OR ? = ''", (scac, scac)).fetchall()
        best = max(pool, key=lambda d: similarity(d["name"], v("driver_name")), default=None)
        if best is not None and similarity(best["name"], v("driver_name")) >= fuzzy:
            driver = best
    if driver is not None:
        by_key["driver_name"].matched_record = f"driver:{driver['id']}"
        extra.append(Flag(field="driver_name", severity=Severity.INFO, code="driver_matched",
                          message=f"Matched driver on file: {driver['name']} ({driver['license_state']} {driver['license_number']})"))
    elif v("driver_name"):
        extra.append(Flag(field="driver_name", severity=Severity.INFO, code="new_driver",
                          message="Driver not on file; a new driver record will be created"))

    # Trailer
    trl = norm_id(v("trailer_number"))
    if trl:
        trailer = next((t for t in conn.execute("SELECT * FROM trailers").fetchall()
                        if norm_id(t["trailer_number"]) == trl), None)
        if trailer is not None:
            by_key["trailer_number"].matched_record = f"trailer:{trailer['trailer_number']}"
            if scac and trailer["carrier_scac"] and trailer["carrier_scac"] != scac:
                review("trailer_number", Flag(field="trailer_number", severity=Severity.WARNING, code="trailer_carrier_mismatch",
                                              message=f"Trailer {trailer['trailer_number']} is registered to {trailer['carrier_scac']}, not {scac}"))

    # Appointment
    bol = norm_id(v("bol_number"))
    if bol:
        appt = next((a for a in conn.execute("SELECT * FROM appointments WHERE status = 'scheduled'").fetchall()
                     if norm_id(a["bol_number"]) == bol), None)
        if appt is None:
            review("bol_number", Flag(field="bol_number", severity=Severity.WARNING, code="no_appointment",
                                      message=f"No scheduled appointment found for BOL {v('bol_number')}"))
        else:
            by_key["bol_number"].matched_record = f"appointment:{appt['id']}"
            if scac and appt["carrier_scac"] and appt["carrier_scac"] != scac:
                review("carrier_scac", Flag(field="carrier_scac", severity=Severity.WARNING, code="appointment_carrier_mismatch",
                                            message=f"Appointment for this BOL is booked with {appt['carrier_scac']}"))
    return extra


# ---------------------------------------------------------------- duplicates


def find_duplicates(fields: list[YmsField], conn, rules: dict) -> tuple[list[dict], list[Flag]]:
    by_key = {f.key: f.value for f in fields}
    since = (datetime.now(timezone.utc) - timedelta(hours=rules["duplicate_window_hours"])).isoformat()
    dupes: dict[int, dict] = {}
    flags: list[Flag] = []

    def hit(visit, reason):
        entry = dupes.setdefault(visit["id"], {"visit_id": visit["id"], "created_at": visit["created_at"],
                                               "status": visit["status"], "trailer_number": visit["trailer_number"],
                                               "bol_number": visit["bol_number"], "reasons": []})
        entry["reasons"].append(reason)

    visits = conn.execute("SELECT * FROM visits WHERE status = 'checked_in' OR created_at >= ?", (since,)).fetchall()

    trl = norm_id(by_key.get("trailer_number"))
    bol = norm_id(by_key.get("bol_number"))
    seal = norm_id(by_key.get("seal_number"))
    tractor = norm_id(by_key.get("tractor_number"))

    for vis in visits:
        on_site = vis["status"] == "checked_in"
        recent = vis["created_at"] >= since
        if trl and norm_id(vis["trailer_number"]) == trl and on_site:
            hit(vis, "trailer already checked in (no check-out recorded)")
        if bol and norm_id(vis["bol_number"]) == bol and recent:
            hit(vis, "same BOL checked in recently")
        if seal and norm_id(vis["seal_number"]) == seal and recent:
            hit(vis, "same seal number checked in recently")
        if tractor and norm_id(vis["tractor_number"]) == tractor and on_site:
            hit(vis, "tractor already on site")

    for d in dupes.values():
        flags.append(Flag(field=None, severity=Severity.ERROR, code="possible_duplicate",
                          message=f"Possible duplicate of visit #{d['visit_id']} ({d['created_at'][:16]}): " + "; ".join(d["reasons"])))
    return list(dupes.values()), flags


def quality_flags(extractions: list[DocumentExtraction], local_issues: list[list[str]], files: list[str]) -> list[Flag]:
    flags = []
    for ext, local, name in zip(extractions, local_issues, files):
        issues = sorted(set(local) | set(ext.image_quality_issues))
        if issues:
            flags.append(Flag(severity=Severity.WARNING, code="image_quality",
                              message=f"{name}: {', '.join(issues)} - consider rescanning"))
        if ext.document_type_confidence < 0.7:
            flags.append(Flag(severity=Severity.INFO, code="unclear_document_type",
                              message=f"{name}: document type unclear (best guess {ext.document_type.value})"))
    return flags
