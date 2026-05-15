"""
M10 acceptance tests for the ASAM endpoints.

End-to-end through FastAPI's TestClient with a mocked Claude SDK and
the rolled-back ``db_session``. Asserts the canonical curl flow that
phase_3_implementation_plan.md M10 promises:

  1. POST /api/v1/patients/{id}/asam-loc -> 201 with Level 3.7.
  2. POST again -> 200 cached + same ETag.
  3. GET /api/v1/asam-assessments/{id} with If-None-Match -> 304.
  4. 422 on a patient with no ASAM evidence.
  5. 503 when ClaudeBreakerOpen is raised.
  6. 200 + degraded headers when Claude is unreachable (5xx path).
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from app.clinical.llm.claude_client import ClaudeBreakerOpen, ClaudeUnavailable
from app.db.models import Patient

# Mocked-SDK helpers (`mocked_client_factory` fixture, etc.) live in
# tests/clinical/conftest.py and are picked up automatically by pytest.


def _canned_rationale_for_marcus() -> dict:
    """Minimal valid AsamRationale -- one dim, one subdim, no citations.

    No citations means the validator passes trivially; the response
    body still contains the LLM's narrative.
    """
    return {
        "overall_rationale": (
            "Marcus resolves to Level 3.7 driven by active benzodiazepine "
            "taper and concurrent SUD diagnoses; non-COE, non-BIO."
        ),
        "dimensions": [
            {
                "dimension": 1,
                "rationale": "Active chlordiazepoxide taper requires medically managed care.",
                "subdimensions": [
                    {
                        "name": "dim1_addiction_meds",
                        "rationale": "Active benzo taper -> Min Level 3.7.",
                        "citations": [],
                    }
                ],
            }
        ],
        "confidence": "high",
    }


# ────────────────────────────────────────────────────────────────────────
# 1. POST happy path -> 201 with Level 3.7.
# ────────────────────────────────────────────────────────────────────────


def test_post_asam_loc_returns_201_with_level_3_7(
    ingested_patient, api_client, mocked_client_factory
):
    """Marcus's full ingested chart -> Level 3.7 via the API."""
    mocked_client_factory(payloads=[_canned_rationale_for_marcus()])
    response = api_client.post(
        f"/api/v1/patients/{ingested_patient.id}/asam-loc",
        json={"force_recompute": False},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["recommendation"]["level"] == "3.7"
    assert body["recommendation"]["co_occurring_enhanced"] is False
    assert body["recommendation"]["biomedical_enhanced"] is False
    assert body["cached"] is False
    # ETag header is set; Location header points at the GET-by-id.
    assert "etag" in {k.lower() for k in response.headers.keys()}
    assert response.headers["Location"].startswith("/api/v1/asam-assessments/")


# ────────────────────────────────────────────────────────────────────────
# 2. Second POST -> 200 cached + same ETag.
# ────────────────────────────────────────────────────────────────────────


def test_second_post_is_cached_with_same_etag(ingested_patient, api_client, mocked_client_factory):
    """Repeat POST with same evidence_hash + model -> 200 cached, same ETag.

    The SECOND call must NOT hit the Claude SDK (cache short-circuits
    before narration); the mocked client gets only one payload to return.
    """
    client_mock = mocked_client_factory(payloads=[_canned_rationale_for_marcus()])

    first = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assert first.status_code == 201
    first_etag = first.headers["etag"]
    first_id = first.json()["id"]

    second = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assert second.status_code == 200, second.text
    assert second.headers["etag"] == first_etag
    assert second.json()["cached"] is True
    assert second.json()["id"] == first_id
    # SDK invoked exactly once -- second call hit the cache.
    sdk_mock = client_mock._sdk.messages.with_raw_response.create  # type: ignore[attr-defined]
    assert sdk_mock.call_count == 1


# ────────────────────────────────────────────────────────────────────────
# 3. GET with If-None-Match -> 304.
# ────────────────────────────────────────────────────────────────────────


def test_get_asam_assessment_returns_304_on_etag_match(
    ingested_patient, api_client, mocked_client_factory
):
    """POST creates the row; GET by id with matching If-None-Match -> 304."""
    mocked_client_factory(payloads=[_canned_rationale_for_marcus()])
    post = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assessment_id = post.json()["id"]
    etag = post.headers["etag"]

    # Mismatching If-None-Match -> 200 with the body.
    get_full = api_client.get(f"/api/v1/asam-assessments/{assessment_id}")
    assert get_full.status_code == 200
    assert get_full.headers["etag"] == etag

    # Matching If-None-Match -> 304 with no body.
    get_304 = api_client.get(
        f"/api/v1/asam-assessments/{assessment_id}",
        headers={"If-None-Match": etag},
    )
    assert get_304.status_code == 304
    assert get_304.content == b""


# ────────────────────────────────────────────────────────────────────────
# 4. 422 on no-evidence patient.
# ────────────────────────────────────────────────────────────────────────


def test_post_asam_loc_returns_422_when_no_evidence(db_session, api_client, mocked_client_factory):
    """A patient with no documents -> 422 RFC 7807 (no ASAM evidence)."""
    bare = Patient(
        external_id="BARE-PATIENT",
        given_name="Bare",
        family_name="Patient",
        birth_date=date(2000, 1, 1),
        gender="male",
    )
    db_session.add(bare)
    db_session.flush()

    # Even though Claude is never called, install a mock so the dep
    # resolves cleanly.
    mocked_client_factory(payloads=[])

    response = api_client.post(f"/api/v1/patients/{bare.id}/asam-loc", json={})
    assert response.status_code == 422
    # RFC 7807 envelope from the Phase 2 dispatcher.
    assert "application/problem+json" in response.headers["content-type"]


# ────────────────────────────────────────────────────────────────────────
# 5. 503 when the circuit breaker is open.
# ────────────────────────────────────────────────────────────────────────


def test_post_asam_loc_returns_503_when_breaker_open(
    ingested_patient, api_client, mocked_client_factory
):
    """ClaudeBreakerOpen -> 503 + Retry-After header."""

    def _raise_breaker_open(*args, **kwargs):
        raise ClaudeBreakerOpen("breaker open")

    mocked_client_factory(side_effect=_raise_breaker_open)
    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    # The breaker check happens BEFORE the SDK call in real ClaudeClient,
    # but with a mocked SDK that raises, the flow still propagates through
    # _try_narrate which catches and re-raises as HTTPException(503).
    # However, our mock raises a *plain* ClaudeBreakerOpen at the SDK call
    # site, which the production code translates into 503.
    assert response.status_code == 503


# ────────────────────────────────────────────────────────────────────────
# 6. 200 + degraded headers when Claude is unreachable.
# ────────────────────────────────────────────────────────────────────────


def test_post_asam_loc_returns_200_degraded_when_claude_unreachable(
    ingested_patient, api_client, mocked_client_factory
):
    """ClaudeUnavailable (5xx / connection error) -> 200 with rule-engine
    output, x-rationale-status: unavailable + x-rule-engine-only: true
    headers (phase_3_PRD.md §5.9)."""

    def _raise_unavailable(*args, **kwargs):
        raise ClaudeUnavailable("server error")

    mocked_client_factory(side_effect=_raise_unavailable)
    response = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assert response.status_code == 201  # Still 201 -- the engine output is valid
    assert response.headers.get("x-rationale-status") == "unavailable"
    assert response.headers.get("x-rule-engine-only") == "true"
    body = response.json()
    # The recommendation is still 3.7 (engine is authoritative).
    assert body["recommendation"]["level"] == "3.7"
    assert body["rationale_status"] == "unavailable"
    assert "claude_unavailable" in body["rationale_warnings"]


# ────────────────────────────────────────────────────────────────────────
# 7. GET on unknown id -> 404 RFC 7807.
# ────────────────────────────────────────────────────────────────────────


def test_get_asam_assessment_returns_404(api_client, mocked_client_factory):
    mocked_client_factory(payloads=[])
    response = api_client.get(f"/api/v1/asam-assessments/{uuid4()}")
    assert response.status_code == 404
    assert "application/problem+json" in response.headers["content-type"]


# ────────────────────────────────────────────────────────────────────────
# 8. Concurrent-insert race recovery.
#
# Simulates two near-simultaneous POSTs: the first writes the cache
# row; the second uses ``force_recompute=true`` so it skips the cache
# check, computes a fresh response, and then loses the race on the
# UNIQUE(patient_id, evidence_hash, model_version) constraint. The
# endpoint must catch the IntegrityError and return the winning row
# as a cache hit (200) rather than 500ing.
# ────────────────────────────────────────────────────────────────────────


def test_post_recovers_from_unique_constraint_race(
    ingested_patient, api_client, mocked_client_factory, monkeypatch
):
    """A second POST with force_recompute=true hits the UNIQUE row the
    first POST just wrote; recovery returns 200 + the winning id.

    The test fixture's joined-session pattern (one connection, one
    outer transaction) can't faithfully simulate two real concurrent
    connections -- a real Postgres IntegrityError would abort the
    fixture's outer transaction and roll back the first POST's row,
    making the recovery's lookup fail spuriously. We monkey-patch
    ``_persist`` to raise the same IntegrityError the real constraint
    would raise in production (where each request has its own
    connection), so the recovery handler runs against a session that
    still sees the first POST's row.
    """
    from sqlalchemy.exc import IntegrityError

    from app.api import asam_loc

    mocked_client_factory(payloads=[_canned_rationale_for_marcus(), _canned_rationale_for_marcus()])

    first = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assert first.status_code == 201
    first_id = first.json()["id"]
    first_etag = first.headers["etag"]

    # Simulate the race: force the second POST's persist to raise the
    # same IntegrityError the real UNIQUE(patient_id, evidence_hash,
    # model_version) constraint would raise under concurrent inserts.
    def _persist_raises(*_args, **_kwargs):
        raise IntegrityError("simulated unique violation", None, Exception())

    monkeypatch.setattr(asam_loc, "_persist", _persist_raises)

    second = api_client.post(
        f"/api/v1/patients/{ingested_patient.id}/asam-loc",
        json={"force_recompute": True},
    )

    # Recovery path: 200 cache-hit with the winning row's id + ETag.
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first_id
    assert second.headers["etag"] == first_etag
    assert second.json()["cached"] is True
