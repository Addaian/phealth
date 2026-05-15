"""
M7 unit tests for the CitationValidator.

The validator is the safety net after Claude's Citations-API output --
if Claude paraphrases instead of quoting verbatim, or cites a document
that doesn't belong to this patient, the validator catches it before
the assessment is persisted.

Each failure mode gets its own test. The happy path is also covered.
The tests build a real ClinicalDocument in the test DB so the validator's
DB lookups exercise the same path the production endpoint will.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.clinical.llm.citation_validator import CitationValidator
from app.clinical.shared.evidence_retrieval import build_evidence_document
from app.db.models import ClinicalDocument, Encounter, Patient

# ─── Helpers ─────────────────────────────────────────────────────────────


def _seed_patient_doc(db_session, raw_text: str = "Marcus admitted 2026-05-04. CIWA-Ar=12."):
    """Insert a Patient + Encounter + ClinicalDocument and return them.

    The doc's raw_text is what the validator will verify citations
    against. Tests should pick offsets that point into raw_text.
    """
    patient = Patient(
        external_id=f"TEST-{uuid4()}",
        given_name="Marcus",
        family_name="Reyes",
        birth_date=date(1991, 11, 4),
        gender="male",
    )
    db_session.add(patient)
    db_session.flush()

    encounter = Encounter(
        patient_id=patient.id,
        period_start=datetime(2026, 5, 4),
        class_code="IMP",
        type_code="intake",
    )
    db_session.add(encounter)
    db_session.flush()

    doc = ClinicalDocument(
        patient_id=patient.id,
        encounter_id=encounter.id,
        document_type="bps_intake",
        authored_on=datetime(2026, 5, 4, 13, 30),
        author_name="Dr Test",
        author_role="psychiatrist",
        raw_text=raw_text,
        content_hash=hashlib.sha256(raw_text.encode()).hexdigest(),
    )
    db_session.add(doc)
    db_session.flush()
    return patient, doc


def _fake_citation(cited_text: str, document_index: int, start: int, end: int):
    """Build a SimpleNamespace matching CitationCharLocation's read shape."""
    return SimpleNamespace(
        type="char_location",
        cited_text=cited_text,
        document_index=document_index,
        document_title=None,
        start_char_index=start,
        end_char_index=end,
        file_id=None,
    )


def _fake_message(citations_per_block: list[list]):
    """Build a SimpleNamespace mimicking anthropic.types.Message.

    ``citations_per_block`` is a list of citation lists -- one inner list
    per text content block. Lets tests construct multi-block responses
    cleanly.
    """
    content = [
        SimpleNamespace(type="text", text="...", citations=cites) for cites in citations_per_block
    ]
    return SimpleNamespace(content=content)


# ─── Happy path ──────────────────────────────────────────────────────────


def test_validator_passes_when_citation_text_matches(db_session):
    """The high-value invariant: raw_text[start:end] == cited_text."""
    raw_text = "Marcus admitted on 2026-05-04. CIWA-Ar = 12 on admission."
    patient, doc = _seed_patient_doc(db_session, raw_text=raw_text)

    # We sent the entire raw_text as the "snippet" (char_start=0).
    document_block = build_evidence_document(
        SimpleNamespace(
            document_id=doc.id,
            char_start=0,
            char_end=len(raw_text),
            snippet=raw_text,
        )
    )

    # Claude cites "CIWA-Ar = 12" -- which lives at chars [31:43] of the snippet.
    cited_text = "CIWA-Ar = 12"
    snippet_start = raw_text.index(cited_text)
    citation = _fake_citation(
        cited_text=cited_text,
        document_index=0,
        start=snippet_start,
        end=snippet_start + len(cited_text),
    )
    message = _fake_message([[citation]])

    result = CitationValidator(db_session).validate(
        message, documents_sent=[document_block], patient_id=patient.id
    )
    assert result.passed is True
    assert result.citations_checked == 1
    assert result.failures == []


def test_validator_passes_on_response_with_no_citations(db_session):
    """An empty response (no citations) passes trivially."""
    patient, _ = _seed_patient_doc(db_session)
    message = _fake_message([[]])
    result = CitationValidator(db_session).validate(
        message, documents_sent=[], patient_id=patient.id
    )
    assert result.passed is True
    assert result.citations_checked == 0


# ─── Failure mode: unknown_doc ───────────────────────────────────────────


def test_validator_flags_unknown_document_index(db_session):
    """A citation whose document_index is out of range fails."""
    patient, doc = _seed_patient_doc(db_session)
    block = build_evidence_document(
        SimpleNamespace(document_id=doc.id, char_start=0, char_end=10, snippet="...")
    )
    citation = _fake_citation(cited_text="x", document_index=5, start=0, end=1)
    message = _fake_message([[citation]])

    result = CitationValidator(db_session).validate(
        message, documents_sent=[block], patient_id=patient.id
    )
    assert result.passed is False
    assert len(result.failures) == 1
    assert result.failures[0].failure_type == "unknown_doc"


