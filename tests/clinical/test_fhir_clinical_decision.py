"""
M11 acceptance tests for the Phase 3 FHIR resources.

  * ``to_clinical_impression`` + ``to_detected_issue`` mappers produce
    R4-valid resources (round-tripped through fhir.resources.R4B).
  * ``GET /fhir/ClinicalImpression?subject=...`` returns a searchset
    Bundle with one entry per persisted ASAM assessment.
  * ``GET /fhir/DetectedIssue?subject=...`` returns a Bundle with one
    DetectedIssue per gap finding (5 for Marcus).
  * ``GET /fhir/metadata`` advertises both new resources.

These tests rely on the M10 POST endpoints to seed assessment / audit
rows, then exercise the FHIR surface against the persisted data. The
mocked-SDK helpers live in ``test_endpoints_asam.py`` /
``test_endpoints_tjc.py``; this file duplicates the smaller pieces it
needs rather than importing across test modules (pytest discourages
cross-test imports).
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.capabilitystatement import CapabilityStatement
from fhir.resources.R4B.clinicalimpression import ClinicalImpression
from fhir.resources.R4B.detectedissue import DetectedIssue

from app.clinical.tjc.runner import run_audit
from app.db.models import AsamAssessment, Patient, TjcAuditResult
from app.fhir.mappers import (
    ASAM_LEVEL_SYSTEM,
    TJC_EP_SYSTEM,
    build_capability_statement,
    to_clinical_impression,
    to_detected_issue,
)

# Mocked-SDK fixtures (`mocked_client_factory`) live in
# tests/clinical/conftest.py.


def _asam_payload_for_marcus() -> dict:
    return {
        "overall_rationale": "Marcus resolves to Level 3.7.",
        "dimensions": [
            {
                "dimension": 1,
                "rationale": "Active benzo taper.",
                "subdimensions": [
                    {
                        "name": "dim1_addiction_meds",
                        "rationale": "Active taper.",
                        "citations": [],
                    }
                ],
            }
        ],
        "confidence": "high",
    }


def _tjc_payload_for_findings(findings) -> dict:
    return {
        "findings": [
            {
                "ep_code": f.ep_code,
                "narrative": f"Standard {f.ep_code}: {f.finding_template}",
                "citations": [],
            }
            for f in findings
        ]
    }


# ────────────────────────────────────────────────────────────────────────
# Unit tests for the pure mappers.
# ────────────────────────────────────────────────────────────────────────


def _stub_patient() -> Patient:
    """Minimal in-memory Patient for mapper unit tests (no DB write)."""
    from datetime import date

    return Patient(
        external_id="STUB",
        given_name="Stub",
        family_name="Patient",
        birth_date=date(1991, 1, 1),
        gender="male",
    )


def test_to_clinical_impression_produces_valid_r4_resource():
    """The mapper must produce a resource that re-parses through fhir.resources.R4B."""
    patient = _stub_patient()
    assessment = AsamAssessment(
        id=uuid4(),
        patient_id=patient.id,
        evidence_hash="abc123",
        recommended_level="3.7",
        modifiers=[],
        dimensions={
            "_level_display": "Medically Monitored Intensive Inpatient",
            "_dimensions_payload": [],
        },
        rules_fired=["Rule 1", "Rule 2"],
        rationale="overall narrative",
        confidence="high",
        rationale_status="ok",
        rationale_warnings=[],
        model_version="claude-sonnet-4-6",
        computed_at=datetime(2026, 5, 15, 14, 22, 8),
    )
    ci = to_clinical_impression(assessment, patient)
    # Re-validate via the model itself.
    revalidated = ClinicalImpression.model_validate(
        ci.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    assert revalidated.status == "completed"
    assert revalidated.subject.reference == f"Patient/{patient.id}"
    # Finding carries the recommended level under the custom CodeSystem.
    finding = revalidated.finding[0]
    assert finding.itemCodeableConcept.coding[0].system == ASAM_LEVEL_SYSTEM
    assert finding.itemCodeableConcept.coding[0].code == "3.7"


def test_to_clinical_impression_handles_missing_rules_fired():
    """No rules_fired -> investigation list is empty (FHIR-valid)."""
    patient = _stub_patient()
    assessment = AsamAssessment(
        id=uuid4(),
        patient_id=patient.id,
        evidence_hash="x",
        recommended_level="3.7",
        modifiers=[],
        dimensions={"_level_display": "x"},
        rules_fired=[],
        rationale="x",
        confidence="high",
        rationale_status="ok",
        rationale_warnings=[],
        model_version="claude-sonnet-4-6",
        computed_at=datetime(2026, 5, 15),
    )
    ci = to_clinical_impression(assessment, patient)
    # An empty investigation list is valid; the field is optional.
    assert ci.investigation in (None, [])


def test_to_detected_issue_produces_valid_r4_resource():
    """Mapper produces R4-valid DetectedIssue for a gap finding."""
    patient = _stub_patient()
    audit = TjcAuditResult(
        id=uuid4(),
        patient_id=patient.id,
        evidence_hash="x",
        findings=[],
        summary={},
        rationale_status="ok",
        rationale_warnings=[],
        model_version="claude-sonnet-4-6",
        computed_at=datetime(2026, 5, 15),
    )
    finding = {
        "ep_code": "CTS.03.01.09",
        "ep_domain": "Measurement-based care",
        "status": "gap",
        "severity": "high",
        "negative_finding": True,
        "narrative": "Standard CTS.03.01.09: not met. Documentation review...",
        "citations": [],
        "linked_planted_gap": "G1",
    }
    di = to_detected_issue(finding, audit, patient)
    revalidated = DetectedIssue.model_validate(
        di.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    assert revalidated.status == "final"
    assert revalidated.code.coding[0].system == TJC_EP_SYSTEM
    assert revalidated.code.coding[0].code == "CTS.03.01.09"
    assert revalidated.severity == "high"
    assert revalidated.patient.reference == f"Patient/{patient.id}"
    # Stable id format: audit_id-ep_slug.
    assert revalidated.id.startswith(str(audit.id))


def test_to_detected_issue_collapses_none_severity_to_low():
    """Engine 'none' severity -> FHIR 'low' (DetectedIssue has no informational severity)."""
    patient = _stub_patient()
    audit = TjcAuditResult(
        id=uuid4(),
        patient_id=patient.id,
        evidence_hash="x",
        findings=[],
        summary={},
        rationale_status="ok",
        rationale_warnings=[],
        model_version="claude-sonnet-4-6",
        computed_at=datetime(2026, 5, 15),
    )
    finding = {
        "ep_code": "RC.01.03.01",
        "ep_domain": "Timeliness",
        "status": "satisfied",
        "severity": "none",
        "negative_finding": False,
        "narrative": "ok",
        "citations": [],
        "linked_planted_gap": None,
    }
    di = to_detected_issue(finding, audit, patient)
    assert di.severity == "low"


# ────────────────────────────────────────────────────────────────────────
# CapabilityStatement advertises both new resources.
# ────────────────────────────────────────────────────────────────────────


def test_capability_statement_lists_clinical_impression_and_detected_issue():
    """phase_3_PRD.md §5.10: both Phase 3 resources advertised."""
    cs = build_capability_statement()
    # Round-trip through the validator to catch shape regressions.
    revalidated = CapabilityStatement.model_validate(
        cs.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    declared = {r.type for r in revalidated.rest[0].resource}
    assert "ClinicalImpression" in declared
    assert "DetectedIssue" in declared


# ────────────────────────────────────────────────────────────────────────
# Live FHIR endpoints: search by subject, read by id, Bundle validation.
# ────────────────────────────────────────────────────────────────────────


def test_search_clinical_impression_returns_bundle_for_persisted_assessment(
    ingested_patient, api_client, mocked_client_factory
):
    """POST /asam-loc creates a row; GET /fhir/ClinicalImpression?subject=...
    surfaces it as a FHIR Bundle entry."""
    mocked_client_factory(payloads=[_asam_payload_for_marcus()])
    api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})

    response = api_client.get(f"/fhir/ClinicalImpression?subject=Patient/{ingested_patient.id}")
    assert response.status_code == 200
    bundle = Bundle.model_validate(response.json())
    assert bundle.type == "searchset"
    assert bundle.total == 1
    entry = bundle.entry[0]
    # ``model_dump`` projects with ``resourceType`` (camelCase) per FHIR wire format.
    assert entry.resource.model_dump(by_alias=True)["resourceType"] == "ClinicalImpression"
    assert entry.resource.subject.reference == f"Patient/{ingested_patient.id}"


def test_read_clinical_impression_by_id(ingested_patient, api_client, mocked_client_factory):
    """The POST's id is stable; GET /fhir/ClinicalImpression/{id} returns it."""
    mocked_client_factory(payloads=[_asam_payload_for_marcus()])
    post = api_client.post(f"/api/v1/patients/{ingested_patient.id}/asam-loc", json={})
    assessment_id = post.json()["id"]

    response = api_client.get(f"/fhir/ClinicalImpression/{assessment_id}")
    assert response.status_code == 200
    resource = ClinicalImpression.model_validate(response.json())
    assert resource.id == assessment_id


