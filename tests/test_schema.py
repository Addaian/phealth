"""
Acceptance tests for the storage substrate.

Proves that the migrated schema accepts a Patient + Encounter +
ClinicalDocument insert, that the FHIR-shaped JSONB columns round-trip, and
that ``Patient.fhir_json`` validates against the ``fhir.resources`` R4 model
(PRD §5 — every relational row carries a FHIR-shaped sibling).

Full internal-model -> FHIR mapping for every resource type is M7; this test
proves the *substrate* — columns, foreign keys, JSONB round-trip, and the
idempotency constraint — is sound.

Phase 3 (documents/phase_3_PRD.md §6.3) appended three tables; the tests
below also prove their JSONB columns round-trip and that the
``(patient_id, evidence_hash, model_version)`` uniqueness constraint on the
two cache tables is enforced at the DB level.
"""

import hashlib
from datetime import date, datetime

import pytest
from fhir.resources.patient import Patient as FhirPatient
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    AsamAssessment,
    ClinicalDocument,
    Encounter,
    LlmInvocation,
    Patient,
    TjcAuditResult,
)


def _make_patient(external_id: str) -> Patient:
    """Minimal Patient with required fields populated — for tests that just
    need a Patient.id to point a Phase 3 row at."""
    return Patient(
        external_id=external_id,
        given_name="Marcus",
        family_name="Reyes",
        birth_date=date(1991, 11, 4),
        gender="male",
    )


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


# ─── Phase 3 (documents/phase_3_PRD.md §6.3) ──────────────────────────────


def test_asam_assessment_row_round_trips(db_session):
    """Insert one AsamAssessment with realistic Phase 3 payload, read it
    back, and confirm every JSONB column survives the round trip."""
    patient = _make_patient("SP-TEST-ASAM-1")
    db_session.add(patient)
    db_session.flush()

    assessment = AsamAssessment(
        patient_id=patient.id,
        evidence_hash="8f3a2c1b9e7d4a06",  # xxhash64 hex shape
        recommended_level="3.7",
        modifiers=[],
        # Truncated to one dimension — full shape lives in phase_3_PRD.md §6.1.
        dimensions={
            "1": {
                "name": "Intoxication, Withdrawal, Addiction Medications",
                "subdimensions": [
                    {"name": "Withdrawal", "rating": "3A", "min_level": "3.7"},
                ],
                "rationale": "CIWA-Ar 12 + concurrent dependence on alcohol and benzos.",
                "confidence": "high",
            }
        },
        rules_fired=[
            "Medically managed care required (Dim 1 = 3A → Min Level 3.7)",
            "Any subdimension Min Level 3 → recommend Level 3.7",
        ],
        rationale="Overall narration (≤120 words in the live response).",
        confidence="high",
        rationale_status="ok",
        rationale_warnings=[],
        model_version="claude-sonnet-4-6",
        computed_at=datetime(2026, 5, 15, 14, 22, 8),
    )
    db_session.add(assessment)
    db_session.flush()

    fetched = db_session.get(AsamAssessment, assessment.id)
    assert fetched is not None
    assert fetched.recommended_level == "3.7"
    assert fetched.modifiers == []
    assert fetched.dimensions["1"]["subdimensions"][0]["rating"] == "3A"
    assert len(fetched.rules_fired) == 2
    assert fetched.rationale_warnings == []
    assert fetched.model_version == "claude-sonnet-4-6"


