"""
Assemble the canonical ``AsamAssessmentRead`` shape.

The narration layer (M8) and the rule engine (M2-M4) each produce a
slice of the final response:

  * Rule engine -> ``LevelDecision`` (level, modifiers, rules_fired)
                + ``dict[Subdimension, RiskRating]`` (per-subdim ratings).
  * Narration   -> ``NarrationOutcome`` (LLM-narrated rationale +
                    validation status + warnings).
  * API endpoint -> ``patient_id``, ``computed_at``, ``evidence_hash``,
                    ``model_version``, ``cached``, ``links`` (cache row id
                    once persisted; links assembled from the request).

This module joins them. Pure function -- no DB, no LLM, no side effects.
That keeps the per-endpoint M10 code thin and predictable.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from app.clinical.asam.level_decision import LevelDecision
from app.clinical.asam.rubric import (
    MIN_LOC_BY_RATING,
    MIN_LOC_DISPLAY,
    MinLoc,
    RiskRating,
    Subdimension,
)
from app.clinical.asam.schemas import (
    AsamAssessmentRead,
    AsamRationale,
    DimensionRationale,
    DimensionRead,
    Recommendation,
    SubdimensionRationale,
    SubdimensionRead,
)
from app.clinical.asam.schemas import (
    Citation as PydCitation,
)
from app.clinical.shared.narration_outcome import NarrationOutcome
from app.clinical.tjc.audit_functions import EpFinding
from app.clinical.tjc.schemas import (
    TjcAuditRead,
    TjcAuditResponse,
    TjcFindingNarration,
    TjcFindingRead,
    TjcSummary,
)

# Stable mapping from subdimension to its parent dimension number, so the
# response builder can group the LLM's per-subdim narration under the right
# DimensionRead entry without re-parsing the StrEnum value.
_SUBDIM_TO_DIM_NUMBER: dict[Subdimension, int] = {
    Subdimension.DIM1_INTOXICATION: 1,
    Subdimension.DIM1_WITHDRAWAL: 1,
    Subdimension.DIM1_ADDICTION_MEDS: 1,
    Subdimension.DIM2_PHYSICAL: 2,
    Subdimension.DIM2_PREGNANCY: 2,
    Subdimension.DIM3_ACTIVE_PSYCH: 3,
    Subdimension.DIM3_PERSISTENT_DISABILITY: 3,
    Subdimension.DIM4_RISKY_USE: 4,
    Subdimension.DIM4_RISKY_BEHAVIORS: 4,
    Subdimension.DIM5_FUNCTIONING: 5,
    Subdimension.DIM5_SUPPORT: 5,
    Subdimension.DIM5_SAFETY: 5,
}


# Human-readable dimension names, per ASAM 4th-ed Chapter 6.
_DIMENSION_NAMES: dict[int, str] = {
    1: "Intoxication, Withdrawal, Addiction Medications",
    2: "Biomedical Conditions",
    3: "Psychiatric and Cognitive Conditions",
    4: "Substance Use-related Risks",
    5: "Recovery Environment Interactions",
    6: "Person-Centered Considerations",
}


# Human-readable subdimension labels (paraphrased -- never CAMBHC verbatim).
_SUBDIMENSION_LABELS: dict[Subdimension, str] = {
    Subdimension.DIM1_INTOXICATION: "Intoxication and Associated Risks",
    Subdimension.DIM1_WITHDRAWAL: "Withdrawal and Associated Risks",
    Subdimension.DIM1_ADDICTION_MEDS: "Addiction Medication Needs",
    Subdimension.DIM2_PHYSICAL: "Physical Health Concerns",
    Subdimension.DIM2_PREGNANCY: "Pregnancy-related Concerns",
    Subdimension.DIM3_ACTIVE_PSYCH: "Active Psychiatric Symptoms",
    Subdimension.DIM3_PERSISTENT_DISABILITY: "Persistent Disability",
    Subdimension.DIM4_RISKY_USE: "Likelihood of Risky Substance Use",
    Subdimension.DIM4_RISKY_BEHAVIORS: "Likelihood of Risky SUD-related Behaviors",
    Subdimension.DIM5_FUNCTIONING: "Ability to Function Effectively",
    Subdimension.DIM5_SUPPORT: "Support in Current Environment",
    Subdimension.DIM5_SAFETY: "Safety in Current Environment",
}


def _resolve_min_loc_for_level(level_token: str) -> MinLoc | None:
    """Look up a MinLoc by its level token (e.g., '3.7' -> MinLoc.L3_7).

    Strips the '-COE' suffix if present so 'level_display' resolves
    correctly for COE-modified recommendations.
    """
    base = level_token.removesuffix("-COE")
    for loc in MinLoc:
        if loc.value == base:
            return loc
    return None


def _build_recommendation(decision: LevelDecision) -> Recommendation:
    """Translate the rule engine's LevelDecision into the API shape."""
    min_loc = _resolve_min_loc_for_level(decision.level)
    level_display = MIN_LOC_DISPLAY[min_loc] if min_loc else decision.level
    return Recommendation(
        level=decision.level,
        level_display=level_display,
        modifiers=list(decision.modifiers),
        co_occurring_enhanced=decision.co_occurring_enhanced,
        biomedical_enhanced=decision.biomedical_enhanced,
    )


