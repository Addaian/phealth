"""
TJC coverage-matrix builder.

Evaluates a patient's full document set against the Joint Commission Element of
Performance (EP) catalog in ``data/seed/tjc_eps.yaml`` and emits one
``TjcCoverage``-shaped dict per EP -- ``status`` satisfied | gap | ambiguous,
with a pointer to the supporting (or conspicuously absent) evidence span.

Each EP has a rule function below, registered in :data:`RULES` by EP code. A
rule receives the document set plus the admission datetime and returns a
:class:`CoverageResult`. The five intentional gaps in the synthetic chart
(G1-G5; see app/synthetic/persona.yaml §intentional_gaps) are each designed to
be caught here as status ``gap``; the remaining EPs are designed to be
``satisfied`` -- that contrast is what makes the Phase 3 audit endpoint
demonstrable.

Document bundle contract
------------------------
Each element of ``documents`` is a dict the orchestrator assembles per PDF::

    {
        "doc_type": "bps_intake" | "soap" | "dap" | "dsap",
        "document_id": uuid.UUID,          # already persisted, for evidence pointers
        "raw_text": str,
        "authored_on": datetime,
        "author_name": str,
        "author_role": str,
        "sections": {section_key: {"title", "text", "char_span"}},
        "scale_observations": list[dict],  # from scale_extractor
        "entity_observations": list[dict], # from entity_tagger
    }
"""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.ingest.seed import load_seed

# LOINC codes referenced by the rules.
_LOINC_PHQ9 = "44261-6"
_LOINC_CSSRS = "93373-7"

# Role classifiers for the documentation-authentication rule. A note authored
# by a non-clinical role with no clinical credential is the RC.01.02.01 finding.
_NON_CLINICAL_ROLE = re.compile(r"\b(?:CPRS|peer|recovery support|specialist)\b", re.IGNORECASE)
_CLINICAL_ROLE = re.compile(
    r"\b(?:MD|DO|LCSW|LMHC|LPC|LPCC|RN|NP|PA|psychiatrist|psychologist|social worker)\b",
    re.IGNORECASE,
)


@dataclass
class CoverageResult:
    """The outcome of evaluating one EP rule against the document set."""

    status: str  # "satisfied" | "gap" | "ambiguous"
    evidence_document_id: uuid.UUID | None
    evidence_span: tuple[int, int] | None
    rationale: str


def build_tjc_coverage(documents: list[dict], admission_date: datetime) -> list[dict]:
    """Evaluate every EP in the catalog and return ``TjcCoverage``-shaped dicts.

    ``patient_id`` is attached by the orchestrator.
    """
    rows: list[dict] = []
    for entry in load_seed("tjc_eps.yaml")["eps"]:
        rule = RULES.get(entry["code"])
        if rule is None:
            result = CoverageResult("ambiguous", None, None, "No rule implemented for this EP.")
        else:
            result = rule(documents, admission_date)
        rows.append(
            {
                "ep_code": entry["code"],
                "status": result.status,
                "evidence_document_id": result.evidence_document_id,
                "evidence_char_start": result.evidence_span[0] if result.evidence_span else None,
                "evidence_char_end": result.evidence_span[1] if result.evidence_span else None,
                "rationale": result.rationale,
            }
        )
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Bundle accessors
# ─────────────────────────────────────────────────────────────────────────────
def _bps(documents: list[dict]) -> dict | None:
    """Return the BPS intake bundle, or None if the set has no intake."""
    return next((doc for doc in documents if doc["doc_type"] == "bps_intake"), None)


def _progress_notes(documents: list[dict]) -> list[dict]:
    """Return the progress-note bundles (soap / dap / dsap), in document order."""
    return [doc for doc in documents if doc["doc_type"] in ("soap", "dap", "dsap")]


def _find_scale(bundle: dict | None, loinc: str) -> dict | None:
    """Return the first scale observation in ``bundle`` with the given LOINC code."""
    if bundle is None:
        return None
    return next(
        (obs for obs in bundle["scale_observations"] if obs["code"] == loinc),
        None,
    )


def _section_text(bundle: dict | None, section_key: str) -> str:
    """Return the text of one section, or '' if the bundle/section is absent."""
    if bundle is None:
        return ""
    return bundle.get("sections", {}).get(section_key, {}).get("text", "")


def _authored_date(bundle: dict) -> str:
    """The bundle's authored-on date as an ISO string, for rationale text."""
    return bundle["authored_on"].date().isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# EP rules — one per code in data/seed/tjc_eps.yaml