def test_asam_assessment_cache_key_uniqueness_enforced(db_session):
    """The UNIQUE(patient_id, evidence_hash, model_version) constraint backs
    the Phase 3 idempotency guarantee — two POSTs with the same inputs hit
    the cache, not two rows (phase_3_PRD.md §5.7)."""
    patient = _make_patient("SP-TEST-ASAM-2")
    db_session.add(patient)
    db_session.flush()

    def make_row() -> AsamAssessment:
        return AsamAssessment(
            patient_id=patient.id,
            evidence_hash="duplicate-hash-for-test",
            recommended_level="3.7",
            modifiers=[],
            dimensions={},
            rules_fired=[],
            rationale="...",
            confidence="high",
            rationale_status="ok",
            rationale_warnings=[],
            model_version="claude-sonnet-4-6",
            computed_at=datetime(2026, 5, 15, 14, 0, 0),
        )

    db_session.add(make_row())
    db_session.flush()

    db_session.add(make_row())
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_asam_assessment_model_swap_creates_new_cache_namespace(db_session):
    """Same patient + same evidence_hash, different model_version → two
    rows allowed. Reviewers can A/B between Sonnet 4.6 and Opus 4.7 outputs
    by swapping ``PHEALTH_LLM_MODEL`` (phase_3_PRD.md §5.7, Q5)."""
    patient = _make_patient("SP-TEST-ASAM-3")
    db_session.add(patient)
    db_session.flush()

    common_kwargs = dict(
        patient_id=patient.id,
        evidence_hash="shared-evidence-hash",
        recommended_level="3.7",
        modifiers=[],
        dimensions={},
        rules_fired=[],
        rationale="...",
        confidence="high",
        rationale_status="ok",
        rationale_warnings=[],
        computed_at=datetime(2026, 5, 15, 14, 0, 0),
    )

    db_session.add(AsamAssessment(**common_kwargs, model_version="claude-sonnet-4-6"))
    db_session.add(AsamAssessment(**common_kwargs, model_version="claude-opus-4-7"))
    db_session.flush()  # both rows persist; the model column differentiates the cache namespace


def test_tjc_audit_result_row_round_trips(db_session):
    """Insert one TjcAuditResult, read it back, confirm the JSONB columns
    (findings list + summary dict) round-trip."""
    patient = _make_patient("SP-TEST-TJC-1")
    db_session.add(patient)
    db_session.flush()

    audit = TjcAuditResult(
        patient_id=patient.id,
        evidence_hash="9b1c4f2e8d3a5067",
        # One finding shown; the live response carries 13 (phase_3_PRD.md §6.2).
        findings=[
            {
                "ep_code": "CTS.03.01.09",
                "ep_domain": "Measurement-based care",
                "status": "gap",
                "severity": "high",
                "negative_finding": True,
                "narrative": "Standard CTS.03.01.09: not met. ...",
                "citations": [],
                "linked_planted_gap": "G1",
            }
        ],
        summary={
            "total_eps_audited": 13,
            "satisfied": 7,
            "gap": 5,
            "not_applicable": 1,
            "overall_status": "non-compliant",
        },
        rationale_status="ok",
        rationale_warnings=[],
        model_version="claude-sonnet-4-6",
        computed_at=datetime(2026, 5, 15, 14, 30, 0),
    )
    db_session.add(audit)
    db_session.flush()

    fetched = db_session.get(TjcAuditResult, audit.id)
    assert fetched is not None
    assert fetched.summary["gap"] == 5
    assert fetched.findings[0]["linked_planted_gap"] == "G1"


def test_llm_invocation_row_round_trips(db_session):
    """Insert one LlmInvocation log row and read it back. No FK to patient,
    so no Patient row is required (phase_3_PRD.md §5.8 — audit retained
    after patient deletion)."""
    import uuid

    invocation = LlmInvocation(
        endpoint="asam-loc",
        patient_id=uuid.uuid4(),  # any UUID; no FK to patient by design
        model="claude-sonnet-4-6",
        prompt_tokens=6_000,
        completion_tokens=2_500,
        cache_read_tokens=0,
        cache_creation_tokens=0,
        latency_ms=9_500,
        response_hash="abcd1234ef567890",
        citation_validation_passed=True,
        occurred_at=datetime(2026, 5, 15, 14, 22, 17),
    )
    db_session.add(invocation)
    db_session.flush()

    fetched = db_session.get(LlmInvocation, invocation.id)
    assert fetched is not None
    assert fetched.endpoint == "asam-loc"
    assert fetched.prompt_tokens == 6_000
    assert fetched.citation_validation_passed is True
    assert fetched.error_class is None  # success path