def test_search_detected_issue_returns_five_gaps_for_marcus(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    """Marcus's TJC audit -> 5 DetectedIssue resources in the search Bundle."""
    findings = run_audit(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_tjc_payload_for_findings(findings)])
    api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})

    response = api_client.get(
        f"/fhir/DetectedIssue?subject=Patient/{ingested_patient.id}&category=tjc-audit"
    )
    assert response.status_code == 200
    bundle = Bundle.model_validate(response.json())
    assert bundle.type == "searchset"
    # 5 gap findings (G1-G5); satisfied/n-a findings are excluded by design.
    assert bundle.total == 5
    for entry in bundle.entry:
        di = DetectedIssue.model_validate(entry.resource.model_dump(mode="json"))
        assert di.status == "final"
        assert di.severity in {"high", "moderate", "low"}


def test_search_detected_issue_with_unknown_category_is_empty(
    ingested_patient, db_session, api_client, mocked_client_factory
):
    """Forward-compat: a category other than 'tjc-audit' yields an empty Bundle."""
    findings = run_audit(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_tjc_payload_for_findings(findings)])
    api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})

    response = api_client.get(
        f"/fhir/DetectedIssue?subject=Patient/{ingested_patient.id}&category=other"
    )
    assert response.status_code == 200
    bundle = Bundle.model_validate(response.json())
    assert bundle.total == 0


def test_read_detected_issue_by_id(ingested_patient, db_session, api_client, mocked_client_factory):
    """Round-trip: search for issues, pick one, GET by id."""
    findings = run_audit(ingested_patient.id, db_session)
    mocked_client_factory(payloads=[_tjc_payload_for_findings(findings)])
    api_client.post(f"/api/v1/patients/{ingested_patient.id}/tjc-audit", json={})

    bundle_resp = api_client.get(
        f"/fhir/DetectedIssue?subject=Patient/{ingested_patient.id}&category=tjc-audit"
    )
    bundle = Bundle.model_validate(bundle_resp.json())
    issue_id = bundle.entry[0].resource.id

    response = api_client.get(f"/fhir/DetectedIssue/{issue_id}")
    assert response.status_code == 200
    di = DetectedIssue.model_validate(response.json())
    assert di.id == issue_id
