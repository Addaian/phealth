"""
M8 acceptance tests for the read API surface.

Exercises the six patient read endpoints (PRD §5.4) against a freshly ingested
synthetic chart, asserting each returns a well-shaped 200 and that an unknown
patient id yields a 404.

Uses the ``api_client`` fixture (a TestClient whose ``get_session`` is
overridden to the test's rolled-back transaction) and the ``ingested_patient``
fixture (clears + ingests the synthetic export within that transaction).
"""

import uuid


def test_get_patient(api_client, ingested_patient):
    """GET /api/v1/patients/{id} returns demographics plus the FHIR Patient resource."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(ingested_patient.id)
    assert body["family_name"] == "Reyes"
    assert body["fhir"]["resourceType"] == "Patient"


def test_get_patient_intake(api_client, ingested_patient):
    """GET /api/v1/patients/{id}/intake returns the BPS raw text and all 21 sections."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/intake")
    assert response.status_code == 200
    body = response.json()
    assert body["raw_text"].strip()
    assert len(body["sections"]) == 21
    # every section carries text and a char-offset span
    for section in body["sections"].values():
        assert section["text"] and len(section["char_span"]) == 2


def test_get_patient_timeline(api_client, ingested_patient):
    """GET /api/v1/patients/{id}/timeline returns the 3 progress notes, oldest first."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/timeline")
    assert response.status_code == 200
    timeline = response.json()
    assert len(timeline) == 3
    assert [entry["type"] for entry in timeline] == ["soap", "dap", "dsap"]
    dates = [entry["date"] for entry in timeline]
    assert dates == sorted(dates)  # oldest first


def test_get_patient_observations(api_client, ingested_patient):
    """GET /api/v1/patients/{id}/observations returns the flat extracted-observation list."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/observations")
    assert response.status_code == 200
    observations = response.json()
    assert len(observations) > 0
    # all 7 LOINC-coded scales are represented, each with a valid provenance span
    loinc_codes = {o["code"] for o in observations if o["code_system"] == "LOINC"}
    assert len(loinc_codes) == 7
    for observation in observations:
        assert observation["char_start"] < observation["char_end"]


def test_get_patient_asam_evidence(api_client, ingested_patient):
    """GET /api/v1/patients/{id}/asam-evidence returns evidence grouped by all 6 dimensions."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/asam-evidence")
    assert response.status_code == 200
    dimensions = response.json()
    assert {d["dimension"] for d in dimensions} == {1, 2, 3, 4, 5, 6}
    for dimension in dimensions:
        assert dimension["name"]
        assert len(dimension["evidence"]) >= 1


def test_get_patient_tjc_coverage(api_client, ingested_patient):
    """GET /api/v1/patients/{id}/tjc-coverage returns the EP matrix with the intentional gaps."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/tjc-coverage")
    assert response.status_code == 200
    coverage = response.json()
    gap_codes = {row["ep_code"] for row in coverage if row["status"] == "gap"}
    expected_gaps = {"CTS.03.01.09", "CTS.03.01.03", "NPSG.15.01.01", "R3-25", "RC.01.02.01"}
    assert expected_gaps <= gap_codes
    assert all(row["title"] and row["rationale"] for row in coverage)


def test_read_endpoints_404_for_unknown_patient(api_client, db_session):
    """Every patient-scoped read endpoint 404s an unknown patient id."""
    unknown = uuid.uuid4()
    for suffix in ("", "/intake", "/timeline", "/observations", "/asam-evidence", "/tjc-coverage"):
        response = api_client.get(f"/api/v1/patients/{unknown}{suffix}")
        assert response.status_code == 404, f"/api/v1/patients/{{id}}{suffix} should 404"
