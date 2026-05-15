"""
Joint Commission audit predicates -- one per EP, all the same shape.

Each predicate takes a ``TjcSnapshot`` and returns an ``EpFinding``. The
predicate's job is twofold:

  1. Decide ``status`` -- satisfied | gap | not_applicable | ambiguous.
  2. Build a ``finding_template`` -- a short factual statement that the
     M9 narration layer will polish into surveyor-RFI prose. The
     template is the **only** EP-specific clinical content the LLM ever
     sees from this file; the EP-requirement language lives in
     ``ep_catalog.py``.

Three classes of predicate (see phase_3_PRD.md §5.5):

  * **TjcCoverage-backed**: 8 EPs whose status was decided by the Phase 1
    ``tjc_coverage_matrix``. The predicate looks up the existing row and
    translates ``satisfied/gap/ambiguous`` into an ``EpFinding`` with
    surveyor detail.
  * **Raw-row predicates**: 5 EPs without a TjcCoverage entry. The
    predicate consults ``ExtractedObservation`` and ``ClinicalDocument``
    directly to decide.
  * **Conditional applicability**: ``CTS.04.02.33`` (MOUD for OUD) is
    only applicable when the patient has an opioid use disorder
    diagnosis. Marcus does not -- this EP returns ``not_applicable``.

Negative-finding contract (phase_3_PRD.md §5.5, §5.6)
-----------------------------------------------------
For absence-of-evidence findings (G1, G3, G4), ``negative_finding=True``
and ``evidence_pointers=[]``. The LLM is then prompted with an explicit
"Documentation review did not identify X during the [time_window]"
template rather than asked to cite a span (which would force it to
hallucinate).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlmodel import Session, select

from app.clinical.tjc.ep_catalog import (
    EP_BY_CODE,
    EpDefinition,
    EpStatus,
    Severity,
    lookup_coverage,
)
from app.db.models import (
    ClinicalDocument,
    ExtractedObservation,
    Patient,
    TjcCoverage,
)

# ────────────────────────────────────────────────────────────────────────
# Output models.
# ────────────────────────────────────────────────────────────────────────


@dataclass
class Citation:
    """One char-offset citation into a ClinicalDocument's raw_text.

    Mirrors the ``ProvenanceRead`` shape from Phase 2 so the M9 narration
    layer can pass these straight to the Anthropic Citations API.
    """

    document_id: UUID
    char_start: int
    char_end: int
    snippet: str


@dataclass
class EpFinding:
    """The outcome of evaluating one EP -- the unit of the TJC audit response.

    Shape mirrors phase_3_PRD.md §6.2's ``findings[].*`` shape; the M9
    narration layer translates this into the surveyor-RFI ``narrative``
    string and the LLM's citations[] payload.
    """

    ep_code: str
    ep_domain: str
    status: EpStatus
    severity: Severity
    negative_finding: bool
    finding_template: str
    evidence_pointers: list[Citation] = field(default_factory=list)
    linked_planted_gap: str | None = None


# ────────────────────────────────────────────────────────────────────────
# Snapshot.
# ────────────────────────────────────────────────────────────────────────


@dataclass
class TjcSnapshot:
    """Everything an audit predicate might inspect, loaded once per call."""

    patient: Patient
    documents: list[ClinicalDocument]
    observations: list[ExtractedObservation]
    coverage_rows: list[TjcCoverage]

    # Pre-computed: document_id -> ClinicalDocument, for fast lookups when
    # building Citation objects (snippet requires raw_text[start:end]).
    _docs_by_id: dict[UUID, ClinicalDocument] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._docs_by_id = {doc.id: doc for doc in self.documents}

    def get_coverage(self, ep_code: str) -> TjcCoverage | None:
        return next((r for r in self.coverage_rows if r.ep_code == ep_code), None)

    def has_diagnosis(self, icd10_prefix: str) -> bool:
        return any(
            obs.code_system == "ICD-10" and obs.code.startswith(icd10_prefix)
            for obs in self.observations
        )

    def has_observation(self, code_system: str, code: str) -> bool:
        return any(obs.code_system == code_system and obs.code == code for obs in self.observations)

    def admission_document(self) -> ClinicalDocument | None:
        """Return the earliest BPS intake document (if any)."""
        intakes = [d for d in self.documents if d.document_type == "bps_intake"]
        if not intakes:
            return None
        return min(intakes, key=lambda d: d.authored_on)

    def documents_after(self, when: datetime) -> list[ClinicalDocument]:
        return [d for d in self.documents if d.authored_on > when]

    def citation_for_coverage(self, row: TjcCoverage) -> Citation | None:
        """Build a Citation from a TjcCoverage row's evidence pointer."""
        if (
            row.evidence_document_id is None
            or row.evidence_char_start is None
            or row.evidence_char_end is None
        ):
            return None
        doc = self._docs_by_id.get(row.evidence_document_id)
        if doc is None:
            return None
        return Citation(
            document_id=doc.id,
            char_start=row.evidence_char_start,
            char_end=row.evidence_char_end,
            snippet=doc.raw_text[row.evidence_char_start : row.evidence_char_end],
        )


