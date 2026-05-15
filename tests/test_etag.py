"""
Tests for the ETag / If-None-Match middleware (Phase 2 PRD §5.7).

Asserts the three invariants of the contract:
1. Every JSON 200 GET carries a weak ETag.
2. The ETag is stable across repeat requests with the same payload.
3. A repeat request that echoes the ETag in ``If-None-Match`` short-circuits
   to 304 Not Modified with an empty body.

Also verifies error responses are *not* ETagged -- caching a 4xx would mask a
later fix and contradicts the body-shape contract from M3.
"""

import uuid


def test_get_returns_etag_header(api_client, ingested_patient):
    """Every successful JSON GET carries a weak ETag."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}")
    assert response.status_code == 200

    etag = response.headers.get("etag")
    assert etag is not None, "expected ETag header on a 200 JSON GET"
    assert etag.startswith('W/"') and etag.endswith('"'), f"expected weak-ETag form, got {etag!r}"


def test_etag_is_stable_across_requests(api_client, ingested_patient):
    """Same payload → same ETag (the determinism contract)."""
    first = api_client.get(f"/api/v1/patients/{ingested_patient.id}")
    second = api_client.get(f"/api/v1/patients/{ingested_patient.id}")

    assert first.headers["etag"] == second.headers["etag"]


def test_if_none_match_returns_304(api_client, ingested_patient):
    """A repeat request echoing the ETag short-circuits to 304 with no body."""
    first = api_client.get(f"/api/v1/patients/{ingested_patient.id}")
    etag = first.headers["etag"]

    second = api_client.get(
        f"/api/v1/patients/{ingested_patient.id}",
        headers={"If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.content == b""
    # RFC 7232 §4.1 requires the ETag is echoed on 304.
    assert second.headers.get("etag") == etag


def test_if_none_match_mismatch_returns_200(api_client, ingested_patient):
    """A wrong ETag must not short-circuit -- the client should get the body."""
    response = api_client.get(
        f"/api/v1/patients/{ingested_patient.id}",
        headers={"If-None-Match": 'W/"deadbeefdeadbeef"'},
    )
    assert response.status_code == 200
    assert response.content  # full body returned


def test_error_responses_are_not_etagged(api_client):
    """A 404 carries no ETag -- caching errors would mask later fixes."""
    response = api_client.get(f"/api/v1/patients/{uuid.uuid4()}/intake")
    assert response.status_code == 404
    assert "etag" not in {k.lower() for k in response.headers}
