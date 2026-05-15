"""
M8 acceptance tests for the ASAM narration + response builder.

These tests run end-to-end against the live ingested Marcus chart but
**never call Claude**. A canned ``AsamRationale`` is injected via a
mocked SDK; the test then verifies that:

  1. The narrate() function returns the canned rationale on the happy path.
  2. Citation validation runs and aggregates failures correctly.
  3. The validation-failure path retries once and degrades on second
     failure (per phase_3_PRD.md §5.9).
  4. The response builder joins rule-engine output + LLM narrative into
     a valid ``AsamAssessmentRead`` matching the PRD §6.1 shape.
  5. Marcus's full pipeline (compute_risk_ratings → decide → narrate →
     build_asam_response) produces Level 3.7 with a coherent response.

The same SDK-mock pattern from ``test_claude_client.py`` is used.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import anthropic
import httpx

from app.clinical.asam.level_decision import decide
from app.clinical.asam.narration import NarrationOutcome, narrate
from app.clinical.asam.risk_ratings import compute_risk_ratings
from app.clinical.asam.schemas import (
    AsamRationale,
    Citation,
    DimensionRationale,
    SubdimensionRationale,
)
from app.clinical.llm.claude_client import ClaudeClient
from app.clinical.shared.response_builder import build_asam_response

# ────────────────────────────────────────────────────────────────────────
# Fake SDK helpers.
# ────────────────────────────────────────────────────────────────────────


def _fake_usage(input_tokens: int = 4000, output_tokens: int = 800):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )


def _fake_tool_use_message(payload: dict):
    """Build a fake Message whose content is one tool_use block."""
    return SimpleNamespace(
        usage=_fake_usage(),
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_structured_response",
                input=payload,
                id="tool_use_1",
            )
        ],
        id="msg_test",
        model_dump_json=lambda: '{"id":"msg_test"}',
    )


def _fake_raw(message):
    return SimpleNamespace(
        parse=lambda: message,
        headers=httpx.Headers({"anthropic-ratelimit-requests-remaining": "999"}),
    )


def _client_returning(payloads: list[dict], db_session) -> ClaudeClient:
    """Build a ClaudeClient whose successive calls return successive payloads.

    Lets a single test exercise both the first call AND the retry.
    """
    sdk = anthropic.Anthropic(api_key="test")
    raw_responses = [_fake_raw(_fake_tool_use_message(p)) for p in payloads]
    sdk.messages.with_raw_response.create = MagicMock(side_effect=raw_responses)  # type: ignore[method-assign]
    return ClaudeClient(sdk_client=sdk, session_factory=lambda: iter([db_session]))


# ────────────────────────────────────────────────────────────────────────
# Canned-rationale builders for Marcus.
# ────────────────────────────────────────────────────────────────────────


def _marcus_rationale_with_valid_citation(citation: Citation) -> dict:
    """Canned AsamRationale for Marcus that includes one valid citation."""
    return {
        "overall_rationale": (
            "Patient resolves to Level 3.7 (Medically Monitored Intensive Inpatient) "
            "driven by active benzo taper and concurrent SUD diagnoses. Non-COE "
            "because passive ideation alone is appropriate for standard "
            "co-occurring-capable care; non-BIO because no 3B anchor is present."
        ),
        "dimensions": [
            {
                "dimension": 1,
                "rationale": (
                    "Active chlordiazepoxide taper for benzodiazepine "
                    "withdrawal with concurrent alcohol use disorder."
                ),
                "subdimensions": [
                    {
                        "name": "dim1_addiction_meds",
                        "rationale": (
                            "Active benzo taper requires medically managed care; "
                            "C anchor maps to Min Level 3.7."
                        ),
                        "citations": [citation.model_dump(mode="json")],
                    }
                ],
            }
        ],
        "confidence": "high",
    }


def _marcus_rationale_with_bogus_citation() -> dict:
    """Canned AsamRationale that cites a non-existent UUID -> validation fails."""
    return {
        "overall_rationale": "Marcus resolves to 3.7.",
        "dimensions": [
            {
                "dimension": 1,
                "rationale": "...",
                "subdimensions": [
                    {
                        "name": "dim1_addiction_meds",
                        "rationale": "...",
                        "citations": [
                            {
                                "document_id": str(uuid4()),  # random; not in DB
                                "char_start": 0,
                                "char_end": 5,
                                "snippet": "wrong",
                            }
                        ],
                    }
                ],
            }
        ],
        "confidence": "moderate",
    }


def _citation_from_marcus_chart(ingested_patient, db_session) -> Citation:
    """Pick a real (document_id, char_start, char_end, snippet) tuple from
    Marcus's AsamEvidence -- guaranteed to validate."""
    from sqlmodel import select

    from app.db.models import AsamEvidence, ClinicalDocument

    # Grab any one evidence row for the patient's documents.
    doc_ids = [
        d.id
        for d in db_session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == ingested_patient.id)
        )
    ]
    evidence = db_session.exec(
        select(AsamEvidence).where(
            AsamEvidence.document_id.in_(doc_ids)  # type: ignore[attr-defined]
        )
    ).first()
    assert evidence is not None
    return Citation(
        document_id=evidence.document_id,
        char_start=evidence.char_start,
        char_end=evidence.char_end,
        snippet=evidence.snippet,
    )


