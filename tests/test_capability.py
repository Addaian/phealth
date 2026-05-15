"""
Tests for /fhir/metadata (CapabilityStatement) and /fhir/Patient/{id}/$everything
(Phase 2 PRD §5.3 + M9).

Two contracts:
  1. ``/fhir/metadata`` validates against ``fhir.resources.R4B.CapabilityStatement``
     and accurately enumerates the served surface (Patient, DocumentReference,
     Observation, Provenance) with their supported interactions + search params.
  2. ``/fhir/Patient/{id}/$everything`` returns a Bundle.searchset whose entry
     count equals (1 Patient) + (4 Encounter) + (4 DocumentReference) +
     (3 ClinicalImpression) + (9 Observation) = 21 for Marcus, in Phase 2 M9.
     M10 will add 9 Provenance entries to bring the total to 30.
"""

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.capabilitystatement import CapabilityStatement

# ---------------------------------------------------------------------------
# /fhir/metadata
# ---------------------------------------------------------------------------


def test_metadata_returns_valid_capability_statement(client):
    """Auth-free per FHIR R4 spec; validates against CapabilityStatement."""
    response = client.get("/fhir/metadata")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")

    statement = CapabilityStatement.model_validate(response.json())
    assert statement.status == "active"
    assert statement.fhirVersion == "4.0.1"
    assert "json" in statement.format


def test_metadata_is_auth_free(client):
    """/fhir/metadata must work without X-API-Key (FHIR R4 spec requirement)."""
    response = client.get("/fhir/metadata")
    assert response.status_code == 200


def test_metadata_enumerates_every_served_resource(client):
    """rest[0].resource lists Patient, DocumentReference, Observation, Provenance."""
    response = client.get("/fhir/metadata")
    statement = CapabilityStatement.model_validate(response.json())
    types = {resource.type for resource in statement.rest[0].resource}
    assert types == {"Patient", "DocumentReference", "Observation", "Provenance"}


def test_metadata_advertises_patient_everything_operation(client):
    """Patient resource entry advertises the $everything operation."""
    statement = CapabilityStatement.model_validate(client.get("/fhir/metadata").json())
    patient_resource = next(r for r in statement.rest[0].resource if r.type == "Patient")
    assert patient_resource.operation is not None
    assert any(op.name == "everything" for op in patient_resource.operation)


def test_metadata_documents_search_params(client):
    """DocumentReference / Observation entries list their supported searchParam names."""
    statement = CapabilityStatement.model_validate(client.get("/fhir/metadata").json())
    by_type = {r.type: r for r in statement.rest[0].resource}

    doc_search = {param.name for param in (by_type["DocumentReference"].searchParam or [])}
    assert {"patient", "type", "_count"}.issubset(doc_search)

    obs_search = {param.name for param in (by_type["Observation"].searchParam or [])}
    assert {"patient", "code", "_count"}.issubset(obs_search)


# ---------------------------------------------------------------------------
# /fhir/Patient/{id}/$everything
# ---------------------------------------------------------------------------


def test_patient_everything_returns_searchset_bundle(api_client, ingested_patient):
    """The $everything operation returns a Bundle.searchset."""
    response = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")

    bundle = Bundle.model_validate(response.json())
    assert bundle.type == "searchset"


def test_patient_everything_entry_count_matches_chart(api_client, ingested_patient):
    """Marcus's $everything: 1 Patient + 4 Encounter + 4 DocumentReference
    + 3 ClinicalImpression + 9 Observation + 9 Provenance = 30 entries.
    """
    response = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything")
    body = response.json()
    Bundle.model_validate(body)  # asserts spec-conformance; the count check uses raw dicts

    # The fhir.resources validated instance has ``resource_type`` as a class
    # variable on each subclass, not an instance attribute; walking the raw
    # JSON ``resourceType`` keys is simpler and equally correct.
    by_type: dict[str, int] = {}
    for entry in body["entry"]:
        rt = entry["resource"]["resourceType"]
        by_type[rt] = by_type.get(rt, 0) + 1

    assert by_type.get("Patient") == 1
    assert by_type.get("Encounter") == 4
    assert by_type.get("DocumentReference") == 4
    assert by_type.get("ClinicalImpression") == 3
    assert by_type.get("Observation") == 9
    assert by_type.get("Provenance") == 9
    assert body["total"] == sum(by_type.values()) == 30


def test_patient_everything_orders_patient_first(api_client, ingested_patient):
    """Parent before children: Patient is entry[0]."""
    body = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything").json()
    Bundle.model_validate(body)  # spec-conformance
    assert body["entry"][0]["resource"]["resourceType"] == "Patient"


def test_patient_everything_unknown_patient_returns_operation_outcome(api_client):
    """Unknown patient → 404 OperationOutcome (via PatientDep + M3 handler)."""
    import uuid

    response = api_client.get(f"/fhir/Patient/{uuid.uuid4()}/$everything")
    assert response.status_code == 404
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