# ─────────────────────────────────────────────────────────────────────────────
def _rule_comprehensive_assessment(documents: list[dict], _admission: datetime) -> CoverageResult:
    """CTS.01.05.01 — a comprehensive BPS assessment was completed at admission."""
    bps = _bps(documents)
    if bps is None:
        return CoverageResult(
            "gap", None, None, "No comprehensive biopsychosocial assessment found."
        )
    section_count = len(bps["sections"])
    if section_count >= 15:  # the BPS template carries 21 sections
        first_section = next(iter(bps["sections"].values()))
        return CoverageResult(
            "satisfied",
            bps["document_id"],
            tuple(first_section["char_span"]),
            f"A comprehensive biopsychosocial assessment with {section_count} documented "
            "sections was completed at admission.",
        )
    return CoverageResult(
        "ambiguous",
        bps["document_id"],
        None,
        f"A biopsychosocial assessment exists but only {section_count} sections are populated.",
    )


def _rule_record_completeness(documents: list[dict], _admission: datetime) -> CoverageResult:
    """RC.01.01.01 — the clinical record (here, the intake) is complete."""
    bps = _bps(documents)
    if bps is None:
        return CoverageResult("gap", None, None, "No intake assessment in the clinical record.")
    empty_sections = [key for key, value in bps["sections"].items() if not value["text"].strip()]
    if empty_sections:
        return CoverageResult(
            "gap",
            bps["document_id"],
            None,
            f"{len(empty_sections)} intake section(s) are empty: {', '.join(empty_sections)}.",
        )
    return CoverageResult(
        "satisfied",
        bps["document_id"],
        None,
        f"All {len(bps['sections'])} intake assessment sections are populated.",
    )


def _rule_treatment_plan_goals(documents: list[dict], _admission: datetime) -> CoverageResult:
    """CTS.04.02.01 — a treatment plan with measurable goals and target dates exists."""
    bps = _bps(documents)
    if bps is None:
        return CoverageResult("gap", None, None, "No intake assessment, so no treatment plan.")
    plan_text = _section_text(bps, "treatment_plan")
    if re.search(r"Goal \d", plan_text) and re.search(r"Target date", plan_text, re.IGNORECASE):
        span = bps["sections"]["treatment_plan"]["char_span"]
        return CoverageResult(
            "satisfied",
            bps["document_id"],
            tuple(span),
            "A treatment plan with numbered, measurable goals and target dates is documented.",
        )
    return CoverageResult(
        "gap",
        bps["document_id"],
        None,
        "No treatment plan with measurable goals and target dates found.",
    )


def _rule_golden_thread(documents: list[dict], _admission: datetime) -> CoverageResult:
    """CTS.03.01.03 (gap G2) — the plan must reflect the services actually delivered.

    The day-5 DAP note documents a peer recovery support group; if the intake
    treatment plan does not list peer support as an intervention, the plan does
    not reflect delivered care.
    """
    bps = _bps(documents)
    peer_pattern = re.compile(r"peer[- ](?:recovery )?support", re.IGNORECASE)

    peer_note = next(
        (note for note in _progress_notes(documents) if peer_pattern.search(note["raw_text"])),
        None,
    )
    if peer_note is None:
        return CoverageResult(
            "satisfied",
            None,
            None,
            "No services documented outside the treatment plan's intervention list.",
        )
    # A peer-support service IS documented in a progress note. Can it be checked
    # against the treatment plan?
    if bps is None:
        return CoverageResult(
            "ambiguous",
            peer_note["document_id"],
            None,
            "Peer support is documented in a progress note, but there is no intake "
            "assessment / treatment plan to check it against.",
        )
    if peer_pattern.search(_section_text(bps, "treatment_plan")):
        return CoverageResult(
            "satisfied",
            bps["document_id"],
            None,
            "Peer support is documented in both the treatment plan and the progress notes.",
        )
    # peer_note was selected by this exact pattern, so it must match again here.
    match = peer_pattern.search(peer_note["raw_text"])
    assert match is not None
    return CoverageResult(
        "gap",
        peer_note["document_id"],
        (match.start(), match.end()),
        f"Progress note dated {_authored_date(peer_note)} documents a peer recovery support "
        "group session, but the treatment plan's intervention list does not include peer "
        "support — the plan does not reflect the services actually being delivered.",
    )


def _rule_measurement_based_care(documents: list[dict], _admission: datetime) -> CoverageResult:
    """CTS.03.01.09 (gap G1) — outcomes must be re-assessed with a standardized tool.

    A PHQ-9 at intake that is never repeated means outcomes were not measured
    over the episode of care — despite the treatment plan specifying bi-weekly PHQ-9.
    """
    bps = _bps(documents)
    if bps is None:
        return CoverageResult(
            "ambiguous",
            None,
            None,
            "No intake assessment found; cannot assess measurement-based care.",
        )
    notes = _progress_notes(documents)
    intake_phq9 = _find_scale(bps, _LOINC_PHQ9)
    if intake_phq9 is None:
        return CoverageResult(
            "ambiguous",
            None,
            None,
            "No PHQ-9 found at intake; cannot assess re-administration.",
        )
    if any(_find_scale(note, _LOINC_PHQ9) for note in notes):
        return CoverageResult(
            "satisfied",
            bps["document_id"],
            (intake_phq9["char_start"], intake_phq9["char_end"]),
            "PHQ-9 administered at intake and re-administered in at least one progress note.",
        )
    return CoverageResult(
        "gap",
        bps["document_id"],
        (intake_phq9["char_start"], intake_phq9["char_end"]),
        f"PHQ-9 administered at intake (total {int(intake_phq9['value_quantity'])}) but not "
        f"re-administered in any of the {len(notes)} progress notes, despite the treatment plan "
        "specifying bi-weekly PHQ-9 — outcomes were not assessed with a standardized tool over "
        "the episode of care.",
    )


