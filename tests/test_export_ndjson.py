"""
Tests for the synchronous NDJSON ``$export`` stub (Phase 2 PRD §11 / M13).

The FHIR Bulk Data IG (https://hl7.org/fhir/uv/bulkdata/) defines a multi-step
async export: kick-off → status poll → manifest → ND-JSON file URLs. The MVP
ships a synchronous variant that goes straight to the resource stream so a
reviewer can ``curl ... -o out.ndjson`` and inspect the wire shape. Production
deployment would slot in the kick-off + status pattern without changing the
per-resource line format -- those bytes are the IG's contract.

Three contracts under test:
  1. Response media type is ``application/fhir+ndjson`` with an
     ``attachment`` disposition (file-download semantics).
  2. Each line of the body is valid JSON for a FHIR resource (every resource
     round-trips via ``fhir.resources.R4B``).
  3. The set of resources matches the ``$everything`` Bundle (same data, just
     a different wire shape).
"""

import json

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.clinicalimpression import ClinicalImpression
from fhir.resources.R4B.documentreference import DocumentReference
from fhir.resources.R4B.encounter import Encounter as FhirEncounter
from fhir.resources.R4B.observation import Observation
from fhir.resources.R4B.patient import Patient as FhirPatient
from fhir.resources.R4B.provenance import Provenance

# Map resourceType -> fhir.resources model, for line-by-line validation.
_VALIDATORS = {
    "Patient": FhirPatient,
    "Encounter": FhirEncounter,
    "DocumentReference": DocumentReference,
    "ClinicalImpression": ClinicalImpression,
    "Observation": Observation,
    "Provenance": Provenance,
}


def test_export_returns_ndjson_media_type(api_client, ingested_patient):
    """The response advertises application/fhir+ndjson with attachment disposition."""
    response = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+ndjson")
    assert "attachment" in response.headers.get("content-disposition", "")


def test_export_each_line_is_valid_fhir_resource(api_client, ingested_patient):
    """Every line parses as JSON and validates against its FHIR resource class."""
    response = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$export")
    lines = [line for line in response.text.split("\n") if line]
    assert lines, "expected at least one NDJSON line"

    for line in lines:
        resource = json.loads(line)
        validator = _VALIDATORS.get(resource["resourceType"])
        assert validator is not None, (
            f"unexpected resourceType in NDJSON stream: {resource['resourceType']}"
        )
        validator.model_validate(resource)


def test_export_resource_counts_match_everything(api_client, ingested_patient):
    """NDJSON $export and the $everything Bundle contain the same resource set.

    The two endpoints are deliberately equivalent in content -- they differ
    only in framing (Bundle vs. line-delimited stream). This test asserts the
    equivalence so a future divergence is loud, not silent.
    """
    response = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$export")
    ndjson_counts: dict[str, int] = {}
    for line in response.text.splitlines():
        if not line:
            continue
        resource = json.loads(line)
        rt = resource["resourceType"]
        ndjson_counts[rt] = ndjson_counts.get(rt, 0) + 1

    bundle_body = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything").json()
    Bundle.model_validate(bundle_body)  # spec-conformance
    bundle_counts: dict[str, int] = {}
    for entry in bundle_body["entry"]:
        rt = entry["resource"]["resourceType"]
        bundle_counts[rt] = bundle_counts.get(rt, 0) + 1

    assert ndjson_counts == bundle_counts
    assert sum(ndjson_counts.values()) == 30  # Marcus's chart


def test_export_404s_for_unknown_patient(api_client):
    """Unknown patient → 404 OperationOutcome (via PatientDep + M3 handler)."""
    import uuid

    response = api_client.get(f"/fhir/Patient/{uuid.uuid4()}/$export")
    assert response.status_code == 404
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"


def test_capability_statement_advertises_export(client):
    """/fhir/metadata lists the Patient/$export operation (M13)."""
    from fhir.resources.R4B.capabilitystatement import CapabilityStatement

    statement = CapabilityStatement.model_validate(client.get("/fhir/metadata").json())
    patient_resource = next(r for r in statement.rest[0].resource if r.type == "Patient")
    operation_names = {op.name for op in (patient_resource.operation or [])}
    assert "export" in operation_names
