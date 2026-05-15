"""
TJC audit orchestrator.

``run_audit(patient_id, db)`` is the single public entry point: it loads
the snapshot once, fans out the 13 predicates in catalog order, and
returns the list of ``EpFinding`` objects.

Acceptance contract (phase_3_PRD.md §6.2, success metric M3):
  * For Marcus's seeded chart, the list contains exactly 13 findings.
  * 5 of those carry ``linked_planted_gap in {G1, G2, G3, G4, G5}``.
  * At least 7 carry ``status == "satisfied"``.
  * 1 carries ``status == "not_applicable"`` (CTS.04.02.33 -- no OUD).
"""

from __future__ import annotations

from uuid import UUID

from sqlmodel import Session

from app.clinical.tjc.audit_functions import (
    AUDIT_REGISTRY,
    EpFinding,
    load_snapshot,
)
from app.clinical.tjc.ep_catalog import EP_CATALOG


def run_audit(patient_id: UUID, db: Session) -> list[EpFinding]:
    """Run all 13 EP predicates and return findings in catalog order."""
    snap = load_snapshot(patient_id, db)
    findings: list[EpFinding] = []
    for ep in EP_CATALOG:
        predicate = AUDIT_REGISTRY[ep.code]
        findings.append(predicate(snap))
    return findings


def summarize(findings: list[EpFinding]) -> dict:
    """Compute the rollup dict that ships under ``summary`` in the API response
    (phase_3_PRD.md §6.2).
    """
    by_status = {"satisfied": 0, "gap": 0, "not_applicable": 0, "ambiguous": 0}
    for finding in findings:
        by_status[finding.status] += 1
    return {
        "total_eps_audited": len(findings),
        "satisfied": by_status["satisfied"],
        "gap": by_status["gap"],
        "not_applicable": by_status["not_applicable"],
        "ambiguous": by_status["ambiguous"],
        "overall_status": "non-compliant" if by_status["gap"] > 0 else "compliant",
    }


__all__ = ["run_audit", "summarize"]
