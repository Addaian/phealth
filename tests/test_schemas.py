"""
Unit tests for the Phase 2 Pydantic schema additions (PRD §5.4 / §5.5).

These are pure schema tests -- no FastAPI, no database -- exercising the
shapes introduced by M5: the ``ProvenanceRead`` envelope, the trinary
``SectionStatus`` on ``SectionRead``, the new ``ScaleRead`` model, and the
optional ``provenance`` slots on the observation/evidence/coverage reads.

The existing endpoint tests cover backwards-compat (the new optional fields
default to ``None``/``"found"`` so nothing already on the wire breaks).
"""

import uuid

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    AsamEvidenceRead,
    ObservationRead,
    ProvenanceRead,
    ScaleRead,
    SectionRead,
    TjcCoverageRead,
)

# ---------------------------------------------------------------------------
# ProvenanceRead
# ---------------------------------------------------------------------------


def test_provenance_read_validates_with_required_fields():
    prov = ProvenanceRead(document_id=uuid.uuid4(), char_start=10, char_end=17, snippet="example")
    # PRD §5.5 round-trip invariant: the recorded char span matches the snippet
    # length. The full-fidelity assertion (raw_text[start:end] == snippet) needs
    # a real document and lives in tests/test_provenance.py.
    assert prov.char_end - prov.char_start == len(prov.snippet)


def test_provenance_read_rejects_missing_fields():
    with pytest.raises(ValidationError):
        ProvenanceRead(document_id=uuid.uuid4(), char_start=0, char_end=1)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# SectionRead: trinary status + optional text/provenance
# ---------------------------------------------------------------------------


def test_section_read_not_assessed_validates_with_no_text_or_provenance():
    """The PRD §5.5 case: a section the assessor never had a prompt for."""
    section = SectionRead(
        title="PHQ-9", char_span=[0, 0], status="not_assessed", text=None, provenance=None
    )
    assert section.status == "not_assessed"
    assert section.text is None
    assert section.provenance is None


def test_section_read_status_defaults_to_found():
    """Existing handlers omit ``status``; the default keeps them green."""
    section = SectionRead(title="Chief Complaint", text="anxiety", char_span=[10, 17])
    assert section.status == "found"


def test_section_read_rejects_invalid_status_literal():
    with pytest.raises(ValidationError):
        SectionRead(title="x", char_span=[0, 0], status="maybe-found")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# ScaleRead
# ---------------------------------------------------------------------------


def test_scale_read_requires_provenance():
    """Every scale must cite the span of text that produced its score."""
    with pytest.raises(ValidationError):
        ScaleRead(instrument="PHQ-9", score=14, severity="moderate")  # type: ignore[call-arg]


def test_scale_read_validates_with_items_and_provenance():
    prov = ProvenanceRead(
        document_id=uuid.uuid4(), char_start=100, char_end=110, snippet="PHQ-9: 14"
    )
    scale = ScaleRead(
        instrument="PHQ-9",
        score=14,
        severity="moderate",
        items={"q1": 2, "q2": 1, "q3": 3},
        provenance=prov,
    )
    assert scale.status == "found"
    assert scale.items is not None and scale.items["q1"] == 2


# ---------------------------------------------------------------------------
# ObservationRead / AsamEvidenceRead / TjcCoverageRead: optional provenance
# ---------------------------------------------------------------------------


def test_observation_read_provenance_is_optional():
    """Phase 1 callers built ObservationRead without provenance; still valid."""
    obs = ObservationRead(
        id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        code_system="LOINC",
        code="44261-6",
        display="PHQ-9 total score",
        value_quantity=14.0,
        value_string=None,
        char_start=100,
        char_end=110,
        extraction_method="regex",
        confidence=0.95,
    )
    assert obs.provenance is None


def test_asam_evidence_read_provenance_optional():
    evidence = AsamEvidenceRead(
        document_id=uuid.uuid4(),
        char_start=200,
        char_end=240,
        subdimension="1A",
        snippet="reports tremor and sweating on day 1",
    )
    assert evidence.provenance is None


def test_tjc_coverage_read_with_gap_has_no_provenance():
    """A 'gap' row legitimately has no provenance -- nothing to cite."""
    row = TjcCoverageRead(
        ep_code="CTS.03.01.09",
        title="Suicide risk assessment",
        status="gap",
        rationale="No suicide-risk language in any document",
        evidence_document_id=None,
        evidence_char_start=None,
        evidence_char_end=None,
    )
    assert row.provenance is None
    assert row.status == "gap"
