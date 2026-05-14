"""
M5 acceptance tests for the storage substrate.

Proves that the migrated schema accepts a Patient + Encounter +
ClinicalDocument insert, that the FHIR-shaped JSONB columns round-trip, and
that ``Patient.fhir_json`` validates against the ``fhir.resources`` R4 model
(PRD §5 — every relational row carries a FHIR-shaped sibling).

Full internal-model -> FHIR mapping for every resource type is M7; this test
proves the *substrate* — columns, foreign keys, JSONB round-trip, and the
idempotency constraint — is sound.
"""

import hashlib
from datetime import date, datetime

import pytest
from fhir.resources.patient import Patient as FhirPatient
from sqlalchemy.exc import IntegrityError

from app.db.models import ClinicalDocument, Encounter, Patient


def test_patient_encounter_document_insert_and_fhir_roundtrip(db_session):
    """Insert the three core rows, read them back, and confirm the stored
    ``Patient.fhir_json`` still validates as a FHIR R4 Patient."""
    # A minimal but valid FHIR R4 Patient resource.
    fhir_patient = FhirPatient(
        name=[{"family": "Reyes", "given": ["Marcus"]}],
        gender="male",
        birthDate=date(1991, 11, 4),
    )

    patient = Patient(
        external_id="SP-TEST-0001",
        given_name="Marcus",
        family_name="Reyes",
        birth_date=date(1991, 11, 4),
        gender="male",
        fhir_json=fhir_patient.model_dump(mode="json"),
    )
    db_session.add(patient)
    db_session.flush()  # assigns patient.id without committing

    encounter = Encounter(
        patient_id=patient.id,
        period_start=datetime(2026, 5, 4, 13, 30),
        class_code="IMP",  # FHIR v3-ActCode: inpatient encounter
        type_code="intake",
        fhir_json={"resourceType": "Encounter", "status": "finished"},
    )
    db_session.add(encounter)
    db_session.flush()

    raw_text = "[BPS] Intake Assessment\n1. Presenting Problem: ..."
    document = ClinicalDocument(
        patient_id=patient.id,
        encounter_id=encounter.id,
        document_type="bps_intake",
        external_id="931333130",
        authored_on=datetime(2026, 5, 4, 13, 30),
        author_name="Dr. Aisha Patel, MD",
        author_role="psychiatrist",
        raw_text=raw_text,
        content_hash=hashlib.sha256(raw_text.encode()).hexdigest(),
        sections={"presenting_problem": {"text": "...", "char_span": [0, 3]}},
        fhir_document_reference={
            "resourceType": "DocumentReference",
            "status": "current",
        },
    )
    db_session.add(document)
    db_session.flush()

    # Read everything back through the ORM.
    fetched_patient = db_session.get(Patient, patient.id)
    assert fetched_patient is not None
    assert fetched_patient.external_id == "SP-TEST-0001"

    fetched_doc = db_session.get(ClinicalDocument, document.id)
    assert fetched_doc is not None
    # JSONB columns round-trip structurally.
    assert fetched_doc.sections["presenting_problem"]["char_span"] == [0, 3]
    # The BPS intake is not a progress note, so it carries no ClinicalImpression.
    assert fetched_doc.fhir_clinical_impression is None

    # The stored fhir_json still validates as a FHIR R4 Patient.
    revalidated = FhirPatient(**fetched_patient.fhir_json)
    assert revalidated.gender == "male"
    assert revalidated.name[0].family == "Reyes"


def test_content_hash_uniqueness_enforced(db_session):
    """Re-inserting a document with a duplicate ``content_hash`` is rejected by
    the database — the backstop for the ingest idempotency guarantee (PRD §7)."""
    patient = Patient(
        external_id="SP-TEST-0002",
        given_name="Marcus",
        family_name="Reyes",
        birth_date=date(1991, 11, 4),
        gender="male",
    )
    db_session.add(patient)
    db_session.flush()

    encounter = Encounter(
        patient_id=patient.id,
        period_start=datetime(2026, 5, 4, 13, 30),
        class_code="IMP",
        type_code="intake",
    )
    db_session.add(encounter)
    db_session.flush()

    def make_doc() -> ClinicalDocument:
        return ClinicalDocument(
            patient_id=patient.id,
            encounter_id=encounter.id,
            document_type="bps_intake",
            authored_on=datetime(2026, 5, 4, 13, 30),
            author_name="Dr. Aisha Patel, MD",
            author_role="psychiatrist",
            raw_text="identical text",
            content_hash="duplicate-hash-value",
        )

    db_session.add(make_doc())
    db_session.flush()

    db_session.add(make_doc())
    with pytest.raises(IntegrityError):
        db_session.flush()