def _rule_suicide_rescreening(documents: list[dict], _admission: datetime) -> CoverageResult:
    """NPSG.15.01.01 (gap G3) — suicide risk must be re-screened at significant points.

    C-SSRS is administered at intake. If a progress note's plan calls for
    re-administration before a level-of-care transition but no C-SSRS result is
    ever documented in a progress note, the re-screening did not occur.
    """
    notes = _progress_notes(documents)
    if any(_find_scale(note, _LOINC_CSSRS) for note in notes):
        return CoverageResult(
            "satisfied",
            None,
            None,
            "C-SSRS administered at intake and re-administered in a progress note.",
        )
    planning_note = next(
        (note for note in notes if re.search(r"C-SSRS", note["raw_text"], re.IGNORECASE)),
        None,
    )
    if planning_note is None:
        return CoverageResult(
            "ambiguous",
            None,
            None,
            "No progress note references C-SSRS re-screening; cannot assess.",
        )
    match = re.search(r"[Rr]e-administer C-SSRS[^.]*\.", planning_note["raw_text"])
    span = (match.start(), match.end()) if match else None
    return CoverageResult(
        "gap",
        planning_note["document_id"],
        span,
        f"C-SSRS administered at intake. The {_authored_date(planning_note)} note's plan calls "
        "for C-SSRS re-administration prior to a level-of-care transfer, but no C-SSRS result is "
        "documented in any progress note — suicide risk was not re-screened at a clinically "
        "significant transition.",
    )


def _rule_transitions_of_care(documents: list[dict], admission_date: datetime) -> CoverageResult:
    """R3-25 (gap G4) — discharge/transition planning within 72 hours of admission.

    The intake assessment itself does not count as a discharge-planning note;
    only a progress note that substantively addresses discharge/transfer/
    step-down within the window satisfies this EP.
    """
    window_end = admission_date + timedelta(hours=72)
    transition_pattern = re.compile(r"\b(?:discharge|transfer|step.?down)\b", re.IGNORECASE)

    for note in _progress_notes(documents):
        if not admission_date <= note["authored_on"] <= window_end:
            continue
        match = transition_pattern.search(_section_text(note, "plan"))
        if match:
            plan_span = note["sections"]["plan"]["char_span"]
            absolute_start = plan_span[0] + match.start()
            return CoverageResult(
                "satisfied",
                note["document_id"],
                (absolute_start, absolute_start + (match.end() - match.start())),
                f"Discharge/transition planning documented within 72 hours of admission "
                f"(progress note dated {_authored_date(note)}).",
            )
    return CoverageResult(
        "gap",
        None,
        None,
        f"Patient admitted {admission_date.date().isoformat()}. No discharge or "
        "transition-of-care planning is documented within 72 hours of admission — required for "
        "substance use disorder treatment transitions of care per R3 Report Issue 25.",
    )


def _rule_documentation_authentication(
    documents: list[dict], _admission: datetime
) -> CoverageResult:
    """RC.01.02.01 (gap G5) — non-clinical documentation needs a clinical co-signature.

    The day-5 DAP note is authored and signed solely by a peer recovery support
    specialist (CPRS), a non-clinical role; the export shows no clinical
    co-signature.
    """
    for note in _progress_notes(documents):
        role = note["author_role"]
        is_non_clinical = bool(_NON_CLINICAL_ROLE.search(role)) and not _CLINICAL_ROLE.search(role)
        if is_non_clinical:
            return CoverageResult(
                "gap",
                note["document_id"],
                None,
                f"Progress note dated {_authored_date(note)} was authored and signed solely by a "
                f"non-clinical role ({role}); no clinical co-signature is documented, contrary to "
                "documentation-authentication requirements.",
            )
    return CoverageResult(
        "satisfied",
        None,
        None,
        "All progress notes are authored by appropriately credentialed clinicians.",
    )


# Registry: EP code -> rule function. Keyed to match data/seed/tjc_eps.yaml.
RULES: dict[str, Callable[[list[dict], datetime], CoverageResult]] = {
    "CTS.01.05.01": _rule_comprehensive_assessment,
    "RC.01.01.01": _rule_record_completeness,
    "CTS.04.02.01": _rule_treatment_plan_goals,
    "CTS.03.01.03": _rule_golden_thread,
    "CTS.03.01.09": _rule_measurement_based_care,
    "NPSG.15.01.01": _rule_suicide_rescreening,
    "R3-25": _rule_transitions_of_care,
    "RC.01.02.01": _rule_documentation_authentication,
}
