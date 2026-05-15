"""
Generate the Phase 3 sample JSON artifacts for the submission.

Uses the FastAPI TestClient with a mocked Claude SDK to produce
deterministic ``examples/marcus_reyes_asam_admission.json`` +
``examples/marcus_reyes_tjc.json``. The mocked rationale is hand-tuned
to read like real surveyor RFI prose so reviewers see a representative
sample even without a live Claude call.

Run:
    python scripts/generate_sample_json.py

Why not the live API? The live samples drift on every Claude call
(Sonnet 4.6 is not bit-stable at temperature=0). For the submission
artifact we want a stable, reviewable file -- one that exercises the
full pipeline (rule engine + response shape + FHIR mapping) without
re-running Claude every time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import anthropic
import httpx
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

import app.api.deps_clinical as deps_clinical
from app.clinical.llm.claude_client import ClaudeClient
from app.clinical.tjc.runner import run_audit
from app.core.security import require_api_key
from app.db.models import (
    AsamAssessment,
    AsamEvidence,
    ClinicalDocument,
    LlmInvocation,
    Patient,
    TjcAuditResult,
)
from app.db.session import engine
from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


# ────────────────────────────────────────────────────────────────────────
# Canned Claude payloads -- hand-tuned to demonstrate the response shape.
# ────────────────────────────────────────────────────────────────────────


def _asam_payload(citation_dict: dict) -> dict:
    """A realistic AsamRationale for Marcus's admission."""
    return {
        "overall_rationale": (
            "Marcus Reyes presents with a moderate alcohol withdrawal syndrome "
            "(CIWA-Ar = 12 on admission) compounded by concurrent benzodiazepine "
            "use disorder, requiring medically managed withdrawal monitoring and "
            "an active chlordiazepoxide taper. The deterministic rule engine "
            "resolves these findings to Level 3.7 (Medically Monitored Intensive "
            "Inpatient) per Chapter 10 rules. Co-occurring depressive and anxious "
            "symptoms (PHQ-9 = 18, GAD-7 = 15) with passive ideation only are "
            "appropriate for a standard co-occurring-capable program, so the "
            "recommendation is non-COE and non-BIO."
        ),
        "dimensions": [
            {
                "dimension": 1,
                "rationale": (
                    "Active benzodiazepine taper plus moderate alcohol withdrawal "
                    "with concurrent sedative-hypnotic use disorder requires "
                    "medically managed inpatient care."
                ),
                "subdimensions": [
                    {
                        "name": "dim1_withdrawal",
                        "rationale": (
                            "Latest CIWA-Ar = 2 reflects stabilization on the "
                            "current regimen; admission rating remains 3A given "
                            "the concurrent benzo dependence and after-hours "
                            "monitoring need."
                        ),
                        "citations": [citation_dict],
                    },
                    {
                        "name": "dim1_addiction_meds",
                        "rationale": (
                            "Active chlordiazepoxide taper maps to anchor C "
                            "(Min Level 3.7) for medication-needs."
                        ),
                        "citations": [],
                    },
                ],
            },
            {
                "dimension": 3,
                "rationale": (
                    "Passive ideation without active SI / plan / intent on the "
                    "C-SSRS qualifies for standard co-occurring-capable care."
                ),
                "subdimensions": [
                    {
                        "name": "dim3_active_psych",
                        "rationale": (
                            "PHQ-9 = 18 (moderately severe) + GAD-7 = 15 (severe) "
                            "with passive ideation only -> non-COE 1B (Min 1.7)."
                        ),
                        "citations": [],
                    }
                ],
            },
        ],
        "confidence": "high",
    }


def _tjc_payload(findings) -> dict:
    """A realistic TjcAuditResponse for Marcus's 13-EP audit."""
    surveyor_templates = {
        "gap": "Standard {ep}: not met. Documentation review revealed {tmpl}",
        "satisfied": "Standard {ep}: met. {tmpl}",
        "not_applicable": "Standard {ep}: not applicable to this patient. {tmpl}",
        "ambiguous": "Standard {ep}: ambiguous. {tmpl}",
    }
    return {
        "findings": [
            {
                "ep_code": f.ep_code,
                "narrative": surveyor_templates.get(f.status, "{ep}: {tmpl}").format(
                    ep=f.ep_code, tmpl=f.finding_template
                ),
                "citations": [],
            }
            for f in findings
        ]
    }