# ────────────────────────────────────────────────────────────────────────
# narrate() happy path.
# ────────────────────────────────────────────────────────────────────────


def test_narrate_happy_path_returns_ok_status(ingested_patient, db_session):
    """Canned rationale with a valid citation -> NarrationOutcome.status == 'ok'."""
    citation = _citation_from_marcus_chart(ingested_patient, db_session)
    payload = _marcus_rationale_with_valid_citation(citation)
    client = _client_returning([payload], db_session)

    ratings = compute_risk_ratings(ingested_patient.id, db_session)
    decision = decide(ratings)
    outcome = narrate(
        client=client,
        db=db_session,
        patient_id=ingested_patient.id,
        decision=decision,
        ratings=ratings,
    )
    assert outcome.status == "ok"
    assert outcome.warnings == []
    assert outcome.validation.passed is True
    assert outcome.validation.citations_checked == 1


# ────────────────────────────────────────────────────────────────────────
# narrate() degraded path on validation failure.
# ────────────────────────────────────────────────────────────────────────


def test_narrate_retries_once_then_degrades_on_double_failure(ingested_patient, db_session):
    """Two consecutive bogus-citation payloads -> degraded outcome with
    citations stripped and the warning tag set."""
    bogus = _marcus_rationale_with_bogus_citation()
    client = _client_returning([bogus, bogus], db_session)

    ratings = compute_risk_ratings(ingested_patient.id, db_session)
    decision = decide(ratings)
    outcome = narrate(
        client=client,
        db=db_session,
        patient_id=ingested_patient.id,
        decision=decision,
        ratings=ratings,
    )
    assert outcome.status == "degraded"
    assert "citation_validation_failed" in outcome.warnings
    # The degraded rationale strips citations.
    for dim in outcome.rationale.dimensions:
        for sub in dim.subdimensions:
            assert sub.citations == []
    # The SDK got hit twice (first attempt + retry).
    sdk_mock = client._sdk.messages.with_raw_response.create  # type: ignore[attr-defined]
    assert sdk_mock.call_count == 2


def test_narrate_retry_recovers_on_second_attempt(ingested_patient, db_session):
    """First call fails citation validation; retry succeeds -> ok status
    with a warning marker so the response builder can stamp it."""
    bogus = _marcus_rationale_with_bogus_citation()
    citation = _citation_from_marcus_chart(ingested_patient, db_session)
    good = _marcus_rationale_with_valid_citation(citation)
    client = _client_returning([bogus, good], db_session)

    ratings = compute_risk_ratings(ingested_patient.id, db_session)
    decision = decide(ratings)
    outcome = narrate(
        client=client,
        db=db_session,
        patient_id=ingested_patient.id,
        decision=decision,
        ratings=ratings,
    )
    assert outcome.status == "ok"
    assert "citation_validation_retry" in outcome.warnings
    assert outcome.validation.passed is True


# ────────────────────────────────────────────────────────────────────────
# build_asam_response() — full end-to-end shape check.
# ────────────────────────────────────────────────────────────────────────