def test_validator_flags_doc_belonging_to_other_patient(db_session):
    """Cross-patient citation -- belongs to a different patient -> fail."""
    # Seed two patients with their own docs.
    _, doc_a = _seed_patient_doc(db_session, raw_text="patient A text")
    patient_b, _ = _seed_patient_doc(db_session, raw_text="patient B text")

    # The prompt cited doc_a (patient A's doc) but the validator runs
    # for patient B -- this MUST fail.
    block = build_evidence_document(
        SimpleNamespace(
            document_id=doc_a.id,
            char_start=0,
            char_end=len("patient A text"),
            snippet="patient A text",
        )
    )
    citation = _fake_citation(cited_text="patient A", document_index=0, start=0, end=9)
    message = _fake_message([[citation]])

    result = CitationValidator(db_session).validate(
        message, documents_sent=[block], patient_id=patient_b.id
    )
    assert result.passed is False
    assert result.failures[0].failure_type == "unknown_doc"


# ─── Failure mode: out_of_range ──────────────────────────────────────────


def test_validator_flags_out_of_range_offsets(db_session):
    """Citation offsets past the document's raw_text length -> fail."""
    raw_text = "short text"  # 10 chars
    patient, doc = _seed_patient_doc(db_session, raw_text=raw_text)
    # We claim our snippet starts at offset 0, length 10, BUT Claude
    # cites chars 50-60 -- out of range.
    block = build_evidence_document(
        SimpleNamespace(document_id=doc.id, char_start=0, char_end=10, snippet=raw_text)
    )
    citation = _fake_citation(cited_text="x" * 10, document_index=0, start=50, end=60)
    message = _fake_message([[citation]])

    result = CitationValidator(db_session).validate(
        message, documents_sent=[block], patient_id=patient.id
    )
    assert result.passed is False
    assert result.failures[0].failure_type == "out_of_range"


# ─── Failure mode: snippet_mismatch ──────────────────────────────────────


def test_validator_flags_snippet_mismatch(db_session):
    """The keystone test: Claude paraphrased instead of quoting verbatim.

    raw_text[start:end] != cited_text -> snippet_mismatch.
    """
    raw_text = "Marcus admitted 2026-05-04. CIWA-Ar = 12 on admission."
    patient, doc = _seed_patient_doc(db_session, raw_text=raw_text)
    block = build_evidence_document(
        SimpleNamespace(document_id=doc.id, char_start=0, char_end=len(raw_text), snippet=raw_text)
    )
    # Claude says it's citing "CIWA-Ar = 12" but the offsets point at
    # "Marcus admit" -- the snippets do not match.
    citation = _fake_citation(
        cited_text="CIWA-Ar = 12",
        document_index=0,
        start=0,
        end=12,
    )
    message = _fake_message([[citation]])

    result = CitationValidator(db_session).validate(
        message, documents_sent=[block], patient_id=patient.id
    )
    assert result.passed is False
    assert result.failures[0].failure_type == "snippet_mismatch"
    assert "Marcus admit" in result.failures[0].detail


# ─── Failure mode: malformed_context ─────────────────────────────────────


def test_validator_flags_malformed_context(db_session):
    """A document block whose context isn't valid JSON -> fail with
    malformed_context."""
    patient, _ = _seed_patient_doc(db_session)
    bad_block = {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": "x"},
        "context": "not-json",  # malformed
        "citations": {"enabled": True},
    }
    citation = _fake_citation(cited_text="x", document_index=0, start=0, end=1)
    message = _fake_message([[citation]])

    result = CitationValidator(db_session).validate(
        message, documents_sent=[bad_block], patient_id=patient.id
    )
    assert result.passed is False
    assert result.failures[0].failure_type == "malformed_context"


# ─── Multi-citation aggregation ──────────────────────────────────────────


def test_validator_reports_all_failures_not_just_first(db_session):
    """When multiple citations fail, the validator returns all of them.

    This lets M8's narration retry log the full failure set in one go
    rather than fixing them one round-trip at a time.
    """
    raw_text = "valid text here for citation testing"
    patient, doc = _seed_patient_doc(db_session, raw_text=raw_text)
    block = build_evidence_document(
        SimpleNamespace(document_id=doc.id, char_start=0, char_end=len(raw_text), snippet=raw_text)
    )
    bad_cite_1 = _fake_citation("hallucinated", document_index=0, start=0, end=12)
    bad_cite_2 = _fake_citation("also-wrong", document_index=99, start=0, end=10)
    message = _fake_message([[bad_cite_1, bad_cite_2]])

    result = CitationValidator(db_session).validate(
        message, documents_sent=[block], patient_id=patient.id
    )
    assert result.passed is False
    assert result.citations_checked == 2
    assert len(result.failures) == 2
    failure_types = {f.failure_type for f in result.failures}
    assert failure_types == {"snippet_mismatch", "unknown_doc"}