def _index_rationale(
    rationale_dims: list[DimensionRationale],
) -> dict[int, DimensionRationale]:
    """Map dimension number to its LLM-emitted DimensionRationale."""
    return {dim.dimension: dim for dim in rationale_dims}


def _index_subdim_rationale(
    dim_rationale: DimensionRationale | None,
) -> dict[str, SubdimensionRationale]:
    """Map subdimension key to its SubdimensionRationale, or empty if none."""
    if dim_rationale is None:
        return {}
    return {sub.name: sub for sub in dim_rationale.subdimensions}


def _build_subdim_read(
    *, subdim: Subdimension, rating: RiskRating, sub_rationale: SubdimensionRationale | None
) -> SubdimensionRead:
    """Build one SubdimensionRead by joining the engine's rating with
    the LLM's narration. Missing narration falls back to a deterministic
    placeholder so the response shape stays well-formed.
    """
    min_loc = MIN_LOC_BY_RATING[rating]
    if sub_rationale is None:
        return SubdimensionRead(
            name=_SUBDIMENSION_LABELS[subdim],
            rating=rating.value,
            min_level=min_loc.value,
            rationale="Documentation does not establish findings for this subdimension.",
            citations=[],
        )
    return SubdimensionRead(
        name=_SUBDIMENSION_LABELS[subdim],
        rating=rating.value,
        min_level=min_loc.value,
        rationale=sub_rationale.rationale,
        citations=list(sub_rationale.citations),
    )


def _build_dimension_read(
    *,
    dim_number: int,
    subdims_in_dim: list[Subdimension],
    ratings: dict[Subdimension, RiskRating],
    rationale_by_dim: dict[int, DimensionRationale],
    overall_confidence: str,
) -> DimensionRead | None:
    """Build a DimensionRead for one dimension, or None if no ratings.

    Falls back to a placeholder dimension-level rationale when the LLM
    omitted one for this dimension (rare; the prompt asks for all 5).
    """
    present_subdims = [s for s in subdims_in_dim if s in ratings]
    if not present_subdims:
        return None

    dim_rationale = rationale_by_dim.get(dim_number)
    subdim_rationales = _index_subdim_rationale(dim_rationale)

    subdims = [
        _build_subdim_read(
            subdim=s, rating=ratings[s], sub_rationale=subdim_rationales.get(s.value)
        )
        for s in present_subdims
    ]

    return DimensionRead(
        dimension=dim_number,
        name=_DIMENSION_NAMES[dim_number],
        subdimensions=subdims,
        rationale=(
            dim_rationale.rationale
            if dim_rationale is not None
            else "Documentation does not establish findings for this dimension."
        ),
        # The DimensionRead confidence column tracks the overall confidence
        # for the dimension. For the MVP we propagate the assessment-level
        # confidence -- per-dimension confidence is future work
        # (phase_3_PRD.md §6.7 leaves this open).
        confidence=overall_confidence,  # type: ignore[arg-type]
    )


def _build_dimensions_list(
    *,
    ratings: dict[Subdimension, RiskRating],
    rationale_by_dim: dict[int, DimensionRationale],
    overall_confidence: str,
) -> list[DimensionRead]:
    """Walk dimensions 1-5 (Dim 6 has no ratings) and emit one
    DimensionRead per dimension that has at least one rated subdim.
    """
    subdims_by_dim: dict[int, list[Subdimension]] = {}
    for subdim, dim_number in _SUBDIM_TO_DIM_NUMBER.items():
        subdims_by_dim.setdefault(dim_number, []).append(subdim)

    out: list[DimensionRead] = []
    for dim_number in sorted(subdims_by_dim.keys()):
        built = _build_dimension_read(
            dim_number=dim_number,
            subdims_in_dim=subdims_by_dim[dim_number],
            ratings=ratings,
            rationale_by_dim=rationale_by_dim,
            overall_confidence=overall_confidence,
        )
        if built is not None:
            out.append(built)
    return out


