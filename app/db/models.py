"""
SQLModel ORM table definitions — the storage substrate across phases.

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

Phase 3 (documents/phase_3_PRD.md §6.3) appends three cache / observability
tables:

  * ``AsamAssessment`` and ``TjcAuditResult`` cache each computed clinical
    assessment by ``(patient_id, evidence_hash, model_version)``. The
    ``evidence_hash`` is an xxhash64 over the contributing-row PKs; a re-POST
    with identical inputs hits the cache and returns a byte-identical body so
    the demo flow is deterministic even though Claude is not.
  * ``LlmInvocation`` is a per-call cost / latency / reliability log. Every
    Claude call writes one row, regardless of whether the assessment was a
    cache hit (no row) or fresh compute (one row). Reviewers see the real
    cost and latency numbers; the table is also the audit ground truth for
    the citation-validation path.

Alembic targets ``SQLModel.metadata``; importing this module registers all
table classes onto it. Table names are explicit snake_case.

The full-text-search column ``clinical_document.fts`` is intentionally NOT a
field here — it is a Postgres GENERATED column created in the Alembic
migration (it derives from ``raw_text`` and so never needs application-side
maintenance).
"""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import Column, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


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
    # NOTE: clinical_document.fts (tsvector) is added as a GENERATED column in
    # the Alembic migration — see this module's docstring. The legacy pgvector
    # ``embedding`` column was dropped during the Phase 3 OpenAI-removal pass
    # (see Alembic revision ``phase3_000_drop_embedding`` and
    # documents/phase_3_PRD.md §3 — Phase 3 retrieval is by structured query,
    # not vector similarity, so the column was never queried).


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