# ────────────────────────────────────────────────────────────────────────
# Mocked Claude SDK + injection.
# ────────────────────────────────────────────────────────────────────────


def _fake_message(payload: dict):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=6000,
            output_tokens=2500,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        content=[
            SimpleNamespace(
                type="tool_use",
                name="submit_structured_response",
                input=payload,
                id="t",
            )
        ],
        id="msg_sample",
        model_dump_json=lambda: '{"id":"msg_sample"}',
    )


def _fake_raw(message):
    return SimpleNamespace(
        parse=lambda: message,
        headers=httpx.Headers({"anthropic-ratelimit-requests-remaining": "999"}),
    )


def _install_mocked_client(payloads: list[dict], session: Session) -> None:
    """Override get_claude_client with one returning the canned payloads."""
    sdk = anthropic.Anthropic(api_key="sample-script")
    responses = [_fake_raw(_fake_message(p)) for p in payloads]
    sdk.messages.with_raw_response.create = MagicMock(side_effect=responses)  # type: ignore[method-assign]
    client = ClaudeClient(sdk_client=sdk, session_factory=lambda: iter([session]))
    app.dependency_overrides[deps_clinical.get_claude_client] = lambda: client


def _clear_phase3_cache(session: Session) -> None:
    """Clear cached assessments + LlmInvocation rows so the sample run
    triggers a fresh compute (mocked Claude, deterministic output)."""
    session.exec(delete(AsamAssessment))
    session.exec(delete(TjcAuditResult))
    session.exec(delete(LlmInvocation))
    session.commit()


def main() -> int:
    with Session(engine) as session:
        patient = session.exec(select(Patient)).first()
        if patient is None:
            print("ERROR: No patient ingested. Run docker compose + ingest first.")
            return 1
        # Pick a real AsamEvidence citation so the validator passes.
        evidence_row = session.exec(
            select(AsamEvidence)
            .join(ClinicalDocument, AsamEvidence.document_id == ClinicalDocument.id)
            .where(ClinicalDocument.patient_id == patient.id)
        ).first()
        if evidence_row is None:
            print("ERROR: No AsamEvidence rows. Re-ingest the chart.")
            return 1
        citation_dict = {
            "document_id": str(evidence_row.document_id),
            "char_start": evidence_row.char_start,
            "char_end": evidence_row.char_end,
            "snippet": evidence_row.snippet,
        }
        _clear_phase3_cache(session)
        findings = run_audit(patient.id, session)

    # Override require_api_key + get_claude_client at the app level.
    app.dependency_overrides[require_api_key] = lambda: None
    # Also use a fresh real session for the mocked-client logging so
    # LlmInvocation rows commit to the live DB (acceptable -- the script
    # is a manual artifact generator, not a test).
    with Session(engine) as session, TestClient(app) as client:
        _install_mocked_client([_asam_payload(citation_dict), _tjc_payload(findings)], session)

        # ASAM admission sample.
        asam_response = client.post(
            f"/api/v1/patients/{patient.id}/asam-loc", json={"force_recompute": True}
        )
        if asam_response.status_code != 201:
            print(f"ERROR: ASAM POST returned {asam_response.status_code}: {asam_response.text}")
            return 1
        asam_path = REPO_ROOT / "examples" / "marcus_reyes_asam_admission.json"
        asam_path.write_text(json.dumps(asam_response.json(), indent=2) + "\n")
        print(f"  wrote {asam_path.name}: level={asam_response.json()['recommendation']['level']}")

        # TJC audit sample.
        tjc_response = client.post(
            f"/api/v1/patients/{patient.id}/tjc-audit", json={"force_recompute": True}
        )
        if tjc_response.status_code != 201:
            print(f"ERROR: TJC POST returned {tjc_response.status_code}: {tjc_response.text}")
            return 1
        tjc_path = REPO_ROOT / "examples" / "marcus_reyes_tjc.json"
        tjc_path.write_text(json.dumps(tjc_response.json(), indent=2) + "\n")
        print(f"  wrote {tjc_path.name}: summary={tjc_response.json()['summary']}")

    app.dependency_overrides.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
