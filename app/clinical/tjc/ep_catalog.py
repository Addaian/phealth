"""
Joint Commission Element-of-Performance catalog -- 13 chart-auditable EPs.

Paraphrased from public R3 reports, the Joint Commission FAQ archive, and
standard accreditation summaries. **Not a verbatim reproduction** of the
Comprehensive Accreditation Manual for Behavioral Health Care (CAMBHC),
which is proprietary -- see phase_3_PRD.md §3 and playbook §F for the
TJC IP boundary.

The 13 codes match phase_3_PRD.md §5.5. Five of them ("gap-bearing" EPs)
are linked to one of the five intentional gaps planted in Marcus's
synthetic chart (G1-G5; see app/synthetic/persona.yaml §intentional_gaps).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.db.models import TjcCoverage

Severity = Literal["high", "moderate", "low", "none"]
EpStatus = Literal["satisfied", "gap", "not_applicable", "ambiguous"]


@dataclass(frozen=True)
class EpDefinition:
    """One Element of Performance in the audit catalog.

    Fields:
      * ``code``           -- canonical token used in the API response.
      * ``domain``         -- short label for grouping (e.g.
                              "Measurement-based care").
      * ``paraphrased_requirement`` -- our paraphrased restatement of the
                              chart-auditable signal. Sent to Claude as
                              part of the narration prompt.
      * ``severity``       -- the severity to apply if this EP is found
                              to be a gap. Severity is per-EP; "none" is
                              used when the EP is satisfied or n/a.
      * ``coverage_code``  -- the Phase 1 ``TjcCoverage.ep_code`` that
                              backs this EP (when the ingester already
                              classified it). ``None`` for EPs that the
                              audit predicate computes from raw rows.
      * ``planted_gap``    -- the synthetic-chart gap id (G1-G5) this EP
                              is designed to catch, or None.
    """

    code: str
    domain: str
    paraphrased_requirement: str
    severity: Severity
    coverage_code: str | None
    planted_gap: str | None


EP_CATALOG: list[EpDefinition] = [
    EpDefinition(
        code="CTS.02.03.07 EP 1",
        domain="SUD history collection",
        paraphrased_requirement=(
            "Documentation captures the patient's substance use history -- substances "
            "used, patterns, route, and frequency -- at intake assessment."
        ),
        severity="moderate",
        coverage_code=None,
        planted_gap=None,
    ),
    EpDefinition(
        code="CTS.02.03.07 EP 7",
        domain="Withdrawal / intoxication risk in assessment",
        paraphrased_requirement=(
            "The intake assessment includes a withdrawal- and intoxication-risk "
            "rating using a standardized scale (e.g., CIWA-Ar for alcohol, COWS "
            "for opioids)."
        ),
        severity="moderate",
        coverage_code=None,
        planted_gap=None,
    ),
    EpDefinition(
        code="CTS.02.01.07 EP 1",
        domain="History and physical within 24h of admission",
        paraphrased_requirement=(
            "A signed, timestamped History and Physical is documented within 24 "
            "hours of inpatient crisis-stabilization admission."
        ),
        severity="high",
        coverage_code=None,
        planted_gap=None,
    ),
    EpDefinition(
        code="CTS.03.01.03 EP 28",
        domain="Individualized treatment plan reflecting assessed needs",
        paraphrased_requirement=(
            "The treatment plan is dated on the day of admission and reflects "
            "every assessed need (the 'golden thread'). Services delivered must "
            "appear on the plan."
        ),
        severity="high",
        coverage_code="CTS.03.01.03",
        planted_gap="G2",
    ),
    EpDefinition(
        code="CTS.03.01.09",
        domain="Measurement-based care",
        paraphrased_requirement=(
            "Outcomes are assessed with a standardized validated instrument "
            "repeated at clinically meaningful intervals across the episode of "
            "care (not just once at intake)."
        ),
        severity="high",
        coverage_code="CTS.03.01.09",
        planted_gap="G1",
    ),
    EpDefinition(
        code="CTS.04.02.33",
        domain="MOUD offered for opioid use disorder",
        paraphrased_requirement=(
            "For patients with a documented opioid use disorder, medication for "
            "opioid use disorder (methadone, buprenorphine, naltrexone-IM) is "
            "offered and either initiated or declined-with-documentation."
        ),
        severity="high",
        coverage_code=None,
        planted_gap=None,
    ),
    EpDefinition(
        code="CTS.04.02 (TOC)",
        domain="Care transition / discharge planning",
        paraphrased_requirement=(
            "Discharge and transition-of-care planning is documented for substance "
            "use disorder treatment. R3 Report Issue 25 specifies within 72 hours "
            "of admission."
        ),
        severity="high",
        coverage_code="R3-25",
        planted_gap="G4",
    ),
    EpDefinition(
        code="NPSG.15.01.01 EP 2",
        domain="Suicide risk screening with validated tool",
        paraphrased_requirement=(
            "Every patient is screened for suicidal ideation using a validated "
            "tool (e.g., C-SSRS) at admission."
        ),
        severity="high",
        coverage_code=None,
        planted_gap=None,
    ),
    EpDefinition(
        code="NPSG.15.01.01 EP 3",
        domain="Suicide risk assessment + reassessment",
        paraphrased_requirement=(
            "When the screen is positive, the patient receives a documented "
            "severity assessment AND a reassessment before any clinically "
            "significant point in care (e.g., level-of-care transition)."
        ),
        severity="high",
        coverage_code="NPSG.15.01.01",
        planted_gap="G3",
    ),
    EpDefinition(
        code="NPSG.15.01.01 EP 4",
        domain="Documented risk level + mitigation plan",
        paraphrased_requirement=(
            "A documented overall suicide risk level and a corresponding "
            "mitigation plan are present in the chart."
        ),
        severity="moderate",
        coverage_code=None,
        planted_gap=None,
    ),
    EpDefinition(
        code="RC.01.01.01 EP 7",
        domain="Entries authenticated, dated, timed",
        paraphrased_requirement=(
            "Every clinical-record entry is authenticated (signed), dated, and "
            "timed by the responsible practitioner."
        ),
        severity="moderate",
        coverage_code=None,
        planted_gap=None,
    ),
    EpDefinition(
        code="RC.01.02.01 EP 3",
        domain="Co-signatures for delegated work",
        paraphrased_requirement=(
            "Notes authored by non-clinical roles (e.g., peer recovery specialist) "
            "are co-signed by a qualified clinician with a consistent timestamp."
        ),
        severity="moderate",
        coverage_code="RC.01.02.01",
        planted_gap="G5",
    ),
    EpDefinition(
        code="RC.01.03.01",
        domain="Timeliness of record completion",
        paraphrased_requirement=(
            "Notes are completed and filed within the organization's defined "
            "window after each encounter."
        ),
        severity="low",
        coverage_code=None,
        planted_gap=None,
    ),
]


# Convenience lookups, built once at import time.
EP_BY_CODE: dict[str, EpDefinition] = {ep.code: ep for ep in EP_CATALOG}
GAP_TO_EP_CODE: dict[str, str] = {ep.planted_gap: ep.code for ep in EP_CATALOG if ep.planted_gap}


def lookup_coverage(ep: EpDefinition, rows: list[TjcCoverage]) -> TjcCoverage | None:
    """Return the TjcCoverage row this EP delegates to, or None.

    Returns None when the EP has no ``coverage_code`` (the predicate
    computes the status from raw rows) OR when the named row is missing
    from the database (a data-integrity issue that the predicate falls
    back on gracefully).
    """
    if ep.coverage_code is None:
        return None
    return next((r for r in rows if r.ep_code == ep.coverage_code), None)


__all__ = [
    "EpDefinition",
    "Severity",
    "EpStatus",
    "EP_CATALOG",
    "EP_BY_CODE",
    "GAP_TO_EP_CODE",
    "lookup_coverage",
]
