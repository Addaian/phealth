"""
M5 acceptance tests for the Joint Commission audit predicates.

Two flavors of coverage:

  1. **Live Marcus chart** (the demo-defining tests): asserts the 13
     findings the PRD requires, the 5 planted-gap linkages (G1-G5), and
     the 7/5/1/0 summary distribution.
  2. **Synthetic snapshot tests** for the raw-row predicates so each
     branch is exercised independently of the Phase 1 ingester.

The TjcCoverage-backed predicates (5 of the 13) are exercised via the
live test rather than synthetic snapshots, because faking a
TjcCoverage row in a unit test would re-test the Phase 1 builder
rather than the audit predicate itself.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from app.clinical.tjc.audit_functions import (
    TjcSnapshot,
    audit_co_signature_consistency,
    audit_discharge_planning,
    audit_entry_authentication,
    audit_history_and_physical_24h,
    audit_measurement_based_care,
    audit_moud_for_oud,
    audit_record_timeliness,
    audit_risk_mitigation_plan,
    audit_sud_history_collection,
    audit_suicide_reassessment,
    audit_suicide_screening,
    audit_treatment_plan_alignment,
    audit_withdrawal_risk_assessment,
)
from app.clinical.tjc.ep_catalog import EP_CATALOG
from app.clinical.tjc.runner import run_audit, summarize
from app.db.models import ClinicalDocument, ExtractedObservation, Patient

# ─── Helpers ─────────────────────────────────────────────────────────────


def _patient(gender: str = "male") -> Patient:
    return Patient(
        id=uuid4(),
        external_id="TJC-TEST",
        given_name="Marcus",
        family_name="Reyes",
        birth_date=date(1991, 11, 4),
        gender=gender,
    )


def _doc(
    patient_id, doc_type: str = "bps_intake", when: datetime | None = None
) -> ClinicalDocument:
    return ClinicalDocument(
        id=uuid4(),
        patient_id=patient_id,
        encounter_id=uuid4(),
        document_type=doc_type,
        authored_on=when or datetime(2026, 5, 4),
        author_name="Dr Test",
        author_role="psychiatrist",
        raw_text="lorem ipsum dolor sit amet",
        content_hash=f"hash-{uuid4()}",
    )


def _obs(document_id, code_system: str, code: str, **kw) -> ExtractedObservation:
    return ExtractedObservation(
        id=uuid4(),
        document_id=document_id,
        code_system=code_system,
        code=code,
        display=f"{code_system}:{code}",
        char_start=0,
        char_end=10,
        extraction_method="regex",
        confidence=1.0,
        **kw,
    )


def _empty_snap(patient: Patient | None = None) -> TjcSnapshot:
    """Return a TjcSnapshot with no documents/observations/coverage."""
    return TjcSnapshot(
        patient=patient or _patient(),
        documents=[],
        observations=[],
        coverage_rows=[],
    )


# ─── Raw-row predicate unit tests ────────────────────────────────────────


def test_sud_history_satisfied_when_dx_and_substance_entities_present():
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[
            _obs(doc.id, "ICD-10", "F10.232"),
            _obs(doc.id, "internal", "substance:alcohol"),
        ],
        coverage_rows=[],
    )
    finding = audit_sud_history_collection(snap)
    assert finding.status == "satisfied"
    assert finding.negative_finding is False


def test_sud_history_gap_when_dx_present_but_no_substance_entities():
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_obs(doc.id, "ICD-10", "F10.232")],
        coverage_rows=[],
    )
    finding = audit_sud_history_collection(snap)
    assert finding.status == "gap"
    assert finding.negative_finding is True


def test_withdrawal_risk_satisfied_with_ciwa():
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_obs(doc.id, "LOINC", "72109-3", value_quantity=12)],
        coverage_rows=[],
    )
    assert audit_withdrawal_risk_assessment(snap).status == "satisfied"


def test_withdrawal_risk_satisfied_with_cows():
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_obs(doc.id, "LOINC", "92103-9")],
        coverage_rows=[],
    )
    assert audit_withdrawal_risk_assessment(snap).status == "satisfied"


def test_withdrawal_risk_gap_when_no_scale():
    assert audit_withdrawal_risk_assessment(_empty_snap()).status == "gap"


def test_h_and_p_24h_satisfied_when_bps_intake_present():
    patient = _patient()
    snap = TjcSnapshot(
        patient=patient,
        documents=[_doc(patient.id, doc_type="bps_intake")],
        observations=[],
        coverage_rows=[],
    )
    finding = audit_history_and_physical_24h(snap)
    assert finding.status == "satisfied"
    assert finding.evidence_pointers  # carries a citation


def test_h_and_p_24h_gap_when_no_intake():
    patient = _patient()
    snap = TjcSnapshot(
        patient=patient,
        documents=[_doc(patient.id, doc_type="soap")],  # progress note, not intake
        observations=[],
        coverage_rows=[],
    )
    assert audit_history_and_physical_24h(snap).status == "gap"


def test_moud_not_applicable_when_no_oud():
    """Marcus's case: no F11 diagnosis -> not_applicable."""
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_obs(doc.id, "ICD-10", "F10.232")],  # alcohol, not opioid
        coverage_rows=[],
    )
    finding = audit_moud_for_oud(snap)
    assert finding.status == "not_applicable"
    assert finding.linked_planted_gap is None


