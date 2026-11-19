"""Compact answer format for the vision model.

Generation speed is the bottleneck on CPU (~15 tokens/s), and the full
DocumentExtraction asks for {value, confidence, note} on ~70 fields even
when they are empty: ~2,400 output tokens per page. The compact schema asks
only for the values (null when absent) and has the model list the few
fields it found hard to read. The result
is converted back to DocumentExtraction, so nothing downstream changes.

The compact models are generated from DocumentExtraction, so adding a field
there adds it here automatically.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional, get_args, get_origin

from pydantic import BaseModel, Field, create_model

from .schemas import DocumentExtraction, DocumentType, Field_

# Names the model sees, where they differ from ours. Small models lean on key names: with
# 'ship_from' it filled the shipper from the letterhead, and with 'weight_lbs' it skipped a
# '5000 kg' gross weight. Document wording works better.
COMPACT_NAMES = {"ship_from": "shipper", "ship_to": "consignee", "weight_lbs": "weight"}
# Duplicates of party fields; asking twice cost tokens and invited disagreement.
EXCLUDED_PATHS = {"shipment.shipper_name", "shipment.consignee_name",
                  "line_items"}  # read by a separate, table-only request (app/table.py)

READ_CONFIDENCE = 0.95       # value read, not flagged by the model
UNCERTAIN_CONFIDENCE = 0.5   # model listed it in uncertain_fields (below the 0.80 review threshold)


def _compact_model(model: type[BaseModel], path: str = "") -> type[BaseModel]:
    """Every key is required (value may be null).

    Letting the model omit keys was faster but made it stop early: on an ocean
    BOL it skipped the shipper, carrier, consignee and table entirely
    (175 tokens). Requiring each key makes it consider every field.
    """
    fields: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        p = f"{path}.{name}" if path else name
        if p in EXCLUDED_PATHS:
            continue
        key = COMPACT_NAMES.get(name, name)
        ann = info.annotation
        desc = info.description
        if ann is Field_:
            fields[key] = (Optional[str], Field(..., description=desc))
        elif get_origin(ann) is list and isinstance(get_args(ann)[0], type) and issubclass(get_args(ann)[0], BaseModel):
            fields[key] = (list[_compact_model(get_args(ann)[0], p)], Field(..., description=desc))
        elif isinstance(ann, type) and issubclass(ann, BaseModel):
            fields[key] = (_compact_model(ann, p), Field(..., description=desc))
        else:
            continue  # document_type etc. are added explicitly below
    return create_model(f"Compact{model.__name__}", **fields)


@lru_cache
def compact_schema_model() -> type[BaseModel]:
    base = _compact_model(DocumentExtraction)
    return create_model(
        "CompactExtraction",
        __base__=base,
        document_type=(DocumentType, ...),
        image_quality_issues=(list[str], Field(default_factory=list,
                              description="Readability problems (blur, glare, cut off, handwriting). Empty if none.")),
        uncertain_fields=(list[str], Field(default_factory=list, description=(
            "Dotted paths of values that are hard to read, handwritten, partly cut off or guessed, "
            "e.g. 'trailer.seal_number' or 'line_items.0.weight_lbs'. Empty if every value is clearly printed."))),
    )


def _expand(model: type[BaseModel], data: dict | None, uncertain: set[str], path: str, cpath: str = "") -> dict:
    """path: our dotted path; cpath: the same path in the model's names (used by uncertain_fields)."""
    if data is not None and not isinstance(data, dict):
        raise ValueError(f"'{path or 'answer'}' should be an object, got {type(data).__name__}")
    data = data or {}
    out: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        p = f"{path}.{name}" if path else name
        key = COMPACT_NAMES.get(name, name)
        cp = f"{cpath}.{key}" if cpath else key
        ann = info.annotation
        if ann is Field_:
            v = data.get(key)
            v = str(v).strip() if v not in (None, "") else None
            if v is None:
                out[name] = {"value": None, "confidence": 1.0, "note": None}
            elif cp in uncertain or p in uncertain:
                out[name] = {"value": v, "confidence": UNCERTAIN_CONFIDENCE, "note": "model marked this as hard to read"}
            else:
                out[name] = {"value": v, "confidence": READ_CONFIDENCE, "note": None}
        elif get_origin(ann) is list and isinstance(get_args(ann)[0], type) and issubclass(get_args(ann)[0], BaseModel):
            item_model = get_args(ann)[0]
            out[name] = [_expand(item_model, item, uncertain, f"{p}.{i}", f"{cp}.{i}")
                         for i, item in enumerate(data.get(key) or [])]
        elif isinstance(ann, type) and issubclass(ann, BaseModel):
            out[name] = _expand(ann, data.get(key), uncertain, p, cp)
    return out


def expand_compact(compact: dict) -> DocumentExtraction:
    """Compact model answer (already parsed JSON) -> full DocumentExtraction."""
    uncertain = {u.strip() for u in compact.get("uncertain_fields") or [] if isinstance(u, str)}
    full = _expand(DocumentExtraction, compact, uncertain, "")
    full["document_type"] = compact.get("document_type") or DocumentType.OTHER.value
    # No self-reported score in the compact format; a recognized type counts as confident.
    full["document_type_confidence"] = 0.6 if full["document_type"] == DocumentType.OTHER.value else 0.9
    full["image_quality_issues"] = [i for i in compact.get("image_quality_issues") or [] if isinstance(i, str)]
    return DocumentExtraction.model_validate(full)
