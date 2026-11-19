"""Freight-table reading as a separate, focused model request.

Asked to fill ~70 fields and map table columns at the same time, the small
local model kept putting cells in the wrong field (PIECES into quantity) or
dropping them. Here it only copies the table as printed: the header row, then
each row's cells in order. Mapping headers to fields is done in code, with
fixed rules, so 'QTY' vs 'PIECES' is never the model's decision.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

TABLE_INSTRUCTION = """Now look only at the freight / commodity table (the grid with column headers such as \
QTY, PKG, TYPE, PIECES, DESCRIPTION, CLASS, NMFC, DIMS, WEIGHT, MARK).
- headers: the column header texts exactly as printed, left to right.
- rows: one list per table row, top to bottom, with one cell per header in the same order. Copy each cell \
exactly as printed; use null for an empty cell. A cell may wrap onto two lines (e.g. '120790-' above '102' \
is '120790-102'). Skip rows that are completely empty.
If the document has no such table, return empty lists."""


class TableAnswer(BaseModel):
    headers: list[str] = Field(description="Column headers as printed, left to right")
    rows: list[list[Optional[str]]] = Field(description="One list of cells per row, aligned with headers")


# Checked in order; first match wins. Pieces before package type, so 'PKGS'/'PIECES' aren't read as 'PKG'.
HEADER_RULES: list[tuple[str, str]] = [
    (r"\bHM\b|HAZ", "hazmat"),
    (r"PIECE|\bPCS?\b|\bPKGS\b|NO\.? ?OF ?(PKGS|PACKAGES)", "pieces"),
    (r"\bQTY\b|QUANTITY|HANDLING|\bH/?U\b", "quantity"),
    (r"\bPKG\b|PACKAGE|PACKAGING|\bTYPE\b|\bUNIT TYPE\b", "package_type"),
    (r"DESC|COMMODITY|ARTICLES", "description"),
    (r"NMFC", "nmfc_number"),
    (r"CLASS", "freight_class"),
    (r"DIM|SIZE|L ?X ?W", "dimensions"),
    (r"WEIGHT|\bWT\b|\bLBS?\b|\bKGS?\b|GROSS", "weight_lbs"),
    (r"MARK", "marks"),
]


def header_to_field(header: str) -> str | None:
    h = (header or "").upper()
    for pattern, field in HEADER_RULES:
        if re.search(pattern, h):
            return field
    return None


def table_to_line_items(answer: dict) -> list[dict[str, Optional[str]]]:
    """Table answer -> line items keyed by our column names (unknown columns are dropped)."""
    headers = [h if isinstance(h, str) else "" for h in answer.get("headers") or []]
    fields = [header_to_field(h) for h in headers]
    items = []
    for row in answer.get("rows") or []:
        if not isinstance(row, list):
            continue
        item: dict[str, Optional[str]] = {}
        for field, cell in zip(fields, row):
            if field and cell not in (None, "") and field not in item:
                item[field] = str(cell).strip()
        if item:
            items.append(item)
    return items
