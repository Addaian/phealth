"""
Pydantic response models for the /api/v1 read API.

Shapes the JSON the read endpoints return and gives ``/docs`` a precise schema.

Phase 2 (PRD §5.4 / §5.5) layers two new shapes into these models:

  1. **Char-offset provenance** -- ``ProvenanceRead`` is the canonical
     ``{document_id, char_start, char_end, snippet}`` envelope. The PRD
     requires every observation in every endpoint to carry one, so a
     reviewer can re-locate the source text in the document's ``raw_text``.

  2. **Trinary completeness** -- ``SectionRead.status`` distinguishes
     ``found`` (text present), ``looked_but_missing`` (the template expects
     this section but it is empty in this chart), and ``not_assessed`` (the
     section is not part of this document type's template at all). M6 will
     populate the status correctly; M5 only widens the schema.

The FHIR-aligned (camelCase, alias-driven) shapes live in
``app/api/fhir_schemas.py`` -- this module is snake_case + Pythonic. The two
modules deliberately do not share a base class: keeping them independent
prevents accidental FHIR-isms (e.g. ``Reference`` objects) from leaking into
the ergonomic API.

Models built from ORM rows set ``from_attributes=True`` so a route can return
the ORM object directly and let FastAPI coerce it.
"""

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# Trinary completeness status (PRD §5.5):
#   "found"               -- the section is present and non-empty
#   "looked_but_missing"  -- the section is in the template but empty in this chart
#   "not_assessed"        -- the section is not in the template (the assessor
#                            never had the prompt for it)
# The distinction matters clinically -- a missing PHQ-9 the assessor was asked
# to perform is a quality gap; a missing PHQ-9 in a document that never had
# that section is just an irrelevant absence.
SectionStatus = Literal["found", "looked_but_missing", "not_assessed"]


class ProvenanceRead(BaseModel):
    """A char-offset citation back into ``ClinicalDocument.raw_text``.

    Every clinically meaningful value the system surfaces -- a scale score,
    an entity tag, an ASAM evidence span -- carries one of these so a
    reviewer can confirm the value against the source text. The round-trip
    invariant: ``document.raw_text[char_start:char_end] == snippet`` (PRD §5.5).
    """

    document_id: uuid.UUID
    char_start: int
    char_end: int
    snippet: str


class SectionRead(BaseModel):
    """One document section: text, char-offset span, and completeness status.

    ``text`` and ``provenance`` are optional because a section may be present
    in the template but blank in the chart (``status="looked_but_missing"``),
    or wholly absent from a particular document type (``status="not_assessed"``).
    M6 populates ``status`` correctly; until then handlers default to
    ``"found"`` so the field is non-breaking.
    """

    title: str
    text: str | None = None
    char_span: list[int]
    status: SectionStatus = "found"
    provenance: ProvenanceRead | None = None


class PatientRead(BaseModel):
    """Patient demographics plus the stored canonical FHIR R4 Patient resource."""

    id: uuid.UUID
    external_id: str
    given_name: str
    family_name: str
    birth_date: date
    gender: str
    fhir: dict  # the stored FHIR R4 Patient resource (Patient.fhir_json)


class IntakeRead(BaseModel):
    """The BPS intake assessment: full raw text plus the sectioned JSON.

    Phase 2 §5.5: ``completeness_score`` is the fraction of *asked* template
    sections that were *answered* (see app/api/completeness.py). Section-level
    trinary status lives on each ``SectionRead`` so a reviewer can pinpoint
    which sections were missed.
    """

    document_id: uuid.UUID
    authored_on: datetime
    author_name: str
    author_role: str
    raw_text: str
    sections: dict[str, SectionRead]
    completeness_score: float


class TimelineEntry(BaseModel):
    """One progress note in the patient's timeline (PRD §5.4 envelope)."""

    document_id: uuid.UUID
    date: datetime
    author: str
    type: str  # soap | dap | dsap
    sections: dict[str, SectionRead]
    raw_text: str


class ObservationRead(BaseModel):
    """An extracted observation with its char-offset provenance.

    The flat ``char_start`` / ``char_end`` fields are preserved alongside
    the new nested ``provenance`` for backwards compatibility with the
    Phase 1 sample JSON; M7's ``/chart`` endpoint populates ``provenance``
    via a handler-side join into the document's ``raw_text``.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    code_system: str
    code: str
    display: str
    value_quantity: float | None
    value_string: str | None
    char_start: int
    char_end: int
    extraction_method: str
    confidence: float
    provenance: ProvenanceRead | None = None


class ScaleRead(BaseModel):
    """A single scale result -- LOINC-coded scoring instrument.

    Wraps a LOINC-coded ``ExtractedObservation`` (PHQ-9, GAD-7, AUDIT-C,
    CIWA-Ar, COWS, ...) with its severity bucket and per-item breakdown
    when available. Provenance is mandatory: every scale must cite the
    span of text that produced its score.

    Wired into ``/chart`` in M7; the schema lives here so M6's completeness
    classifier and M7's handler share a single definition.
    """

    instrument: str  # PHQ-9 | GAD-7 | AUDIT-C | CIWA-Ar | COWS | ...
    score: float | None = None
    severity: str | None = None  # mild | moderate | severe | ...
    status: SectionStatus = "found"
    items: dict[str, int | float] | None = None
    provenance: ProvenanceRead


class AsamEvidenceRead(BaseModel):
    """One ASAM-evidence span anchored in a document's ``raw_text``.

    ``provenance`` is a redundant nested form of the flat
    ``document_id``/``char_start``/``char_end``/``snippet`` fields, added in
    Phase 2 so every observation-shaped object across the API speaks the
    same provenance envelope.
    """

    model_config = ConfigDict(from_attributes=True)

    document_id: uuid.UUID
    char_start: int
    char_end: int
    subdimension: str | None
    snippet: str
    provenance: ProvenanceRead | None = None


class AsamDimensionRead(BaseModel):
    """ASAM evidence grouped under one of the six 4th-edition dimensions."""

    dimension: int
    name: str
    evidence: list[AsamEvidenceRead]


class TjcCoverageRead(BaseModel):
    """Coverage status for one Joint Commission Element of Performance."""

    ep_code: str
    title: str
    status: str  # satisfied | gap | ambiguous
    rationale: str
    evidence_document_id: uuid.UUID | None
    evidence_char_start: int | None
    evidence_char_end: int | None
    # Populated only when evidence pointers are non-null; a "gap" row has no
    # provenance because there is no supporting text to cite.
    provenance: ProvenanceRead | None = None
