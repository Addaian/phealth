"""
M10 acceptance tests for the TJC compliance-audit endpoints.

Mirrors test_endpoints_asam.py. Asserts:

  1. POST /api/v1/patients/{id}/tjc-audit -> 201 with all 5 planted gaps.
  2. POST again -> 200 cached + same ETag.
  3. GET /api/v1/tjc-audits/{id} with If-None-Match -> 304.
  4. 422 on a patient with no documents (no TjcCoverage rows can exist).
  5. 503 when ClaudeBreakerOpen is raised.
  6. 200 + degraded headers when Claude is unreachable.

Reuses the mocked-SDK helpers via the M8 / M10-ASAM test patterns.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.clinical.llm.claude_client import ClaudeBreakerOpen, ClaudeUnavailable
from app.clinical.tjc.runner import run_audit
from app.db.models import Patient

# Mocked-SDK helpers (`mocked_client_factory` fixture, etc.) live in
# tests/clinical/conftest.py.


def _canned_tjc_payload_for(findings) -> dict:
    """One TjcFindingNarration per input EpFinding, no citations."""
    return {
        "findings": [
            {
                "ep_code": f.ep_code,
                "narrative": f"Standard {f.ep_code}: ... {f.finding_template}",
                "citations": [],
            }
            for f in findings
        ]
    }


# ────────────────────────────────────────────────────────────────────────
# 1. POST happy path: 5 planted gaps.
# ────────────────────────────────────────────────────────────────────────


def test_post_tjc_audit_surfaces_five_planted_gaps(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    """The demo-defining test: all 5 planted gaps surface through the API."""
    findings = run_audit(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_canned_tjc_payload_for(findings)])

    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["summary"]["gap"] == 5
    assert body["summary"]["satisfied"] == 7
    assert body["summary"]["not_applicable"] == 1
    assert body["summary"]["overall_status"] == "non-compliant"
    # All 5 planted gaps linked.
    planted = {f["linked_planted_gap"] for f in body["findings"] if f.get("linked_planted_gap")}
    assert planted == {"G1", "G2", "G3", "G4", "G5"}


# ────────────────────────────────────────────────────────────────────────
# 2. Second POST -> 200 cached + same ETag.
# ────────────────────────────────────────────────────────────────────────


def test_second_post_tjc_audit_is_cached(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    findings = run_audit(ingested_patient.id, db_session)
    client_mock = mocked_client_factory(payloads=[_canned_tjc_payload_for(findings)])

    first = api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})
    assert first.status_code == 201
    first_etag = first.headers["etag"]
    first_id = first.json()["id"]

    second = api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})
    assert second.status_code == 200
    assert second.headers["etag"] == first_etag
    assert second.json()["cached"] is True
    assert second.json()["id"] == first_id
    sdk_mock = client_mock._sdk.messages.with_raw_response.create  # type: ignore[attr-defined]
    assert sdk_mock.call_count == 1


# ────────────────────────────────────────────────────────────────────────
# 3. GET with If-None-Match -> 304.
# ────────────────────────────────────────────────────────────────────────


def test_get_tjc_audit_returns_304_on_etag_match(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    findings = run_audit(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_canned_tjc_payload_for(findings)])

    post = api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})
    audit_id = post.json()["id"]
    etag = post.headers["etag"]

    get_full = api_client.get(f"/api/v1/tjc-audits/{audit_id}")
    assert get_full.status_code == 200
    assert get_full.headers["etag"] == etag

    get_304 = api_client.get(f"/api/v1/tjc-audits/{audit_id}", headers={"If-None-Match": etag})
    assert get_304.status_code == 304


# ────────────────────────────────────────────────────────────────────────
# 4. 422 on no-coverage patient.
# ────────────────────────────────────────────────────────────────────────


def test_post_tjc_audit_returns_422_when_no_coverage(db_session, api_client, mocked_client_factory):
    bare = Patient(
        external_id="BARE-TJC",
        given_name="Bare",
        family_name="Patient",
        birth_date=date(2000, 1, 1),
        gender="male",
    )
    db_session.add(bare)
    db_session.flush()

    mocked_client_factory(payloads=[])
    response = api_client.post(f"/api/v1/patients/{bare.id}/tjc-audit", json={})
    assert response.status_code == 422
    assert "application/problem+json" in response.headers["content-type"]


# ────────────────────────────────────────────────────────────────────────
# 5. 503 when breaker is open.
# ────────────────────────────────────────────────────────────────────────


def test_post_tjc_audit_returns_503_when_breaker_open(
    ingested_patient, api_client, mocked_client_factory
):
    def _raise(*args, **kwargs):
        raise ClaudeBreakerOpen("breaker open")

    mocked_client_factory(side_effect=_raise)
    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})
    assert response.status_code == 503


# ────────────────────────────────────────────────────────────────────────
# 6. 200 + degraded headers when Claude unreachable.
# ────────────────────────────────────────────────────────────────────────


def test_post_tjc_audit_returns_201_degraded_when_claude_unreachable(
    ingested_patient, api_client, mocked_client_factory
):
    def _raise(*args, **kwargs):
        raise ClaudeUnavailable("server error")

    mocked_client_factory(side_effect=_raise)
    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})
    # 201 because the engine output is still valid; the rationale is just
    # unavailable.
    assert response.status_code == 201
    assert response.headers.get("x-rationale-status") == "unavailable"
    assert response.headers.get("x-rule-engine-only") == "true"
    body = response.json()
    # Engine findings still surface all 5 planted gaps.
    assert body["summary"]["gap"] == 5
    assert body["rationale_status"] == "unavailable"


# ────────────────────────────────────────────────────────────────────────
# 7. GET on unknown id -> 404.
# ────────────────────────────────────────────────────────────────────────


def test_get_tjc_audit_returns_404(api_client, mocked_client_factory):
    mocked_client_factory(payloads=[])
    response = api_client.get(f"/api/v1/tjc-audits/{uuid4()}")
    assert response.status_code == 404
    assert "application/problem+json" in response.headers["content-type"]
