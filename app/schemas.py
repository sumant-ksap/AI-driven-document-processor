"""Data shapes shared across the pipeline.

`DocumentExtraction` is the exact JSON schema the vision model is constrained
to return (Ollama `format=`), so every field the model can fill lives here.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    BOL = "bill_of_lading"
    IAC = "iac_form"
    PACKING_LIST = "packing_list"
    DELIVERY_ORDER = "delivery_order"
    DRIVER_LICENSE = "driver_license"
    OTHER = "other_shipment_document"


class Field_(BaseModel):
    """One extracted value with the model's own certainty about it."""

    value: Optional[str] = Field(
        None, description="Value exactly as printed (normalized whitespace). null if not present or blank."
    )
    confidence: float = Field(
        description="0.0-1.0. Use <0.6 when the text is blurry, handwritten, partially cut off, or inferred."
    )
    note: Optional[str] = Field(
        None, description="Short reason when confidence is low or the value was inferred; otherwise null."
    )


class Party(BaseModel):
    """A company/person block: ship from, ship to, bill to, customs agent."""

    company: Field_
    contact_name: Field_
    street: Field_ = Field(description="Street address line(s), e.g. 1846 N Hwy 8")
    city: Field_
    state: Field_ = Field(description="State/province code, e.g. UT")
    postal_code: Field_ = Field(description="ZIP/postal code, e.g. 84526. Often printed after the country.")
    country: Field_ = Field(description="e.g. United States")
    phone: Field_
    email: Field_


class TruckInfo(BaseModel):
    tractor_number: Field_
    license_plate: Field_
    plate_state: Field_


class TrailerInfo(BaseModel):
    trailer_number: Field_
    license_plate: Field_
    plate_state: Field_
    seal_number: Field_
    trailer_type: Field_ = Field(description="e.g. dry van, reefer, flatbed, container")


class DriverInfo(BaseModel):
    name: Field_
    license_number: Field_
    license_state: Field_
    phone: Field_


class CarrierInfo(BaseModel):
    name: Field_ = Field(description="Carrier company name (the 'Carrier' box), not the document issuer in the letterhead")
    scac: Field_ = Field(description="2-4 letter Standard Carrier Alpha Code")
    usdot_number: Field_
    mc_number: Field_
    street: Field_ = Field(description="Carrier address street, e.g. 789 Oak St")
    city: Field_
    state: Field_
    postal_code: Field_
    country: Field_


class OceanInfo(BaseModel):
    """Ocean / intermodal bills of lading."""

    port_of_loading: Field_
    port_of_discharge: Field_
    vessel_name: Field_ = Field(description="e.g. MV Pacific Star")
    voyage_number: Field_ = Field(description="e.g. V123")
    container_number: Field_ = Field(description="e.g. MSCU1234567")


class LineItem(BaseModel):
    """One row of the commodity/freight table."""

    quantity: Field_ = Field(description="Handling-unit count (QTY column)")
    package_type: Field_ = Field(description="PKG or TYPE column, e.g. Pallet, Crate, PKG001")
    pieces: Field_ = Field(description="PCS column")
    description: Field_
    freight_class: Field_ = Field(description="NMFC freight class, e.g. 85")
    nmfc_number: Field_ = Field(description="NMFC item number, e.g. 120790-102")
    dimensions: Field_ = Field(description="DIMS as printed, e.g. 4'x4'x4'")
    weight_lbs: Field_ = Field(description="Row weight as printed, with its unit if not pounds (e.g. 300, 136 kg)")
    hazmat: Field_ = Field(description="'yes' or 'no' if the row has an HM marker; else null")
    marks: Field_ = Field(description="MARK / marks and numbers column")