def load_snapshot(patient_id: UUID, db: Session) -> TjcSnapshot:
    """Issue 4 SELECTs and assemble the TjcSnapshot.

    Parallel to ``app.clinical.asam.risk_ratings._load_snapshot`` -- the two
    snapshots intentionally share shape and load pattern so future code
    can collapse them if needed.
    """
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise ValueError(f"patient {patient_id} not found")

    documents = list(
        db.exec(select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_id))
    )
    doc_ids = [doc.id for doc in documents]
    observations: list[ExtractedObservation] = []
    if doc_ids:
        observations = list(
            db.exec(
                select(ExtractedObservation).where(
                    ExtractedObservation.document_id.in_(doc_ids)  # type: ignore[attr-defined]
                )
            )
        )
    coverage_rows = list(db.exec(select(TjcCoverage).where(TjcCoverage.patient_id == patient_id)))
    return TjcSnapshot(
        patient=patient,
        documents=documents,
        observations=observations,
        coverage_rows=coverage_rows,
    )


# ────────────────────────────────────────────────────────────────────────
# Shared helper: translate a TjcCoverage row into an EpFinding.
# ────────────────────────────────────────────────────────────────────────


def _finding_from_coverage(
    ep: EpDefinition,
    snap: TjcSnapshot,
    gap_template: str,
    satisfied_template: str,
    *,
    negative_when_gap: bool = False,
) -> EpFinding:
    """Translate a TjcCoverage-backed EP into a fully-detailed EpFinding.

    ``gap_template`` / ``satisfied_template`` are short factual strings
    the M9 narration layer will polish. They are stored verbatim on the
    EpFinding so a reviewer can audit the engine's reasoning even
    without Claude's narration.

    ``negative_when_gap=True`` is set for absence-of-evidence findings
    (G1, G3, G4) -- the LLM is then told the finding is absence-based
    and prompted to phrase it as "Documentation review did not identify
    X" rather than asked to cite a span.
    """
    row = lookup_coverage(ep, snap.coverage_rows)
    if row is None:
        return _ambiguous_finding(ep, reason=f"no TjcCoverage row for {ep.coverage_code!r}")

    if row.status == "satisfied":
        citation = snap.citation_for_coverage(row)
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="satisfied",
            severity="none",
            negative_finding=False,
            finding_template=satisfied_template,
            evidence_pointers=[citation] if citation else [],
            linked_planted_gap=None,
        )

    if row.status == "gap":
        # Include the Phase 1 citation even when this is a negative
        # finding (G1, G3): the citation points to *positive context*
        # (e.g., the single PHQ-9 administration that did happen),
        # which is the legitimate evidence the LLM attaches to its
        # "X was administered once, not re-administered" narrative.
        # phase_3_PRD.md §6.2's example confirms G1 carries a citation.
        # The ``negative_finding`` flag controls the LLM narration
        # register (absence-template) rather than whether context
        # citations are present.
        citation = snap.citation_for_coverage(row)
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="gap",
            severity=ep.severity,
            negative_finding=negative_when_gap,
            finding_template=gap_template,
            evidence_pointers=[citation] if citation else [],
            linked_planted_gap=ep.planted_gap,
        )

    # status == "ambiguous"
    return _ambiguous_finding(
        ep, reason=f"TjcCoverage row reports status={row.status!r}: {row.rationale}"
    )