def test_moud_satisfied_when_oud_and_moud_med():
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[
            _obs(doc.id, "ICD-10", "F11.20"),
            _obs(doc.id, "internal", "medication:buprenorphine"),
        ],
        coverage_rows=[],
    )
    assert audit_moud_for_oud(snap).status == "satisfied"


def test_moud_gap_when_oud_but_no_moud():
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_obs(doc.id, "ICD-10", "F11.20")],
        coverage_rows=[],
    )
    assert audit_moud_for_oud(snap).status == "gap"


def test_suicide_screening_satisfied_with_cssrs():
    patient = _patient()
    doc = _doc(patient.id)
    snap = TjcSnapshot(
        patient=patient,
        documents=[doc],
        observations=[_obs(doc.id, "LOINC", "93373-7", value_string="passive ideation")],
        coverage_rows=[],
    )
    assert audit_suicide_screening(snap).status == "satisfied"


def test_suicide_screening_gap_without_cssrs():
    assert audit_suicide_screening(_empty_snap()).status == "gap"


def test_risk_mitigation_satisfied_with_cssrs_and_progress_note():
    patient = _patient()
    intake = _doc(patient.id, doc_type="bps_intake")
    soap = _doc(patient.id, doc_type="soap")
    snap = TjcSnapshot(
        patient=patient,
        documents=[intake, soap],
        observations=[_obs(intake.id, "LOINC", "93373-7")],
        coverage_rows=[],
    )
    assert audit_risk_mitigation_plan(snap).status == "satisfied"


def test_entry_authentication_gap_when_author_missing():
    patient = _patient()
    doc = _doc(patient.id)
    doc.author_name = ""  # simulate missing signature
    snap = TjcSnapshot(patient=patient, documents=[doc], observations=[], coverage_rows=[])
    assert audit_entry_authentication(snap).status == "gap"


def test_entry_authentication_satisfied_when_all_signed():
    patient = _patient()
    snap = TjcSnapshot(
        patient=patient,
        documents=[_doc(patient.id), _doc(patient.id, doc_type="soap")],
        observations=[],
        coverage_rows=[],
    )
    assert audit_entry_authentication(snap).status == "satisfied"


def test_entry_authentication_ambiguous_when_no_documents():
    assert audit_entry_authentication(_empty_snap()).status == "ambiguous"


def test_record_timeliness_satisfied_with_documents():
    patient = _patient()
    snap = TjcSnapshot(
        patient=patient, documents=[_doc(patient.id)], observations=[], coverage_rows=[]
    )
    assert audit_record_timeliness(snap).status == "satisfied"


def test_record_timeliness_ambiguous_without_documents():
    assert audit_record_timeliness(_empty_snap()).status == "ambiguous"


# ─── Live Marcus chart -- the demo-defining test ─────────────────────────


def test_ep_catalog_has_thirteen_entries():
    """phase_3_PRD.md §5.5 lists exactly 13 EPs."""
    assert len(EP_CATALOG) == 13


def test_marcus_audit_returns_thirteen_findings(ingested_patient, db_session):
    findings = run_audit(ingested_patient.id, db_session)
    assert len(findings) == 13


def test_marcus_audit_surfaces_all_five_planted_gaps(ingested_patient, db_session):
    """The PRD §8 success metric M3: all 5 planted gaps surfaced via
    ``linked_planted_gap``."""
    findings = run_audit(ingested_patient.id, db_session)
    planted = {f.linked_planted_gap for f in findings if f.linked_planted_gap}
    assert planted == {"G1", "G2", "G3", "G4", "G5"}


def test_marcus_audit_summary_distribution(ingested_patient, db_session):
    """phase_3_PRD.md §6.2 example: 7 satisfied, 5 gap, 1 n/a, 0 ambiguous."""
    findings = run_audit(ingested_patient.id, db_session)
    summary = summarize(findings)
    assert summary == {
        "total_eps_audited": 13,
        "satisfied": 7,
        "gap": 5,
        "not_applicable": 1,
        "ambiguous": 0,
        "overall_status": "non-compliant",
    }


