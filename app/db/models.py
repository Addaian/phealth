"""
SQLModel ORM table definitions — the Phase 1 storage substrate.

This is the FHIR-R4-shaped schema described in documents/phase_1_PRD.md §5 and
documents/phase_1.md §Step 3. Design intent:

  * One unified ``ClinicalDocument`` table holds the BPS intake AND every
    progress note; the format-specific structure (SOAP vs DAP vs DSAP) is
    preserved in the JSONB ``sections`` column rather than in separate tables.
  * Every relational row with a FHIR analogue carries a sibling ``fhir_json``
    (or ``fhir_*``) JSONB column, populated at write time. The substrate is
    FHIR-shaped, not FHIR-served — a future GET /fhir/<Resource> endpoint
    becomes a no-op join.
  * ``ExtractedObservation``, ``AsamEvidence`` and ``TjcCoverage`` store
    char-offset provenance ``(document_id, char_start, char_end)`` so every
    downstream finding can cite the exact source span (PRD §5.5).
  * Pre-computed evidence tables (``AsamEvidence``, ``TjcCoverage``) turn the
    Phase 3 reasoning endpoints into retrieval queries rather than
    full-document inference.

Alembic targets ``SQLModel.metadata``; importing this module registers all
table classes onto it. Table names are explicit snake_case.

The full-text-search column ``clinical_document.fts`` is intentionally NOT a
field here — it is a Postgres GENERATED column created in the Alembic
migration (it derives from ``raw_text`` and so never needs application-side
maintenance).
"""

import uuid
from datetime import UTC, date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

# Embedding dimensionality for OpenAI text-embedding-3-small (PRD §7; used in M6).
# Must match the output dimension of whatever model the EMBEDDING_MODEL setting
# (app/core/config.py) names — if that model is swapped, update this too.
EMBEDDING_DIM = 1536


def _utcnow() -> datetime:
    """Naive UTC timestamp for audit rows.

    Naive (not tz-aware) to match the plain ``TIMESTAMP`` columns used
    throughout this MVP schema — the deployment is single-timezone. Computed
    via ``datetime.now(UTC)`` rather than the deprecated ``datetime.utcnow()``.
    """
    return datetime.now(UTC).replace(tzinfo=None)


class Patient(SQLModel, table=True):
    """A single patient. Mirrors a FHIR R4 ``Patient`` resource in ``fhir_json``."""

    __tablename__ = "patient"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # SimplePractice client id (from the export). Indexed for idempotent
    # re-ingest and for cross-referencing back to the source EMR.
    external_id: str = Field(index=True)
    given_name: str
    family_name: str
    birth_date: date
    gender: str  # FHIR administrative-gender: male | female | other | unknown
    # Canonical FHIR R4 Patient resource as JSON. Populated by app.fhir.mappers (M7).
    fhir_json: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))


class Encounter(SQLModel, table=True):
    """A clinical encounter (admission, progress session, reassessment).

    Mirrors a FHIR R4 ``Encounter`` resource in ``fhir_json``.
    """

    __tablename__ = "encounter"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID = Field(foreign_key="patient.id", index=True)
    period_start: datetime
    period_end: datetime | None = None
    # FHIR v3-ActCode encounter class: AMB (ambulatory) | IMP (inpatient) | EMER.
    class_code: str
    # Local encounter type: intake | progress | discharge | loc-reassessment.
    type_code: str
    fhir_json: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))