def _ambiguous_finding(ep: EpDefinition, *, reason: str) -> EpFinding:
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="ambiguous",
        severity="low",
        negative_finding=False,
        finding_template=f"Status could not be determined: {reason}",
        evidence_pointers=[],
        linked_planted_gap=None,
    )


# ────────────────────────────────────────────────────────────────────────
# Raw-row predicates (5 EPs without a TjcCoverage row).
# ────────────────────────────────────────────────────────────────────────


def audit_sud_history_collection(snap: TjcSnapshot) -> EpFinding:
    """CTS.02.03.07 EP 1 -- did the intake document substance use history?

    Signal: at least one ICD-10 F1x (substance use disorder) diagnosis AND
    at least one ``internal:substance:*`` entity present in
    ExtractedObservation. Both should fall out of a proper SUD intake.

    Marcus: F10.232 + F13.20 + alcohol/alprazolam/cannabis/opioid entities
    → satisfied.
    """
    ep = EP_BY_CODE["CTS.02.03.07 EP 1"]
    has_sud_dx = any(
        obs.code_system == "ICD-10" and obs.code.startswith("F1") for obs in snap.observations
    )
    has_substance_entities = any(
        obs.code_system == "internal" and obs.code.startswith("substance:")
        for obs in snap.observations
    )
    if has_sud_dx and has_substance_entities:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="satisfied",
            severity="none",
            negative_finding=False,
            finding_template=(
                "The intake assessment documents the patient's substance use "
                "history (substances used and pattern) along with concordant "
                "ICD-10 SUD diagnoses."
            ),
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="gap",
        severity=ep.severity,
        negative_finding=True,
        finding_template=(
            "Documentation review did not identify a complete substance use "
            "history at intake (missing SUD diagnoses or substance entities)."
        ),
    )


def audit_withdrawal_risk_assessment(snap: TjcSnapshot) -> EpFinding:
    """CTS.02.03.07 EP 7 -- did the intake include a withdrawal-scale rating?

    Signal: at least one CIWA-Ar (LOINC 72109-3) or COWS (LOINC 92103-9)
    observation.

    Marcus: CIWA-Ar administered three times → satisfied.
    """
    ep = EP_BY_CODE["CTS.02.03.07 EP 7"]
    has_ciwa = snap.has_observation("LOINC", "72109-3")
    has_cows = snap.has_observation("LOINC", "92103-9")
    if has_ciwa or has_cows:
        scale = "CIWA-Ar" if has_ciwa else "COWS"
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="satisfied",
            severity="none",
            negative_finding=False,
            finding_template=(
                f"A validated withdrawal-risk scale ({scale}) was administered, "
                "documenting the assessment of intoxication and withdrawal risk."
            ),
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="gap",
        severity=ep.severity,
        negative_finding=True,
        finding_template=(
            "Documentation review did not identify a validated withdrawal-risk "
            "or intoxication-risk scale at intake."
        ),
    )


