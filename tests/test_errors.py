"""
Tests for the unified exception handler (Phase 2 PRD §5.8).

Asserts that the same Python exception surfaces in two distinct content types
depending on which namespace was hit:

- ``/api/v1/*`` and unprefixed routes -> RFC 7807 ``application/problem+json``
- ``/fhir/*``                          -> FHIR ``application/fhir+json``
                                          OperationOutcome

The ``/fhir`` route table is empty until M8/M9, so the OperationOutcome tests
exercise Starlette's auto-generated 404 for unmounted paths -- proving the
dispatcher fires on framework-internal raises, not just our own.
"""

import uuid


def test_api_v1_not_found_returns_problem_details(api_client):
    """A 404 from PatientDep is wrapped in an RFC 7807 envelope."""
    unknown_id = uuid.uuid4()
    response = api_client.get(f"/api/v1/patients/{unknown_id}/intake")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert str(unknown_id) in body["detail"]
    assert body["instance"] == f"/api/v1/patients/{unknown_id}/intake"


def test_api_v1_validation_error_returns_problem_details(api_client):
    """A 422 from path coercion (UUID parse) surfaces the validation list."""
    response = api_client.get("/api/v1/patients/not-a-uuid")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    assert body["status"] == 422
    assert body["title"] == "Unprocessable Entity"
    assert isinstance(body["errors"], list)
    assert body["errors"], "expected at least one validation error entry"
    # Each entry carries the safe subset projected by _validation_exception_handler.
    first = body["errors"][0]
    assert "loc" in first and "msg" in first and "type" in first


def test_api_v1_unauthorized_returns_problem_details(client):
    """A 401 from require_api_key is also wrapped (the unauthenticated client)."""
    response = client.get("/api/v1/patients")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")

    body = response.json()
    assert body["status"] == 401
    assert body["title"] == "Unauthorized"
    assert body["instance"] == "/api/v1/patients"


def test_fhir_unmounted_path_returns_operation_outcome(api_client):
    """Starlette's framework-internal 404 still dispatches to OperationOutcome.

    ``/fhir/$everything`` and ``/fhir/Patient/{id}/$everything`` are M9 work,
    so this path hits Starlette's router fallback. Verifies the dispatcher
    branches on the *request path*, not on where the exception was raised.
    Uses ``api_client`` (auth-bypassed) to ensure the 404 comes from the
    framework's route table, not from ``require_api_key``.
    """
    response = api_client.get(f"/fhir/Patient/{uuid.uuid4()}/$everything")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/fhir+json")

    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["severity"] == "error"
    assert body["issue"][0]["code"] == "not-found"


def test_fhir_read_404_returns_operation_outcome(api_client):
    """A 404 from a mounted /fhir/* read returns OperationOutcome, not problem+json."""
    response = api_client.get(f"/fhir/Patient/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/fhir+json")
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "not-found"


def test_unprefixed_path_defaults_to_problem_details(client):
    """Routes outside both prefixes (e.g. /nope) get the RFC 7807 default."""
    response = client.get("/nope")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404