def test_build_asam_response_marcus_full_pipeline(ingested_patient, db_session):
    """End-to-end: chart -> ratings -> decision -> narration -> response.

    Mocked Claude, real DB-ingested chart, real rule engine. Asserts:
      * recommendation.level == "3.7"
      * Marcus's recommendation flags are non-COE non-BIO
      * dimensions list is non-empty
      * Each SubdimensionRead carries a rating + min_level
      * rationale text is non-empty
    """
    citation = _citation_from_marcus_chart(ingested_patient, db_session)
    payload = _marcus_rationale_with_valid_citation(citation)
    client = _client_returning([payload], db_session)

    ratings = compute_risk_ratings(ingested_patient.id, db_session)
    decision = decide(ratings)
    outcome = narrate(
        client=client,
        db=db_session,
        patient_id=ingested_patient.id,
        decision=decision,
        ratings=ratings,
    )

    response = build_asam_response(
        patient_id=ingested_patient.id,
        evidence_hash="test-hash",
        model_version="claude-sonnet-4-6",
        cached=False,
        decision=decision,
        ratings=ratings,
        narration=outcome,
        computed_at=datetime(2026, 5, 15, 14, 22, 8),
    )

    # Top-level shape.
    assert response.patient_id == ingested_patient.id
    assert response.model_version == "claude-sonnet-4-6"
    assert response.cached is False
    assert response.evidence_hash == "test-hash"

    # The demo-defining assertion.
    assert response.recommendation.level == "3.7"
    assert response.recommendation.co_occurring_enhanced is False
    assert response.recommendation.biomedical_enhanced is False
    assert response.recommendation.level_display == "Medically Monitored Intensive Inpatient"

    # Dimensions list is populated; every subdim has a rating + min_level.
    assert len(response.dimensions) >= 1
    for dim in response.dimensions:
        assert dim.name
        for sub in dim.subdimensions:
            assert sub.rating
            assert sub.min_level

    # The rule-fired trace and the overall rationale both made it through.
    assert response.rules_fired  # non-empty
    assert response.rationale  # non-empty
    assert response.rationale_status == "ok"

    # Pydantic round-trip.
    payload_out = response.model_dump(mode="json")
    assert payload_out["recommendation"]["level"] == "3.7"


def test_build_asam_response_degraded_status_propagates_to_top_level(ingested_patient, db_session):
    """When narration degrades, the response's rationale_status reflects it."""
    bogus = _marcus_rationale_with_bogus_citation()
    client = _client_returning([bogus, bogus], db_session)

    ratings = compute_risk_ratings(ingested_patient.id, db_session)
    decision = decide(ratings)
    outcome = narrate(
        client=client,
        db=db_session,
        patient_id=ingested_patient.id,
        decision=decision,
        ratings=ratings,
    )
    response = build_asam_response(
        patient_id=ingested_patient.id,
        evidence_hash="x",
        model_version="claude-sonnet-4-6",
        cached=False,
        decision=decision,
        ratings=ratings,
        narration=outcome,
        computed_at=datetime(2026, 5, 15),
    )
    assert response.rationale_status == "degraded"
    assert "citation_validation_failed" in response.rationale_warnings
    # The level itself is unchanged — the engine is the source of truth.
    assert response.recommendation.level == "3.7"


# ────────────────────────────────────────────────────────────────────────
# NarrationOutcome shape stability.
# ────────────────────────────────────────────────────────────────────────


def test_narration_outcome_is_serializable():
    """The narration outcome wraps Pydantic models -- ensure they round-trip."""
    outcome = NarrationOutcome(
        rationale=AsamRationale(
            overall_rationale="x",
            dimensions=[
                DimensionRationale(
                    dimension=1,
                    rationale="y",
                    subdimensions=[SubdimensionRationale(name="dim1_withdrawal", rationale="z")],
                )
            ],
            confidence="high",
        ),
        validation=None,  # type: ignore[arg-type]
        status="ok",
        warnings=[],
    )
    payload = outcome.rationale.model_dump(mode="json")
    assert payload["overall_rationale"] == "x"
    assert payload["dimensions"][0]["subdimensions"][0]["name"] == "dim1_withdrawal"