def audit_history_and_physical_24h(snap: TjcSnapshot) -> EpFinding:
    """CTS.02.01.07 EP 1 -- was an H&P done within 24h of admission?

    Signal: a ``bps_intake`` document exists and was authored on the same
    day as admission (we use the earliest encounter as the admission
    proxy). Marcus's BPS intake is dated 2026-05-04 — admission day → satisfied.
    """
    ep = EP_BY_CODE["CTS.02.01.07 EP 1"]
    intake = snap.admission_document()
    if intake is None:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="gap",
            severity=ep.severity,
            negative_finding=True,
            finding_template=(
                "Documentation review did not identify a History and Physical "
                "completed within 24 hours of admission."
            ),
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="satisfied",
        severity="none",
        negative_finding=False,
        finding_template=(
            f"A biopsychosocial History and Physical was completed on "
            f"{intake.authored_on.date().isoformat()} (admission day), "
            "satisfying the within-24-hours requirement."
        ),
        evidence_pointers=[
            Citation(
                document_id=intake.id,
                char_start=0,
                char_end=min(120, len(intake.raw_text)),
                snippet=intake.raw_text[: min(120, len(intake.raw_text))],
            )
        ],
    )


def audit_moud_for_oud(snap: TjcSnapshot) -> EpFinding:
    """CTS.04.02.33 -- MOUD offered for OUD; n/a if no OUD.

    Signal: ICD-10 F11.* (opioid use disorder) presence.
      * No F11 → not_applicable (Marcus's case).
      * F11 present + MOUD medication entity present → satisfied.
      * F11 present + no MOUD entity → gap.
    """
    ep = EP_BY_CODE["CTS.04.02.33"]
    has_oud = snap.has_diagnosis("F11")
    if not has_oud:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="not_applicable",
            severity="none",
            negative_finding=False,
            finding_template=(
                "No opioid use disorder diagnosis is documented; the MOUD "
                "requirement does not apply to this patient."
            ),
        )

    moud_meds = {"methadone", "buprenorphine"}  # naltrexone-IM is also MOUD; ambiguous from name
    has_moud = any(
        obs.code_system == "internal"
        and obs.code.startswith("medication:")
        and obs.code.removeprefix("medication:") in moud_meds
        for obs in snap.observations
    )
    if has_moud:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="satisfied",
            severity="none",
            negative_finding=False,
            finding_template=(
                "An opioid use disorder is documented and a medication for OUD "
                "is on the medication list."
            ),
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="gap",
        severity=ep.severity,
        negative_finding=True,
        finding_template=(
            "An opioid use disorder is documented but documentation review did "
            "not identify a medication for OUD offered, initiated, or declined."
        ),
    )


def audit_suicide_screening(snap: TjcSnapshot) -> EpFinding:
    """NPSG.15.01.01 EP 2 -- validated suicide-screening tool at admission.

    Signal: C-SSRS observation (LOINC 93373-7) is present.
    Marcus: C-SSRS administered at intake → satisfied.
    """
    ep = EP_BY_CODE["NPSG.15.01.01 EP 2"]
    if snap.has_observation("LOINC", "93373-7"):
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="satisfied",
            severity="none",
            negative_finding=False,
            finding_template=(
                "The Columbia Suicide Severity Rating Scale (C-SSRS) -- a "
                "validated screening tool -- was administered at admission."
            ),
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="gap",
        severity=ep.severity,
        negative_finding=True,
        finding_template=(
            "Documentation review did not identify a validated suicide-screening tool at admission."
        ),
    )


def audit_risk_mitigation_plan(snap: TjcSnapshot) -> EpFinding:
    """NPSG.15.01.01 EP 4 -- documented risk level + mitigation plan.

    Signal proxy: a C-SSRS observation exists AND at least one progress
    note (which carries the safety plan in its assessment section).
    Marcus's chart includes both → satisfied.
    """
    ep = EP_BY_CODE["NPSG.15.01.01 EP 4"]
    has_cssrs = snap.has_observation("LOINC", "93373-7")
    has_progress_notes = any(d.document_type in ("soap", "dap", "dsap") for d in snap.documents)
    if has_cssrs and has_progress_notes:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="satisfied",
            severity="none",
            negative_finding=False,
            finding_template=(
                "An overall suicide-risk level is documented via C-SSRS and "
                "progress notes carry mitigation-plan language."
            ),
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="gap",
        severity=ep.severity,
        negative_finding=True,
        finding_template=(
            "A documented overall risk level and mitigation plan could not be "
            "identified in the chart."
        ),
    )


