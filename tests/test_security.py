"""
Tests for the X-API-Key auth dependency on state-changing endpoints.

Phase 1 guarded only the ``POST /ingest/*`` route with ``require_api_key``;
Phase 2 extends auth to the reads (M1). These tests pin the rejection
behaviour: a missing key and a wrong key each yield a clean **401**, not the
422-for-missing-required-header FastAPI default. (The clean 401 comes from
``Header(default=None)`` in ``app/core/security.py`` rather than
``Header(...)``, which is the documented design choice there.)
"""

from io import BytesIO


def test_ingest_zip_rejects_missing_api_key(client):
    """A POST without any X-API-Key header is rejected with 401."""
    response = client.post(
        "/ingest/simplepractice-zip",
        files={"file": ("empty.zip", BytesIO(b""), "application/zip")},
    )
    assert response.status_code == 401


def test_ingest_zip_rejects_wrong_api_key(client):
    """A POST with the wrong X-API-Key value is rejected with 401."""
    response = client.post(
        "/ingest/simplepractice-zip",
        headers={"X-API-Key": "definitely-not-the-key"},
        files={"file": ("empty.zip", BytesIO(b""), "application/zip")},
    )
    assert response.status_code == 401


def test_api_v1_read_rejects_missing_api_key(client):
    """An /api/v1/* GET without X-API-Key is rejected with 401."""
    response = client.get("/api/v1/patients/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 401


def test_fhir_bundle_endpoint_rejects_missing_api_key(client):
    """The fhir-bundle endpoint also requires X-API-Key (Phase 2 §5.11)."""
    response = client.get("/api/v1/patients/00000000-0000-0000-0000-000000000000/fhir-bundle")
    assert response.status_code == 401
