"""
M7 acceptance tests for the FHIR mappers and the Bundle endpoint.

Asserts the M7 acceptance criteria (implementation plan §M7):
  * the Bundle round-trips through the ``fhir.resources`` parsers;
  * every Bundle entry's ``fullUrl`` is unique;
  * the Bundle contains 1 Patient, >=1 Encounter, 4 DocumentReference,
    >=3 ClinicalImpression, and one Observation per LOINC-coded extracted
    scale result;
  * the ``fhir_json`` columns the orchestrator stored are valid FHIR R4;
  * GET /api/v1/patients/{id}/fhir-bundle serves the Bundle (404 for an unknown id).

Runs inside the rolled-back ``db_session`` via the ``ingested_patient`` fixture.
The endpoint tests use the ``api_client`` fixture, which overrides ``get_session``
to point at the test transaction *and* makes ``require_api_key`` a no-op.
"""

import uuid
from collections import Counter

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.clinicalimpression import ClinicalImpression
from fhir.resources.R4B.documentreference import DocumentReference
from fhir.resources.R4B.encounter import Encounter as FhirEncounter
from fhir.resources.R4B.patient import Patient as FhirPatient
from sqlmodel import select

from app.db.models import ClinicalDocument, Encounter, ExtractedObservation
from app.fhir.mappers import build_patient_bundle


def test_patient_bundle_round_trips_and_has_expected_resources(db_session, ingested_patient):
    """The assembled Bundle re-parses as FHIR and has the expected resource counts."""
    patient = ingested_patient
    encounters = list(
        db_session.exec(select(Encounter).where(Encounter.patient_id == patient.id)).all()
    )
    documents = list(
        db_session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == patient.id)
        ).all()
    )
    observations = list(
        db_session.exec(
            select(ExtractedObservation).where(ExtractedObservation.code_system == "LOINC")
        ).all()
    )

    bundle = build_patient_bundle(patient, encounters, documents, observations)
    dumped = bundle.model_dump(mode="json", by_alias=True, exclude_none=True)

    # Round-trips through the fhir.resources parser.
    Bundle.model_validate(dumped)

    # Every fullUrl is unique.
    full_urls = [entry["fullUrl"] for entry in dumped["entry"]]
    assert len(full_urls) == len(set(full_urls))

    # Expected resource counts (implementation plan §M7).
    counts = Counter(entry["resource"]["resourceType"] for entry in dumped["entry"])
    assert counts["Patient"] == 1
    assert counts["Encounter"] >= 1
    assert counts["DocumentReference"] == 4
    assert counts["ClinicalImpression"] >= 3
    assert counts["Observation"] == len(observations)


def test_stored_fhir_columns_are_valid_fhir(db_session, ingested_patient):
    """The fhir_json columns the orchestrator populated validate as FHIR R4."""
    FhirPatient.model_validate(ingested_patient.fhir_json)

    for encounter in db_session.exec(select(Encounter)).all():
        FhirEncounter.model_validate(encounter.fhir_json)

    for document in db_session.exec(select(ClinicalDocument)).all():
        DocumentReference.model_validate(document.fhir_document_reference)
        if document.document_type == "bps_intake":
            # The BPS intake is not a progress note -- no ClinicalImpression.
            assert document.fhir_clinical_impression is None
        else:
            ClinicalImpression.model_validate(document.fhir_clinical_impression)


def test_fhir_bundle_endpoint_serves_the_bundle(api_client, ingested_patient):
    """GET /api/v1/patients/{id}/fhir-bundle returns the assembled Bundle."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/fhir-bundle")
    assert response.status_code == 200
    body = response.json()
    assert body["resourceType"] == "Bundle"
    assert body["type"] == "transaction"
    Bundle.model_validate(body)  # the served JSON is valid FHIR


def test_fhir_bundle_endpoint_404_for_unknown_patient(api_client):
    """An unknown patient id yields a 404."""
    response = api_client.get(f"/api/v1/patients/{uuid.uuid4()}/fhir-bundle")
    assert response.status_code == 404