class ShipmentInfo(BaseModel):
    bol_number: Field_
    pro_number: Field_
    po_numbers: Field_ = Field(description="PO/Reference numbers, comma-separated if several")
    shipper_name: Field_ = Field(description="Ship-from company name")
    consignee_name: Field_ = Field(description="Ship-to company name")
    # Copied as printed; app/normalize.py converts to YYYY-MM-DD and HH:MM-HH:MM.
    ship_date: Field_ = Field(description="Pick-up, ship or loading date exactly as printed, e.g. 1/1/23")
    estimated_delivery_date: Field_ = Field(description="Estimated delivery / discharge / arrival date exactly as printed")
    pickup_window: Field_ = Field(description="Pick-up time window exactly as printed, e.g. 12pm - 5pm")
    piece_count: Field_ = Field(description="Total pieces across all line items")
    weight_lbs: Field_ = Field(description="Total / gross weight as printed, with its unit if not pounds (e.g. 18250, 5000 kg)")
    height: Field_ = Field(description="Overall height with unit, e.g. 3 m")
    length: Field_ = Field(description="Overall length with unit, e.g. 10 m")
    width: Field_ = Field(description="Overall width with unit, e.g. 2.5 m")
    goods_description: Field_ = Field(description="Overall goods / commodity description, e.g. Electronics")
    freight_charges: Field_ = Field(description="Freight charges with currency, e.g. 5000 USD")
    service_level: Field_ = Field(description="Service / service level, e.g. Standard, Expedited")
    hazmat: Field_ = Field(description="'yes' or 'no' only if the document has a hazmat box or HM marking; otherwise null")
    iac_number: Field_ = Field(description="Indirect Air Carrier number, IAC forms only")
    special_instructions: Field_


class SignatureInfo(BaseModel):
    shipper_printed_name: Field_
    shipper_signed: Field_ = Field(description="'yes' if a shipper signature is present, 'no' if the box is blank")
    carrier_printed_name: Field_
    carrier_signed: Field_ = Field(description="'yes' if a carrier signature is present, 'no' if the box is blank")
    units_received: Field_ = Field(description="'# of units received' written by the carrier")


class DocumentExtraction(BaseModel):
    document_type: DocumentType
    document_type_confidence: float
    image_quality_issues: list[str] = Field(
        description="Problems that hurt readability: blur, glare, skew, cropped edges, low resolution, handwriting. Empty if none."
    )
    issuer: Party = Field(description="Company in the document header/letterhead that issued the BOL (may differ from shipper and carrier)")
    carrier: CarrierInfo
    ship_from: Party
    ship_to: Party
    bill_to: Party = Field(description="'Bill freight to' / third-party billing block")
    customs_agent: Party
    shipment: ShipmentInfo
    ocean: OceanInfo
    line_items: list[LineItem] = Field(description="Every row of the freight table, top to bottom. Empty if no table.")
    truck: TruckInfo
    trailer: TrailerInfo
    driver: DriverInfo
    signatures: SignatureInfo


# ---------- pipeline output (not sent to the model) ----------


class Severity(str, Enum):
    ERROR = "error"      # blocks check-in until resolved
    WARNING = "warning"  # needs user review
    INFO = "info"


class Flag(BaseModel):
    field: Optional[str] = None  # YMS field key, or None for document-level flags
    severity: Severity
    code: str
    message: str


class YmsField(BaseModel):
    key: str
    label: str
    group: str = "Other"
    value: Optional[str]
    confidence: float
    source: Optional[str] = None  # extraction path, e.g. "trailer.trailer_number"
    needs_review: bool = False
    matched_record: Optional[str] = None  # e.g. "carrier:ABCD"


LINE_ITEM_COLUMNS = ["quantity", "package_type", "pieces", "description", "freight_class",
                     "nmfc_number", "dimensions", "weight_lbs", "hazmat", "marks"]


class YmsLineItem(BaseModel):
    values: dict[str, Optional[str]]  # keyed by LINE_ITEM_COLUMNS
    min_confidence: float
    needs_review: bool = False


class ScanResult(BaseModel):
    scan_id: str
    files: list[str]
    extractor: str
    document_types: list[str]
    fields: list[YmsField]
    line_items: list[YmsLineItem] = []
    flags: list[Flag]
    possible_duplicates: list[dict]
    ready_for_check_in: bool