def build_asam_response(
    *,
    patient_id: UUID,
    evidence_hash: str,
    model_version: str,
    cached: bool,
    decision: LevelDecision,
    ratings: dict[Subdimension, RiskRating],
    narration: NarrationOutcome,
    computed_at: datetime,
    assessment_id: UUID | None = None,
) -> AsamAssessmentRead:
    """Assemble the canonical ``AsamAssessmentRead`` from rule-engine +
    narration outputs.

    Pure function -- writes nothing, reads nothing. The cache-row id
    (``assessment_id``) is generated here if the caller doesn't supply
    one; M10's endpoint will pass the persisted row's id.
    """
    aid = assessment_id or uuid4()
    rationale = narration.rationale
    # The NarrationOutcome.rationale union is narrowed by call-site:
    # build_asam_response is only called with AsamRationale-bearing
    # outcomes (asam_loc.py path). Assert defensively so a future caller
    # threading a TjcAuditResponse in here gets a clear failure instead
    # of an AttributeError 100 lines later.
    if not isinstance(rationale, AsamRationale):
        raise TypeError(
            f"build_asam_response expects an AsamRationale; got {type(rationale).__name__}"
        )
    rationale_by_dim = _index_rationale(rationale.dimensions)

    dimensions = _build_dimensions_list(
        ratings=ratings,
        rationale_by_dim=rationale_by_dim,
        overall_confidence=rationale.confidence,
    )

    return AsamAssessmentRead(
        id=aid,
        patient_id=patient_id,
        computed_at=computed_at,
        evidence_hash=evidence_hash,
        model_version=model_version,
        cached=cached,
        recommendation=_build_recommendation(decision),
        dimensions=dimensions,
        rules_fired=list(decision.rules_fired),
        rationale=rationale.overall_rationale,
        confidence=rationale.confidence,
        rationale_status=narration.status,
        rationale_warnings=list(narration.warnings),
        links={
            "self": f"/api/v1/asam-assessments/{aid}",
            "fhir": f"/fhir/ClinicalImpression/{aid}",
        },
    )


def build_tjc_response(
    *,
    patient_id: UUID,
    evidence_hash: str,
    model_version: str,
    cached: bool,
    findings: list[EpFinding],
    narration: NarrationOutcome,
    computed_at: datetime,
    audit_id: UUID | None = None,
) -> TjcAuditRead:
    """Assemble the TjcAuditRead from engine findings + LLM narration.

    Pure function -- no DB, no LLM. The engine's ``EpFinding`` list
    carries status / severity / planted-gap linkage; the LLM's
    ``TjcAuditResponse`` (riding inside the NarrationOutcome's
    ``rationale`` slot) carries the narrative + citations. We join by
    ``ep_code`` -- the LLM may emit findings in any order, but the
    canonical EP_CATALOG order is preserved on output.
    """
    # NarrationOutcome.rationale is a Union; this path expects the TJC
    # variant. A defensive type-check makes the failure mode explicit
    # if a future caller hands the ASAM variant in by mistake.
    audit_response = narration.rationale
    if not isinstance(audit_response, TjcAuditResponse):
        raise TypeError(
            f"build_tjc_response expects a TjcAuditResponse; got {type(audit_response).__name__}"
        )
    narration_by_code: dict[str, TjcFindingNarration] = {
        f.ep_code: f for f in audit_response.findings
    }

    aid = audit_id or uuid4()
    findings_read: list[TjcFindingRead] = []
    counts = {"satisfied": 0, "gap": 0, "not_applicable": 0, "ambiguous": 0}

    for finding in findings:
        narration_for_ep = narration_by_code.get(finding.ep_code)
        if narration_for_ep is not None:
            narrative = narration_for_ep.narrative
            citations = list(narration_for_ep.citations)
        else:
            # LLM omitted this EP -- shouldn't happen given the forced
            # tool, but if it does we fall back to the engine's
            # finding_template so the response is still well-formed.
            narrative = finding.finding_template
            citations = []

        # When the LLM emitted no citations, fall back to converting
        # the engine's evidence_pointers (M5 dataclass Citation) into
        # Pydantic Citation models. Only for positive findings -- negative
        # findings carry [] pointers by contract.
        if not citations and finding.evidence_pointers and not finding.negative_finding:
            citations = [
                PydCitation(
                    document_id=p.document_id,
                    char_start=p.char_start,
                    char_end=p.char_end,
                    snippet=p.snippet,
                )
                for p in finding.evidence_pointers
            ]

        findings_read.append(
            TjcFindingRead(
                ep_code=finding.ep_code,
                ep_domain=finding.ep_domain,
                status=finding.status,
                severity=finding.severity,
                negative_finding=finding.negative_finding,
                narrative=narrative,
                citations=citations,
                linked_planted_gap=finding.linked_planted_gap,
            )
        )
        counts[finding.status] += 1

    summary = TjcSummary(
        total_eps_audited=len(findings),
        satisfied=counts["satisfied"],
        gap=counts["gap"],
        not_applicable=counts["not_applicable"],
        ambiguous=counts["ambiguous"],
        overall_status="non-compliant" if counts["gap"] > 0 else "compliant",
    )

    return TjcAuditRead(
        id=aid,
        patient_id=patient_id,
        computed_at=computed_at,
        evidence_hash=evidence_hash,
        model_version=model_version,
        cached=cached,
        summary=summary,
        findings=findings_read,
        rationale_status=narration.status,
        rationale_warnings=list(narration.warnings),
        links={
            "self": f"/api/v1/tjc-audits/{aid}",
            "fhir_bundle": (f"/fhir/DetectedIssue?subject=Patient/{patient_id}&category=tjc-audit"),
        },
    )


__all__ = ["build_asam_response", "build_tjc_response"]