class ClinicalDocument(SQLModel, table=True):
    """Unified store for the BPS intake AND every progress note.

    The format-specific structure is preserved in ``sections`` (JSONB):
        SOAP rows -> subjective / objective / assessment / plan
        DAP  rows -> data / assessment / plan
        DSAP rows -> data / subjective / assessment / plan
        BPS  rows -> the 21 intake sections
    Each section value is ``{"text": ..., "char_span": [start, end]}``.

    Mirrors FHIR ``DocumentReference`` (always) and ``ClinicalImpression``
    (progress notes only) in the ``fhir_*`` columns.
    """

    __tablename__ = "clinical_document"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID = Field(foreign_key="patient.id", index=True)
    encounter_id: uuid.UUID = Field(foreign_key="encounter.id", index=True)
    # bps_intake | soap | dap | dsap
    document_type: str
    # SimplePractice document id (from the export filename); part of the
    # idempotency story alongside content_hash.
    external_id: str | None = Field(default=None, index=True)
    authored_on: datetime
    author_name: str
    author_role: str
    # Exact text extracted from the source PDF — the provenance ground truth
    # that every ExtractedObservation char-offset indexes into.
    raw_text: str
    # sha256(raw_text); a re-ingest of an identical document is a no-op
    # (PRD §7 idempotency). Unique so the DB enforces it.
    content_hash: str = Field(index=True, unique=True)
    sections: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    fhir_document_reference: dict = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )
    # Only progress notes carry a ClinicalImpression; the BPS intake leaves this null.
    fhir_clinical_impression: dict | None = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    # Per-document section embedding for Phase 3 semantic retrieval (pgvector).
    # NOTE: clinical_document.fts (tsvector) is added as a GENERATED column in
    # the Alembic migration — see this module's docstring.
    embedding: list[float] | None = Field(
        default=None, sa_column=Column(Vector(EMBEDDING_DIM), nullable=True)
    )


class ExtractedObservation(SQLModel, table=True):
    """One clinically meaningful value extracted from a document.

    Backs both the Phase 2 structured output and the Phase 3 explainable
    reasoning. Carries char-offset provenance back into
    ``ClinicalDocument.raw_text`` (PRD §5.5).
    """

    __tablename__ = "extracted_observation"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    document_id: uuid.UUID = Field(foreign_key="clinical_document.id", index=True)
    # LOINC | SNOMED | RxNorm | internal
    code_system: str
    code: str  # e.g. "44261-6" — LOINC code for PHQ-9 total score
    display: str  # human-readable label, e.g. "PHQ-9 total score"
    value_quantity: float | None = None
    value_string: str | None = None
    # Provenance: exact offsets into the source document's raw_text.
    char_start: int
    char_end: int
    # regex | llm | manual — per-field, flips to llm only on a regex miss (PRD §5.3).
    extraction_method: str
    confidence: float


class AsamEvidence(SQLModel, table=True):
    """A span of text providing evidence for one ASAM 4th-edition dimension.

    Composite PK (document_id, dimension, char_start): one row per distinct
    evidence span. Pre-computed at ingest so the Phase 3 ASAM endpoint is a
    SELECT, not a full-document inference.
    """

    __tablename__ = "asam_evidence"

    document_id: uuid.UUID = Field(foreign_key="clinical_document.id", primary_key=True)
    # ASAM 4th-edition dimension number, 1..6 (see documents/phase_1.md §B).
    dimension: int = Field(primary_key=True)
    char_start: int = Field(primary_key=True)
    char_end: int
    subdimension: str | None = None
    snippet: str


class TjcCoverage(SQLModel, table=True):
    """Coverage status for one Joint Commission Element of Performance (EP),
    per patient.

    Composite PK (patient_id, ep_code). ``status`` is satisfied | gap |
    ambiguous, with a pointer to the supporting (or conspicuously absent)
    evidence span.
    """

    __tablename__ = "tjc_coverage"

    patient_id: uuid.UUID = Field(foreign_key="patient.id", primary_key=True)
    ep_code: str = Field(primary_key=True)  # e.g. "CTS.03.01.09"
    # satisfied | gap | ambiguous
    status: str
    evidence_document_id: uuid.UUID | None = Field(default=None, foreign_key="clinical_document.id")
    evidence_char_start: int | None = None
    evidence_char_end: int | None = None
    rationale: str


class AuditEvent(SQLModel, table=True):
    """Event-sourced log of every state-changing operation (PRD §7).

    Every ingest / extract / amend appends a row here, giving longitudinal
    queryability for ASAM reassessment workflows for free.
    """

    __tablename__ = "audit_event"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    occurred_at: datetime = Field(default_factory=_utcnow)
    actor: str
    # ingest | extract | amend
    action: str
    resource_type: str
    resource_id: uuid.UUID
    payload: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))


__all__ = [
    "SQLModel",
    "EMBEDDING_DIM",
    "Patient",
    "Encounter",
    "ClinicalDocument",
    "ExtractedObservation",
    "AsamEvidence",
    "TjcCoverage",
    "AuditEvent",
]
