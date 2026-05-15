"""
Pydantic shapes for the canonical ``GET /api/v1/patients/{id}/chart`` endpoint.

These match the response shape in Phase 2 PRD §6.1 verbatim -- the brief's
Task 2 deliverable plus the extraction surplus (observations, scales,
ASAM-evidence summary, TJC coverage summary, completeness score).

The chart shape diverges enough from the per-endpoint schemas in
``app/api/schemas.py`` (patient name is nested ``{given, family}``,
observations carry a nested ``code: {system, code, display}``, timeline
entries flatten sections to ``dict[str, str]``) that mixing them would make
both unreadable. Kept here in their own module instead.

Section-level reuse: ``SectionRead``, ``ScaleRead``, ``ProvenanceRead`` come
in from ``app/api/schemas.py`` so the trinary status / provenance envelope
is identical between ``/intake`` and ``/chart``.
"""

import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.api.schemas import ProvenanceRead, ScaleRead, SectionRead


class ChartMeta(BaseModel):
    """Top-of-response metadata envelope.

    ``generated_at`` is the latest source-document ``authored_on`` for this
    chart -- i.e. "when the chart data was finalized in the source EMR", NOT
    wall-clock time at the request. This is intentional: a wall-clock value
    would change every request, defeating the ETag/If-None-Match contract
    (PRD §5.7). The chart is a deterministic function of the row store, so
    its response bytes -- and thus its ETag -- stay stable until new data
    lands.

    ``etag`` is left ``None`` here -- the ETag middleware (M4) sets the
    ``ETag`` HTTP header, which is the source of truth; the body field is a
    reviewer-friendly mirror that we cannot populate without recursing into
    the rendered bytes.

    ``audit_id`` / ``compliance_check_id`` are pre-allocated nullable slots
    for Phase 3 to fill once the LOC engine writes a typed audit + compliance
    check per ``/chart`` invocation.
    """

    generated_at: datetime
    extraction_version: str
    completeness_score: float
    etag: str | None = None
    audit_id: uuid.UUID | None = None
    compliance_check_id: uuid.UUID | None = None


class PatientName(BaseModel):
    """FHIR-style nested name shape: a list of given names + one family name."""

    given: list[str]
    family: str


class PatientIdentifier(BaseModel):
    """One identifier issued by an external system (e.g. SimplePractice)."""

    system: str
    value: str


class ChartPatient(BaseModel):
    """Patient demographics in the canonical /chart shape (PRD §6.1)."""

    id: uuid.UUID
    identifiers: list[PatientIdentifier]
    name: PatientName
    birth_date: date
    gender: str
    # Phase 1 stores name/dob/gender + SimplePractice id; address / phone /
    # preferred_language are not in the relational columns and stay null.
    # They can be filled from the stored Patient.fhir_json in a future pass.
    address: dict | None = None
    phone: str | None = None
    preferred_language: str | None = "en"


class LoincCoding(BaseModel):
    """A LOINC-coded ``CodeableConcept.coding`` entry (PRD §6.2)."""

    system: str = "http://loinc.org"
    code: str
    display: str


class Author(BaseModel):
    """Document author identity. NPI is null for the MVP -- no NPI registry."""

    name: str
    npi: str | None = None


class ChartIntake(BaseModel):
    """The BPS intake assessment in the canonical /chart shape.

    ``sections`` carries the trinary status + provenance from M5/M6.
    ``scales`` is the list of LOINC-coded scoring instruments extracted
    from this document (PHQ-9, GAD-7, ...), each with its own provenance.
    """

    document_id: uuid.UUID
    document_type: str
    encounter_date: datetime
    author: Author
    loinc_type: LoincCoding
    full_text: str
    sections: dict[str, SectionRead]
    scales: list[ScaleRead]


class TimelineNote(BaseModel):
    """One progress note in the canonical /chart timeline shape.

    ``sections`` is a flat ``label -> text`` dict here (not the
    status-bearing SectionRead) because the timeline summary is meant to be
    skim-readable; per-section trinary status lives on the intake response.
    """

    document_id: uuid.UUID
    date: datetime
    type: str = "progress_note"  # currently only progress notes; intake is in `intake`
    format: str  # SOAP | DAP | DSAP -- the format-specific structure label
    loinc_type: LoincCoding
    author: Author
    sections: dict[str, str]
    full_text: str


class ChartCoding(BaseModel):
    """A coded value with system + code + display."""

    system: str
    code: str
    display: str


class ChartObservation(BaseModel):
    """One observation in the canonical /chart shape, with mandatory provenance.

    Differs from ``ObservationRead`` (the /api/v1/observations shape):
    code is nested here, value-string is omitted (LOINC scales are
    quantity-valued), and provenance is required (PRD §5.5 / Success
    Metric M2 -- 100% of observations carry char-offset provenance).
    """

    id: uuid.UUID
    code: ChartCoding
    value_quantity: float | None = None
    value_string: str | None = None
    effective_date: date | None = None
    provenance: ProvenanceRead


class AsamSummary(BaseModel):
    """Per-dimension evidence counts.

    Phase 2 surfaces the evidence-collection status only; clinical severity
    classification (the dimension-level risk that drives LOC recommendation)
    is Phase 3's LOC reasoning engine. The counts here are inputs to that
    engine, not outputs of it -- shipping severity labels in Phase 2 would
    fabricate clinical meaning we have not earned.
    """

    dim_1: int = 0
    dim_2: int = 0
    dim_3: int = 0
    dim_4: int = 0
    dim_5: int = 0
    dim_6: int = 0


class TjcCoverageSummary(BaseModel):
    """Joint Commission coverage at a glance: counts + the EP codes of any gaps.

    A "gap" is an EP whose ``status == "gap"`` in the per-row coverage matrix
    (the chart-level read at ``/tjc-coverage`` carries the row-level detail).
    """

    covered_eps: int
    total_eps: int
    gap_eps: list[str]


class ChartRead(BaseModel):
    """The canonical /chart response (PRD §6.1)."""

    meta: ChartMeta
    patient: ChartPatient
    intake: ChartIntake
    timeline: list[TimelineNote]
    observations: list[ChartObservation]
    asam_summary: AsamSummary
    tjc_coverage: TjcCoverageSummary
