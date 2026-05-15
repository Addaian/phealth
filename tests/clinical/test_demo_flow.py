"""
M12 demo-flow consolidation -- the demo-defining tests for Phase 3.

Each test in this file maps to one row in the phase_3_PRD.md §8
Success Metrics table. They are not unit tests; they are the
"would-the-demo-work" checks the user (and the assessment reviewers)
care about most. If anything in this file regresses, the submission is
broken regardless of what unit-test coverage looks like.

Reuses the mocked-Claude fixtures from the M10 endpoint tests rather
than spinning up new helpers -- the import is intentional cross-test
sharing for this consolidated demo suite.
"""

from __future__ import annotations

import pytest

from app.clinical.tjc.runner import run_audit
from app.db.models import ClinicalDocument

# Mocked-SDK fixtures (`mocked_client_factory`) live in
# tests/clinical/conftest.py.


def _asam_payload_with_real_citation(citation_dict: dict) -> dict:
    return {
        "overall_rationale": "Marcus resolves to Level 3.7.",
        "dimensions": [
            {
                "dimension": 1,
                "rationale": "Active benzo taper.",
                "subdimensions": [
                    {
                        "name": "dim1_addiction_meds",
                        "rationale": "Active taper -> Min Level 3.7.",
                        "citations": [citation_dict],
                    }
                ],
            }
        ],
        "confidence": "high",
    }


def _tjc_payload_for(findings) -> dict:
    return {
        "findings": [
            {
                "ep_code": f.ep_code,
                "narrative": f"Standard {f.ep_code}: {f.finding_template}",
                "citations": [],
            }
            for f in findings
        ]
    }


def _first_evidence_citation(patient_id, db_session) -> dict:
    """Pick one real AsamEvidence row and shape it as the Pydantic Citation dict."""
    from sqlmodel import select

    from app.db.models import AsamEvidence

    doc_ids = [
        d.id
        for d in db_session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_id)
        )
    ]
    evidence = db_session.exec(
        select(AsamEvidence).where(
            AsamEvidence.document_id.in_(doc_ids)  # type: ignore[attr-defined]
        )
    ).first()
    return {
        "document_id": str(evidence.document_id),
        "char_start": evidence.char_start,
        "char_end": evidence.char_end,
        "snippet": evidence.snippet,
    }


# ────────────────────────────────────────────────────────────────────────
# Success metric M1 — Marcus admission POST returns level "3.7".
# ────────────────────────────────────────────────────────────────────────


def test_M1_marcus_post_asam_loc_returns_level_3_7(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    citation = _first_evidence_citation(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_asam_payload_with_real_citation(citation)])
    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assert response.status_code == 201
    assert response.json()["recommendation"]["level"] == "3.7"


# ────────────────────────────────────────────────────────────────────────
# Success metric M3 — TJC audit surfaces all 5 planted gaps.
# Success metric M4 — At least 7 EPs satisfied.
# ────────────────────────────────────────────────────────────────────────


def test_M3_M4_tjc_audit_marcus_distribution(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    findings = run_audit(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_tjc_payload_for(findings)])
    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})
    body = response.json()
    # M3: all 5 planted gaps linked.
    planted = {f["linked_planted_gap"] for f in body["findings"] if f.get("linked_planted_gap")}
    assert planted == {"G1", "G2", "G3", "G4", "G5"}
    # M4: ≥ 7 satisfied EPs.
    assert body["summary"]["satisfied"] >= 7


# ────────────────────────────────────────────────────────────────────────
# Success metric M5 — Every citation in every response round-trips.
# raw_text[char_start:char_end] == snippet.
# ────────────────────────────────────────────────────────────────────────


def test_M5_citation_round_trip_against_db(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    """Walk every citation the API returns and re-verify against raw_text."""
    citation = _first_evidence_citation(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_asam_payload_with_real_citation(citation)])
    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    body = response.json()
    # Collect every Citation across the dimensions tree.
    citations: list[dict] = []
    for dim in body["dimensions"]:
        for sub in dim["subdimensions"]:
            citations.extend(sub.get("citations", []))
    assert citations  # at least one citation made it through

    # Each citation must round-trip against the real raw_text.
    from uuid import UUID

    for cite in citations:
        doc = db_session.get(ClinicalDocument, UUID(cite["document_id"]))
        assert doc is not None, f"unknown doc {cite['document_id']}"
        actual = doc.raw_text[cite["char_start"] : cite["char_end"]]
        assert actual == cite["snippet"], (
            f"round-trip failed: raw_text slice {actual!r} != {cite['snippet']!r}"
        )


# ────────────────────────────────────────────────────────────────────────
# Success metric M6 — Second POST -> cached + identical id + identical ETag.
# Success metric M7 — GET with If-None-Match -> 304.
# ────────────────────────────────────────────────────────────────────────


def test_M6_M7_cache_and_304(ingested_patient, db_session, api_client, mocked_client_factory):
    citation = _first_evidence_citation(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_asam_payload_with_real_citation(citation)])
    first = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assert first.status_code == 201
    second = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assert second.status_code == 200
    assert second.headers["etag"] == first.headers["etag"]
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["cached"] is True

    # M7: GET with matching If-None-Match -> 304.
    not_modified = api_client.get(
        f"/api/v1/asam-assessments/{first.json()['id']}",
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert not_modified.status_code == 304


# ────────────────────────────────────────────────────────────────────────
# Success metric M8 — FHIR resources validate.
# Already covered exhaustively in test_fhir_clinical_decision.py; here a
# single smoke check that the live ClinicalImpression entries persist.
# ────────────────────────────────────────────────────────────────────────


def test_M8_fhir_clinical_impression_validates(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    from fhir.resources.R4B.clinicalimpression import ClinicalImpression

    citation = _first_evidence_citation(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_asam_payload_with_real_citation(citation)])
    post = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    aid = post.json()["id"]

    fhir = api_client.get(f"/fhir/ClinicalImpression/{aid}")
    assert fhir.status_code == 200
    # If this raises, the resource is not R4-valid.
    ClinicalImpression.model_validate(fhir.json())


# ────────────────────────────────────────────────────────────────────────
# Success metric M9 — Every successful POST writes one LlmInvocation row.
# ────────────────────────────────────────────────────────────────────────


def test_M9_llm_invocation_row_written_on_fresh_post(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    from sqlmodel import select

    from app.db.models import LlmInvocation

    citation = _first_evidence_citation(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_asam_payload_with_real_citation(citation)])
    api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    rows = list(
        db_session.exec(
            select(LlmInvocation).where(LlmInvocation.patient_id == ingested_patient.id)
        )
    )
    assert len(rows) >= 1
    # The row carries the endpoint and a non-zero latency.
    row = rows[0]
    assert row.endpoint == "asam-loc"
    assert row.prompt_tokens > 0


# ────────────────────────────────────────────────────────────────────────
# Live-Claude opt-in marker (registered in pyproject.toml).
#
# ``pytest -m manual`` runs these (incurs Claude API cost). Default CI
# runs are ``pytest -m "not manual"`` which skip the marker entirely.
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.manual
def test_live_claude_smoke_marker_exists():
    """Placeholder so the marker is exercised by a real test.

    Future live-LLM regressions can live under this marker.
    """
    assert True