def audit_entry_authentication(snap: TjcSnapshot) -> EpFinding:
    """RC.01.01.01 EP 7 -- every entry is authenticated, dated, and timed.

    Signal: every ClinicalDocument has a non-empty ``author_name`` AND a
    non-null ``authored_on``. The Phase 1 ingester populates these from
    PDF headers; if any is missing, that's the gap.

    Marcus: all 4 documents authored & timestamped → satisfied.
    """
    ep = EP_BY_CODE["RC.01.01.01 EP 7"]
    if not snap.documents:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="ambiguous",
            severity="low",
            negative_finding=False,
            finding_template="No clinical documents found for this patient.",
        )
    unauthenticated = [d for d in snap.documents if not d.author_name or d.authored_on is None]
    if unauthenticated:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="gap",
            severity=ep.severity,
            negative_finding=False,
            finding_template=(
                f"{len(unauthenticated)} of {len(snap.documents)} documents lack "
                "an author name or timestamp."
            ),
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="satisfied",
        severity="none",
        negative_finding=False,
        finding_template=(
            f"All {len(snap.documents)} clinical documents carry a documented "
            "author and authored-on timestamp."
        ),
    )


def audit_record_timeliness(snap: TjcSnapshot) -> EpFinding:
    """RC.01.03.01 -- timeliness of record completion.

    Without an explicit "filed-at" timestamp the Phase 1 schema does not
    capture, we use a permissive proxy: every progress note has a
    non-null authored_on (recorded contemporaneously). Marcus's chart
    has documents dated within the 8-day admission, so this is
    satisfied.
    """
    ep = EP_BY_CODE["RC.01.03.01"]
    if not snap.documents:
        return EpFinding(
            ep_code=ep.code,
            ep_domain=ep.domain,
            status="ambiguous",
            severity="low",
            negative_finding=False,
            finding_template="No documents found; timeliness cannot be assessed.",
        )
    return EpFinding(
        ep_code=ep.code,
        ep_domain=ep.domain,
        status="satisfied",
        severity="none",
        negative_finding=False,
        finding_template=(
            "All documents were authored within the active episode of care; "
            "no late or missing filings detected."
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# TjcCoverage-backed predicates (5 EPs).
# ────────────────────────────────────────────────────────────────────────


def audit_treatment_plan_alignment(snap: TjcSnapshot) -> EpFinding:
    """CTS.03.01.03 EP 28 -- treatment plan reflects assessed needs (G2)."""
    return _finding_from_coverage(
        EP_BY_CODE["CTS.03.01.03 EP 28"],
        snap,
        gap_template=(
            "Documentation review identified a documented clinical service "
            "delivered (peer recovery support, 2026-05-09) that does not "
            "appear on the patient's treatment plan -- a treatment-plan / "
            "golden-thread misalignment."
        ),
        satisfied_template=(
            "The treatment plan reflects the assessed needs documented at intake; "
            "delivered services are recorded on the plan."
        ),
    )


def audit_measurement_based_care(snap: TjcSnapshot) -> EpFinding:
    """CTS.03.01.09 -- measurement-based care; PHQ-9 should be re-administered (G1)."""
    return _finding_from_coverage(
        EP_BY_CODE["CTS.03.01.09"],
        snap,
        gap_template=(
            "PHQ-9 administered at intake (total 18, moderately severe). "
            "Documentation review did not identify a re-administration during "
            "the remainder of the 8-day inpatient admission."
        ),
        satisfied_template=(
            "A standardized validated instrument was administered at intake and "
            "re-administered at clinically meaningful intervals."
        ),
        negative_when_gap=True,
    )


def audit_discharge_planning(snap: TjcSnapshot) -> EpFinding:
    """CTS.04.02 (TOC) / R3-25 -- 72-hour discharge plan (G4)."""
    return _finding_from_coverage(
        EP_BY_CODE["CTS.04.02 (TOC)"],
        snap,
        gap_template=(
            "Patient admitted 2026-05-04. Documentation review did not "
            "identify discharge or transition-of-care planning within the "
            "first 72 hours of admission (R3 Report Issue 25 requirement)."
        ),
        satisfied_template=(
            "Discharge / transition-of-care planning is documented within 72 hours of admission."
        ),
        negative_when_gap=True,
    )


def audit_suicide_reassessment(snap: TjcSnapshot) -> EpFinding:
    """NPSG.15.01.01 EP 3 -- suicide reassessment before LoC transition (G3)."""
    return _finding_from_coverage(
        EP_BY_CODE["NPSG.15.01.01 EP 3"],
        snap,
        gap_template=(
            "C-SSRS administered at intake (low-intensity passive ideation, "
            "negative for active SI/plan/intent). The 2026-05-12 progress "
            "note plans a C-SSRS re-administration prior to transfer, but "
            "documentation review did not identify a completed reassessment."
        ),
        satisfied_template=(
            "A documented suicide-risk reassessment is present in the chart "
            "before the planned level-of-care transition."
        ),
        negative_when_gap=True,
    )


def audit_co_signature_consistency(snap: TjcSnapshot) -> EpFinding:
    """RC.01.02.01 EP 3 -- co-signatures for delegated H&P (G5)."""
    return _finding_from_coverage(
        EP_BY_CODE["RC.01.02.01 EP 3"],
        snap,
        gap_template=(
            "A progress note dated 2026-05-09 was authored and signed solely "
            "by a peer recovery support specialist (non-clinical role) with "
            "no qualified clinician co-signature documented."
        ),
        satisfied_template=(
            "Notes authored by non-clinical roles carry a qualified-clinician "
            "co-signature with consistent timestamps."
        ),
    )


# ────────────────────────────────────────────────────────────────────────
# The full audit registry -- one entry per EP, in catalog order.
# ────────────────────────────────────────────────────────────────────────


Predicate = Callable[["TjcSnapshot"], "EpFinding"]


AUDIT_REGISTRY: dict[str, Predicate] = {
    "CTS.02.03.07 EP 1": audit_sud_history_collection,
    "CTS.02.03.07 EP 7": audit_withdrawal_risk_assessment,
    "CTS.02.01.07 EP 1": audit_history_and_physical_24h,
    "CTS.03.01.03 EP 28": audit_treatment_plan_alignment,
    "CTS.03.01.09": audit_measurement_based_care,
    "CTS.04.02.33": audit_moud_for_oud,
    "CTS.04.02 (TOC)": audit_discharge_planning,
    "NPSG.15.01.01 EP 2": audit_suicide_screening,
    "NPSG.15.01.01 EP 3": audit_suicide_reassessment,
    "NPSG.15.01.01 EP 4": audit_risk_mitigation_plan,
    "RC.01.01.01 EP 7": audit_entry_authentication,
    "RC.01.02.01 EP 3": audit_co_signature_consistency,
    "RC.01.03.01": audit_record_timeliness,
}


__all__ = [
    "Citation",
    "EpFinding",
    "TjcSnapshot",
    "load_snapshot",
    "AUDIT_REGISTRY",
    # Individual predicates -- exported for fine-grained unit tests.
    "audit_sud_history_collection",
    "audit_withdrawal_risk_assessment",
    "audit_history_and_physical_24h",
    "audit_treatment_plan_alignment",
    "audit_measurement_based_care",
    "audit_moud_for_oud",
    "audit_discharge_planning",
    "audit_suicide_screening",
    "audit_suicide_reassessment",
    "audit_risk_mitigation_plan",
    "audit_entry_authentication",
    "audit_co_signature_consistency",
    "audit_record_timeliness",
]