def test_marcus_negative_findings_set_flag_correctly(ingested_patient, db_session):
    """G1, G3, G4 are absence-of-evidence findings; ``negative_finding``
    must be True so the LLM narrates them with the "Documentation review
    did not identify X" template (phase_3_PRD.md §5.6).

    Note: negative findings MAY still carry a Phase 1 citation pointing
    to positive *context* (e.g. the single PHQ-9 administration that did
    happen, contextualizing the absence of repeats). phase_3_PRD.md §6.2
    explicitly shows G1 with a citation. The ``negative_finding`` flag
    drives narration register, not the presence of context citations.
    """
    findings = run_audit(ingested_patient.id, db_session)
    negatives = [f for f in findings if f.negative_finding]
    assert {f.linked_planted_gap for f in negatives} == {"G1", "G3", "G4"}


def test_marcus_g1_carries_context_citation(ingested_patient, db_session):
    """phase_3_PRD.md §6.2 explicitly shows G1 carrying a citation to
    the single PHQ-9 administration (positive context for the absence).
    This is the bug-check test for the negative-finding-citation issue."""
    findings = run_audit(ingested_patient.id, db_session)
    g1 = next(f for f in findings if f.linked_planted_gap == "G1")
    assert g1.negative_finding is True
    assert len(g1.evidence_pointers) == 1, (
        "G1 must carry a context citation to the single PHQ-9 admin"
    )


def test_marcus_treatment_plan_gap_g2_is_positive_finding(ingested_patient, db_session):
    """G2 (treatment-plan misalignment) is a positive finding -- a peer
    note exists that isn't on the plan -- so it MAY carry a citation to
    the offending note."""
    findings = run_audit(ingested_patient.id, db_session)
    g2 = next(f for f in findings if f.linked_planted_gap == "G2")
    assert g2.negative_finding is False
    assert g2.status == "gap"


def test_marcus_each_finding_has_required_fields(ingested_patient, db_session):
    """Every EpFinding must carry the M9-narration-required fields."""
    findings = run_audit(ingested_patient.id, db_session)
    for finding in findings:
        assert finding.ep_code, f"missing ep_code: {finding}"
        assert finding.ep_domain, f"missing ep_domain: {finding}"
        assert finding.status in {"satisfied", "gap", "not_applicable", "ambiguous"}
        assert finding.severity in {"high", "moderate", "low", "none"}
        assert finding.finding_template, f"missing finding_template: {finding.ep_code}"


# ─── TjcCoverage-backed predicates -- explicit per-gap mapping ───────────


def test_g1_links_to_measurement_based_care(ingested_patient, db_session):
    findings = run_audit(ingested_patient.id, db_session)
    g1_findings = [f for f in findings if f.linked_planted_gap == "G1"]
    assert len(g1_findings) == 1
    assert g1_findings[0].ep_code == "CTS.03.01.09"


def test_g2_links_to_treatment_plan_alignment(ingested_patient, db_session):
    findings = run_audit(ingested_patient.id, db_session)
    g2_findings = [f for f in findings if f.linked_planted_gap == "G2"]
    assert len(g2_findings) == 1
    assert g2_findings[0].ep_code == "CTS.03.01.03 EP 28"


def test_g3_links_to_suicide_reassessment(ingested_patient, db_session):
    findings = run_audit(ingested_patient.id, db_session)
    g3_findings = [f for f in findings if f.linked_planted_gap == "G3"]
    assert len(g3_findings) == 1
    assert g3_findings[0].ep_code == "NPSG.15.01.01 EP 3"


def test_g4_links_to_discharge_planning(ingested_patient, db_session):
    findings = run_audit(ingested_patient.id, db_session)
    g4_findings = [f for f in findings if f.linked_planted_gap == "G4"]
    assert len(g4_findings) == 1
    assert g4_findings[0].ep_code == "CTS.04.02 (TOC)"


def test_g5_links_to_co_signature_consistency(ingested_patient, db_session):
    findings = run_audit(ingested_patient.id, db_session)
    g5_findings = [f for f in findings if f.linked_planted_gap == "G5"]
    assert len(g5_findings) == 1
    assert g5_findings[0].ep_code == "RC.01.02.01 EP 3"


# ─── Unit test for the individual TjcCoverage-backed predicates ──────────


def test_measurement_based_care_ambiguous_without_coverage_row():
    """When the Phase 1 TjcCoverage row is missing entirely, the
    predicate falls back to ambiguous rather than crashing."""
    finding = audit_measurement_based_care(_empty_snap())
    assert finding.status == "ambiguous"


def test_treatment_plan_alignment_ambiguous_without_coverage_row():
    finding = audit_treatment_plan_alignment(_empty_snap())
    assert finding.status == "ambiguous"


def test_discharge_planning_ambiguous_without_coverage_row():
    finding = audit_discharge_planning(_empty_snap())
    assert finding.status == "ambiguous"


def test_suicide_reassessment_ambiguous_without_coverage_row():
    finding = audit_suicide_reassessment(_empty_snap())
    assert finding.status == "ambiguous"


def test_co_signature_consistency_ambiguous_without_coverage_row():
    finding = audit_co_signature_consistency(_empty_snap())
    assert finding.status == "ambiguous"