class AsamAssessment(SQLModel, table=True):
    """One ASAM 4th-edition Level-of-Care assessment for a patient.

    Caches the output of the deterministic rule engine + Claude-narrated
    rationale (phase_3_PRD.md §5.2, §5.7). The row is the *whole* response
    body — ``dimensions`` carries the per-subdimension ratings + rationale
    + citations, ``rules_fired`` lists every Chapter 10 rule that fired, and
    ``rationale`` holds the overall narration.

    Cache contract — POSTs are idempotent by ``(patient_id, evidence_hash,
    model_version)``: a repeat POST with the same chart and the same model
    returns this row verbatim, byte-stable ETag included. The unique
    constraint enforces this at the DB level (a duplicate insert raises and
    the API code falls back to a SELECT).

    The ``evidence_hash`` is the xxhash64 hex over the sorted contributing
    AsamEvidence + ExtractedObservation row PKs (phase_3_PRD.md §5.7) —
    deterministic, and changes only when the underlying evidence does.
    """

    __tablename__ = "asam_assessment"
    __table_args__ = (
        UniqueConstraint(
            "patient_id",
            "evidence_hash",
            "model_version",
            name="uq_asam_assessment_cache_key",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID = Field(foreign_key="patient.id", index=True)
    # xxhash64 hex of the canonical contributing-row set. Indexed for the
    # cache lookup; combined with patient_id + model_version it forms the
    # unique cache key (see __table_args__).
    evidence_hash: str = Field(index=True)
    # Recommended level token, e.g. "3.7" | "3.7-BIO" | "2.5-COE" — see
    # phase_3_PRD.md §5.10 for the closed enumeration.
    recommended_level: str
    # List of applied modifiers ("COE", "BIO", ...). Empty list = no modifier.
    modifiers: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    # Per-dimension findings: ratings, per-subdimension rationale, citations
    # (phase_3_PRD.md §6.1). JSONB so the shape can evolve without a migration.
    dimensions: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    # Ordered list of human-readable rules from Chapter 10 that contributed
    # to the decision (phase_3_PRD.md §5.4).
    rules_fired: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    rationale: str
    # high | moderate | low — see phase_3_PRD.md §6.7 for the threshold rules.
    confidence: str
    # ok | unavailable | degraded — set to "unavailable" when Claude was
    # unreachable (rule-engine-only fallback) and "degraded" when the
    # citation validator forced a re-narration (phase_3_PRD.md §5.9).
    rationale_status: str
    # Free-form warning tags, e.g. ["citation_validation_failed"]. Empty list
    # is the happy path.
    rationale_warnings: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    # Claude model id at compute time, e.g. "claude-sonnet-4-6". Part of the
    # cache key — swapping the model creates a new cache namespace so two
    # models' outputs can be compared side by side (phase_3_PRD.md §5.7).
    model_version: str
    computed_at: datetime = Field(default_factory=_utcnow)


class TjcAuditResult(SQLModel, table=True):
    """One Joint Commission compliance audit for a patient.

    Same caching pattern as ``AsamAssessment``: keyed by ``(patient_id,
    evidence_hash, model_version)``, and the row is the whole response body.
    ``findings`` is the per-EP list with the surveyor-narrated text and
    citations; ``summary`` is the {satisfied, gap, n/a, overall} rollup
    (phase_3_PRD.md §6.2).
    """

    __tablename__ = "tjc_audit_result"
    __table_args__ = (
        UniqueConstraint(
            "patient_id",
            "evidence_hash",
            "model_version",
            name="uq_tjc_audit_cache_key",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID = Field(foreign_key="patient.id", index=True)
    evidence_hash: str = Field(index=True)
    # List of TjcFinding dicts (phase_3_PRD.md §6.2). One entry per audited EP
    # (13 entries on the seeded Marcus chart).
    findings: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    # Rollup counts: {total_eps_audited, satisfied, gap, not_applicable,
    # overall_status} per phase_3_PRD.md §6.2.
    summary: dict = Field(default_factory=dict, sa_column=Column(JSONB, nullable=False))
    rationale_status: str
    rationale_warnings: list = Field(default_factory=list, sa_column=Column(JSONB, nullable=False))
    model_version: str
    computed_at: datetime = Field(default_factory=_utcnow)


class LlmInvocation(SQLModel, table=True):
    """One Claude API call's cost + latency + reliability record.

    Written by the ClaudeClient on every call regardless of cache state on
    the *assessment* side — cache hits don't call the LLM, so they don't
    write an LlmInvocation row; only fresh computes do. The table is the
    cost/latency ground truth surfaced in the demo recording and the
    operational backstop for the rate-limit / circuit-breaker logic
    (phase_3_PRD.md §5.8).

    Not indexed beyond ``patient_id`` for the MVP — query volume is low and
    the table is append-only. Add a (occurred_at) index if dashboards need
    time-bounded scans later.
    """

    __tablename__ = "llm_invocation"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    # "asam-loc" | "tjc-audit" | "section-detector" (the regex-miss fallback).
    endpoint: str
    patient_id: uuid.UUID = Field(index=True)
    model: str
    prompt_tokens: int
    completion_tokens: int
    # Anthropic prompt-cache reads — billed at 0.1× input rate; tracked
    # separately so the README cost line is accurate (phase_3_PRD.md §7).
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    latency_ms: int
    # xxhash64 hex of the response body; lets us notice when two cache-bypass
    # POSTs produced identical text (rare but useful for the demo).
    response_hash: str
    # False when the post-hoc CitationValidator (phase_3_PRD.md §5.6) flagged
    # at least one citation as broken. The narration path retries once on a
    # failure; this records the *final* outcome.
    citation_validation_passed: bool
    # Exception class name on a failed call, NULL on success.
    error_class: str | None = None
    occurred_at: datetime = Field(default_factory=_utcnow)


__all__ = [
    "SQLModel",
    "Patient",
    "Encounter",
    "ClinicalDocument",
    "ExtractedObservation",
    "AsamEvidence",
    "TjcCoverage",
    "AuditEvent",
    "AsamAssessment",
    "TjcAuditResult",
    "LlmInvocation",
]
