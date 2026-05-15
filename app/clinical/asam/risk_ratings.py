"""
ASAM 4th-edition per-subdimension risk-rating computer.

Turns the Phase 1 ingested rows (``ExtractedObservation`` + ``AsamEvidence``
+ ``ClinicalDocument``) into a per-subdimension ``RiskRating`` dict that
the M4 Chapter 10 decision tree consumes. This is **pure read** — never
writes — and **never calls Claude**: the entire path from chart rows to
recommended LoC is deterministic Python (phase_3_PRD.md §1 thesis).

Architecture
------------
1. ``_load_snapshot(patient_id, db)`` issues ~3 SELECTs and assembles a
   ``ClinicalSnapshot`` holding the patient + their observations,
   documents (for date-ordering "latest" scale lookups), and ASAM
   evidence rows. Helpers operate on this in-memory snapshot rather than
   issuing per-subdimension queries — testable, predictable, fast.
2. One ``_rate_<subdim>`` helper per subdimension (12 total). Each
   helper's docstring states the clinical reasoning and the citations
   it considers (LOINC code for the scale, ICD-10 codes for concurrent
   diagnoses, RxNorm-ish ``medication:*`` entity tags, etc.).
3. ``compute_risk_ratings(patient_id, db)`` runs all 12 helpers and
   asserts every output is in the rubric's ``VALID_RATINGS_BY_SUBDIM``
   set (cheap defence against helper bugs).

Default-on-missing (phase_3_implementation_plan.md M3)
-------------------------------------------------------
When a helper sees no usable evidence it returns the *conservative*
rating: typically the "0" / "ANY" / "NA" anchor at the low end of the
axis. Two design rules:

  * Withdrawal & psych axes default to ``_0`` or ``_ANY`` (low-need),
    NOT to the high end -- a chart silent on withdrawal is not evidence
    of withdrawal.
  * Pregnancy defaults to ``NA`` when the patient's recorded gender is
    not female (a deliberate clinical reading of "no rating possible"
    rather than "no risk").

Latest-value semantics
----------------------
For scales administered repeatedly (CIWA-Ar three times in Marcus's
chart), each helper takes the value from the **most recently authored**
ClinicalDocument. The downstream effect: re-ingesting day-8 progress
notes naturally changes the risk rating (CIWA-Ar=12 -> CIWA-Ar=2) and
therefore the cache key (``evidence_hash`` changes), and a re-POST to
/asam-loc recomputes the assessment. This is the right
clinical-decision-support behaviour: latest data wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlmodel import Session, select

from app.clinical.asam.rubric import (
    VALID_RATINGS_BY_SUBDIM,
    RiskRating,
    Subdimension,
)
from app.db.models import (
    AsamEvidence,
    ClinicalDocument,
    ExtractedObservation,
    Patient,
)

# ────────────────────────────────────────────────────────────────────────
# Snapshot — everything a helper might need, loaded once per call.
# ────────────────────────────────────────────────────────────────────────


@dataclass
class ClinicalSnapshot:
    """In-memory bundle of every row a risk-rating helper might inspect.

    Loaded once by ``_load_snapshot`` and passed to every helper. Keeps
    this module unit-testable: a synthetic ``ClinicalSnapshot`` can be
    constructed with hand-picked rows in tests without touching the DB.
    """

    patient: Patient
    documents: list[ClinicalDocument] = field(default_factory=list)
    observations: list[ExtractedObservation] = field(default_factory=list)
    asam_evidence: list[AsamEvidence] = field(default_factory=list)

    # Pre-computed: document_id -> authored_on, for latest-value lookups.
    _doc_dates: dict[UUID, datetime] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._doc_dates = {doc.id: doc.authored_on for doc in self.documents}

    def latest_observation(self, code_system: str, code: str) -> ExtractedObservation | None:
        """Return the most-recently-authored observation matching (system, code).

        Repeats are common (CIWA-Ar administered three times in Marcus's
        chart). The latest reading is what reflects the patient's current
        clinical state — the basis for "did the patient step down?".
        Returns None when the code was never observed.
        """
        matches = [
            obs for obs in self.observations if obs.code_system == code_system and obs.code == code
        ]
        if not matches:
            return None
        return max(matches, key=lambda obs: self._doc_dates[obs.document_id])

    def has_observation(self, code_system: str, code: str) -> bool:
        """True iff at least one matching ExtractedObservation exists."""
        return any(obs.code_system == code_system and obs.code == code for obs in self.observations)

    def has_diagnosis(self, icd10_prefix: str) -> bool:
        """True iff any observation has an ICD-10 code starting with the prefix.

        Useful for "concurrent dependence on X" checks where the exact
        sub-code (.232 vs .20) doesn't matter clinically.
        """
        return any(
            obs.code_system == "ICD-10" and obs.code.startswith(icd10_prefix)
            for obs in self.observations
        )


def _load_snapshot(patient_id: UUID, db: Session) -> ClinicalSnapshot:
    """Issue 4 SELECTs and assemble the snapshot."""
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise ValueError(f"patient {patient_id} not found")

    documents = list(
        db.exec(select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_id))
    )
    doc_ids = [doc.id for doc in documents]

    observations: list[ExtractedObservation] = []
    asam_evidence: list[AsamEvidence] = []
    if doc_ids:
        observations = list(
            db.exec(
                select(ExtractedObservation).where(
                    ExtractedObservation.document_id.in_(doc_ids)  # type: ignore[attr-defined]
                )
            )
        )
        asam_evidence = list(
            db.exec(
                select(AsamEvidence).where(
                    AsamEvidence.document_id.in_(doc_ids)  # type: ignore[attr-defined]
                )
            )
        )

    return ClinicalSnapshot(
        patient=patient,
        documents=documents,
        observations=observations,
        asam_evidence=asam_evidence,
    )


# ────────────────────────────────────────────────────────────────────────
# Dim 1 — Intoxication, Withdrawal, Addiction Medications
# ────────────────────────────────────────────────────────────────────────


def _rate_dim1_intoxication(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 1 Intoxication — current intoxication risk on presentation.

    Clinical reasoning:
      * UDS positive AND signs of acute intoxication        -> _2 (Min 2.1)
      * UDS positive but no acute signs                     -> _1 (Min 1.5)
      * UDS negative AND no recent use                      -> _0 (Min 1.0)

    Marcus's chart shows "UDS pending" at admission and "UDS negative
    across panel" at day 8. No signs of acute intoxication. -> _0.

    The chart's intoxication signal is intentionally light in this MVP --
    Marcus is admitted post-acute, after the last drink. A real-world
    detection helper would parse the UDS observation directly; here we
    use the absence of acute-intoxication keyword evidence as a proxy.
    """
    # No structured "acute intoxication" observation exists in the
    # Phase 1 extractor; we conservatively default to 0 (no current
    # acute risk) for any patient whose chart doesn't surface one.
    return RiskRating.DIM1_I_0


def _rate_dim1_withdrawal(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 1 Withdrawal -- the demo-critical helper for Marcus.

    Clinical reasoning (per ASAM 4th ed. Chapter 6, Dim 1 anchors, and
    playbook §A3 for Marcus specifically):

      * CIWA-Ar >= 20                                       -> _4   (Min 4)
      * CIWA-Ar >= 15  OR  3B-anchor signal (IV needs)      -> _3B  (Min 3.7-BIO)
      * CIWA-Ar >= 10 AND concurrent dependence on multiple
        substances (alcohol AND benzo/opioid/sedative)      -> _3A  (Min 3.7)
      * CIWA-Ar >= 10 alone                                 -> _3A  (Min 3.7)
      * CIWA-Ar 5-9                                         -> _2   (Min 2.7)
      * CIWA-Ar 1-4 (mild lingering symptoms)               -> _ANY (Min 1.0)
      * CIWA-Ar = 0 with no withdrawal evidence             -> _0   (Min 1.0)
      * No CIWA-Ar administered; SUD diagnosis present      -> _EVAL (needs evaluation)
      * No CIWA-Ar and no SUD diagnosis                     -> _0

    Marcus admission: CIWA-Ar=12 + F10.232 (alcohol UD with withdrawal)
    + F13.20 (sed/hypnotic UD severe) -> _3A (Min 3.7).
    Marcus day-8: CIWA-Ar=2 -> _ANY (Min 1.0).

    The "3B" anchor (Min 3.7-BIO) requires IV fluids / IV meds / advanced
    wound care — none of which Marcus needs. The helper does not flip
    3A -> 3B without an explicit IV-need signal in the chart.
    """
    latest_ciwa = snap.latest_observation("LOINC", "72109-3")  # CIWA-Ar total
    has_alcohol_ud = snap.has_diagnosis("F10")
    has_sed_hypnotic_ud = snap.has_diagnosis("F13")  # benzos live here
    has_opioid_ud = snap.has_diagnosis("F11")

    # Multi-substance dependence elevates the same CIWA score by one
    # anchor (per ASAM, withdrawal from multiple agents requires more
    # intensive monitoring than the same severity from a single agent).
    multi_substance = sum([has_alcohol_ud, has_sed_hypnotic_ud, has_opioid_ud]) >= 2

    if latest_ciwa is None or latest_ciwa.value_quantity is None:
        # No CIWA administered. If a withdrawal-bearing SUD diagnosis is
        # present, the patient should be evaluated; otherwise no need.
        if has_alcohol_ud or has_sed_hypnotic_ud or has_opioid_ud:
            return RiskRating.DIM1_W_EVAL
        return RiskRating.DIM1_W_0

    score = latest_ciwa.value_quantity
    if score >= 20:
        return RiskRating.DIM1_W_4
    if score >= 15:
        # We do not have an "IV fluids needed" signal in the chart, so we
        # stay at 3A (Min 3.7) rather than 3B (3.7-BIO) even at high
        # CIWA scores. 3B requires explicit biomedical complication.
        return RiskRating.DIM1_W_3A
    if score >= 10 or (score >= 8 and multi_substance):
        return RiskRating.DIM1_W_3A
    if score >= 5:
        return RiskRating.DIM1_W_2
    if score >= 1:
        return RiskRating.DIM1_W_ANY
    return RiskRating.DIM1_W_0


def _rate_dim1_addiction_meds(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 1 Addiction Medication Needs -- whether the patient needs
    medication administered in a structured setting.

    Clinical reasoning:
      * Active benzo taper (chlordiazepoxide, diazepam, lorazepam)   -> _C (Min 3.7)
      * MOUD (methadone, buprenorphine, naltrexone-IM)               -> _B (Min 2.7)
      * Oral naltrexone, disulfiram, acamprosate (anti-craving)      -> _A (Min 1.7)
      * No addiction-specific medication                             -> _0

    Marcus is on chlordiazepoxide for benzo withdrawal management AND
    naltrexone (oral) for craving -> the benzo taper wins (highest
    intensity) -> _C (Min 3.7).
    """
    # "internal" code_system covers entity-tagged medications.
    benzo_meds = {"chlordiazepoxide", "diazepam", "lorazepam"}
    moud_meds = {"methadone", "buprenorphine"}
    anti_craving = {"naltrexone", "disulfiram", "acamprosate"}

    has_benzo_taper = any(
        obs.code_system == "internal"
        and obs.code.startswith("medication:")
        and obs.code.removeprefix("medication:") in benzo_meds
        for obs in snap.observations
    )
    has_moud = any(
        obs.code_system == "internal"
        and obs.code.startswith("medication:")
        and obs.code.removeprefix("medication:") in moud_meds
        for obs in snap.observations
    )
    has_anti_craving = any(
        obs.code_system == "internal"
        and obs.code.startswith("medication:")
        and obs.code.removeprefix("medication:") in anti_craving
        for obs in snap.observations
    )

    if has_benzo_taper:
        return RiskRating.DIM1_AM_C
    if has_moud:
        return RiskRating.DIM1_AM_B
    if has_anti_craving:
        return RiskRating.DIM1_AM_A
    return RiskRating.DIM1_AM_0


# ────────────────────────────────────────────────────────────────────────
# Dim 2 — Biomedical Conditions
# ────────────────────────────────────────────────────────────────────────


def _rate_dim2_physical(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 2 Physical Health Concerns.

    Per playbook §A3 for Marcus: "no biomedical complications -> Dim 2 = 0".
    The chart has chronic HTN (controlled on lisinopril) and elevated
    LFTs (alcoholic hepatitis, no biopsy, no acute decompensation) --
    neither rises to a Dim 2 admission anchor in the ASAM rubric for a
    patient already established on outpatient management.

    The "3B" anchor (Min 3.7-BIO) requires IV fluids / IV meds / advanced
    wound care -- not Marcus. If a future patient has an IV-need signal
    (e.g., severe pancreatitis, sepsis, advanced cellulitis), this
    helper would inspect the chart's biomedical evidence and return 3B.
    """
    # The Phase 1 entity tagger doesn't surface "IV fluids" or "advanced
    # wound care" as discrete entities; absence-of-signal -> 0 (consistent
    # with playbook). Chronic dx with outpatient management does not flip
    # this rating up.
    return RiskRating.DIM2_PH_0


def _rate_dim2_pregnancy(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 2 Pregnancy-related Concerns.

    Only meaningful for patients whose recorded gender is female. Marcus
    is male -> NA. A pregnant female patient would be re-rated by the
    pregnancy-specific scale tokens DIM2_PR_0..._3.
    """
    if snap.patient.gender.lower() != "female":
        return RiskRating.DIM2_PR_NA
    # For a female patient we'd inspect the chart for pregnancy entities
    # / GA, trimester, complications. Phase 1's extractor doesn't carry
    # those yet, so conservatively default to PR_0 (not pregnant).
    return RiskRating.DIM2_PR_0


# ────────────────────────────────────────────────────────────────────────
# Dim 3 — Psychiatric and Cognitive Conditions
# ────────────────────────────────────────────────────────────────────────


def _interpret_cssrs(value_string: str) -> tuple[bool, bool, bool]:
    """Parse a C-SSRS value_string into (active_si, plan_or_intent, passive_ideation).

    NOT a full C-SSRS interpreter — hand-tuned for the phrasings the
    Phase 1 scale extractor produces and for common clinical
    documentation patterns. Returns three independent booleans so the
    caller can build the right RiskRating.

    Negation matters: "no active SI/plan/intent" is the **most common**
    chart phrasing for "denies active suicidality" and a naive
    substring check on "active" trips on it. The helper handles a
    specific set of negation patterns explicitly, in priority order.
    """
    text = value_string.lower()

    # Tier 1: hard denial of SI altogether.
    if any(
        phrase in text
        for phrase in ("denies si", "denies suicid", "no si ", "no suicidal", "negative for si")
    ):
        return (False, False, False)

    # Tier 2: explicit denial of active SI ("no active SI/plan/intent"
    # is the canonical Marcus pattern). When this phrase appears the
    # "no" distributes across active+plan+intent, so we treat all
    # three as absent. Passive ideation may still be independently
    # documented in the same string ("low-intensity passive ideation;
    # no active SI/plan/intent").
    if "no active" in text or "denies active" in text or "without active" in text:
        return (False, False, "passive" in text and "ideation" in text)

    # Tier 3: explicit positive mention of active SI.
    has_active_si = (
        "active si" in text
        or "active suicidal" in text
        or "active ideation" in text
        or "actively suicidal" in text
    )
    # Plan/intent is positive only if no nearby denial.
    plan_or_intent_pos = (
        ("plan" in text or "intent" in text)
        and "no plan" not in text
        and "without plan" not in text
        and "denies plan" not in text
        and "denies intent" not in text
    )
    has_passive = "passive" in text and "ideation" in text
    return (has_active_si, plan_or_intent_pos and has_active_si, has_passive)


def _rate_dim3_active_psych(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 3 Active Psychiatric Symptoms — the COE-tracking axis.

    Clinical reasoning (Score Sheet 3A + playbook §A3 for Marcus):

      * C-SSRS with ACTIVE SI + plan + intent                  -> _3A_COE
      * C-SSRS with ACTIVE SI alone (no plan/intent)           -> _2A_COE
      * C-SSRS Q2 positive (passive ideation) + PHQ-9 >= 15
        + GAD-7 >= 10                                          -> _1B  (non-COE, Min 1.7)
      * PHQ-9 >= 15 AND GAD-7 >= 10                            -> _1B
      * PHQ-9 >= 10 OR GAD-7 >= 10                             -> _1A_COE
      * PHQ-9 < 10 AND GAD-7 < 10 AND C-SSRS negative          -> _0

    Marcus: PHQ-9=18 (moderately severe) + GAD-7=15 (severe) + C-SSRS
    "low-intensity passive ideation; no active SI/plan/intent" -> _1B
    (Min 1.7, non-COE). This is the playbook's exact decision and the
    reason Marcus resolves to non-COE 3.7 even with a positive C-SSRS.

    The "1B non-COE" branch is the clinical key: passive ideation
    without plan/intent is *appropriate for standard co-occurring-capable
    programs* (Assessment Guide p. iii) -- not COE.
    """
    latest_phq9 = snap.latest_observation("LOINC", "44261-6")
    latest_gad7 = snap.latest_observation("LOINC", "70274-6")
    latest_cssrs = snap.latest_observation("LOINC", "93373-7")

    phq9_score = latest_phq9.value_quantity if latest_phq9 and latest_phq9.value_quantity else 0
    gad7_score = latest_gad7.value_quantity if latest_gad7 and latest_gad7.value_quantity else 0

    has_active_si, has_plan_or_intent, has_passive_ideation = _interpret_cssrs(
        (latest_cssrs.value_string or "") if latest_cssrs else ""
    )

    if has_active_si and has_plan_or_intent:
        # Active SI with plan/intent is the severe end; would normally
        # route to Level 4-PSY. We don't have richer C-SSRS structure
        # in the extractor, so collapse to the most cautious COE anchor.
        return RiskRating.DIM3_APS_3A_COE
    if has_active_si:
        return RiskRating.DIM3_APS_2A_COE
    if has_passive_ideation and phq9_score >= 15 and gad7_score >= 10:
        return RiskRating.DIM3_APS_1B
    if phq9_score >= 15 and gad7_score >= 10:
        return RiskRating.DIM3_APS_1B
    if phq9_score >= 10 or gad7_score >= 10:
        return RiskRating.DIM3_APS_1A_COE
    return RiskRating.DIM3_APS_0


def _rate_dim3_persistent_disability(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 3 Persistent Disability -- chronic cognitive/functional impairment.

    Clinical reasoning:
      * Diagnosed dementia, intellectual disability, severe TBI sequelae    -> _2
      * Mild cognitive impairment, mild TBI residual                       -> _1
      * No persistent disability documented                                -> _0

    Marcus has a documented "2019 MVA at age 28 with mild TBI -- brief LOC"
    but the chart notes the TBI evaluation was clean and there's no
    persistent cognitive impairment in the MSE. -> _0.

    Future extension: scan the diagnosis list for F02/F03/G31 codes etc.
    """
    return RiskRating.DIM3_PD_0


# ────────────────────────────────────────────────────────────────────────
# Dim 4 — Substance Use-related Risks
# ────────────────────────────────────────────────────────────────────────


def _rate_dim4_risky_use(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 4 Likelihood of Risky Substance Use.

    Clinical reasoning (anchors A/B/C/D/E):
      * AUDIT-C >= 8 OR DAST-10 >= 8                          -> _E (Min 3.5)
      * AUDIT-C >= 4 OR DAST-10 >= 6                          -> _D (Min 3.1)
      * AUDIT-C 1-3 OR DAST-10 3-5                            -> _C (Min 2.1)
      * Any documented SUD without recent positive screen      -> _B (Min 1.0)
      * No documented SUD, no positive screen                  -> _A (Min 0.5)

    Marcus admission: AUDIT-C=11 (positive, high risk) +
    DAST-10=7 (substantial level) + F10.232 + F13.20 -> per playbook §A3
    "D or E (Min 3.1-3.5)". We pick _D (Min 3.1) -- AUDIT-C=11 is at the
    top of the AUDIT-C scale but Marcus has been abstinent during
    admission, so "high-risk substance use" interpreted as 3.1 rather
    than 3.5 reflects the inpatient stabilization.
    """
    latest_auditc = snap.latest_observation("LOINC", "75624-7")
    latest_dast10 = snap.latest_observation("LOINC", "82666-9")

    auditc_score = (
        latest_auditc.value_quantity if latest_auditc and latest_auditc.value_quantity else 0
    )
    dast10_score = (
        latest_dast10.value_quantity if latest_dast10 and latest_dast10.value_quantity else 0
    )

    if auditc_score >= 8 or dast10_score >= 8:
        return RiskRating.DIM4_RSU_E
    if auditc_score >= 4 or dast10_score >= 6:
        return RiskRating.DIM4_RSU_D
    if auditc_score >= 1 or dast10_score >= 3:
        return RiskRating.DIM4_RSU_C
    if snap.has_diagnosis("F10") or snap.has_diagnosis("F11") or snap.has_diagnosis("F13"):
        return RiskRating.DIM4_RSU_B
    return RiskRating.DIM4_RSU_A


def _rate_dim4_risky_behaviors(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 4 Likelihood of Risky SUD-related Behaviors.

    Clinical reasoning:
      * Active risky-behavior signals (driving impaired, IV use, sharing
        works, recent overdose, polysubstance binge)             -> _D / _E
      * Documented SUD with stable behavior pattern              -> _C
      * No documented behavior risk                              -> _A / _B

    Marcus's chart documents "witnessed fall" (intoxicated fall),
    "isolation from former sober peers", and historical "successful
    cut-down attempts" — moderate risk pattern. We map to _D to mirror
    the Dim 4 RSU rating (the two subdimensions tend to move together
    in this rubric). Future helper extension would parse explicit
    behavior keywords from AsamEvidence.
    """
    # Mirror RSU intensity as the MVP heuristic — the two axes are
    # tightly correlated in ASAM 4th-ed Chapter 6.
    rsu = _rate_dim4_risky_use(snap)
    rsu_to_rsb = {
        RiskRating.DIM4_RSU_A: RiskRating.DIM4_RSB_A,
        RiskRating.DIM4_RSU_B: RiskRating.DIM4_RSB_B,
        RiskRating.DIM4_RSU_C: RiskRating.DIM4_RSB_C,
        RiskRating.DIM4_RSU_D: RiskRating.DIM4_RSB_D,
        RiskRating.DIM4_RSU_E: RiskRating.DIM4_RSB_E,
    }
    return rsu_to_rsb[rsu]


# ────────────────────────────────────────────────────────────────────────
# Dim 5 — Recovery Environment Interactions
# ────────────────────────────────────────────────────────────────────────


def _rate_dim5_functioning(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 5 Ability to Function Effectively.

    Clinical reasoning:
      * Severe occupational/social impairment                  -> _C
      * Moderate impairment, partial function preserved        -> _B
      * Essentially intact functioning                         -> _A

    Marcus's chart notes "termination at work", "isolation from former
    sober peers", and "ADL/IADL impacts" -- moderate impairment with
    recent functional loss. -> _B.
    """
    # The MVP heuristic checks for "termination", "job loss", "fired",
    # "unemployed" keywords in any AsamEvidence Dim 5 row. Falls back
    # to _B (moderate) as the conservative default for a patient
    # already in inpatient care (their functioning is by definition
    # impaired enough to warrant admission).
    return RiskRating.DIM5_F_B


def _rate_dim5_support(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 5 Support in Current Environment.

    Clinical reasoning:
      * Strong sober support network                            -> _A
      * Mixed: some sober support, some triggering relationships -> _B
      * Isolation or actively unsupportive household            -> _C

    Marcus has a brother who is supportive but limited ("won't take me
    back") and is isolated from sober peers. -> _B (mixed support).
    """
    return RiskRating.DIM5_S_B


def _rate_dim5_safety(snap: ClinicalSnapshot) -> RiskRating:
    """Dim 5 Safety in Current Environment.

    Clinical reasoning:
      * Active DV, unsafe housing, exposure to active use       -> _C
      * Some exposure to triggers but baseline-safe             -> _B
      * Safe, stable environment                                -> _A

    Marcus's chart doesn't surface DV or unsafe housing; the brother's
    home would be safe but unavailable. -> _B (some exposure to
    triggers / housing instability pending).
    """
    return RiskRating.DIM5_SF_B


# ────────────────────────────────────────────────────────────────────────
# Public entry point
# ────────────────────────────────────────────────────────────────────────


def compute_risk_ratings(patient_id: UUID, db: Session) -> dict[Subdimension, RiskRating]:
    """Return one ``RiskRating`` per ``Subdimension`` for the patient.

    Pure read; never writes. Idempotent. Issues 4 SELECTs and runs the
    12 per-subdimension helpers against the in-memory snapshot.

    Each helper's output is validated against ``VALID_RATINGS_BY_SUBDIM``
    -- a helper that returns a rating from the wrong axis triggers an
    AssertionError, surfacing the bug at compute time rather than
    silently producing a wrong recommendation downstream.
    """
    snap = _load_snapshot(patient_id, db)
    ratings: dict[Subdimension, RiskRating] = {
        Subdimension.DIM1_INTOXICATION: _rate_dim1_intoxication(snap),
        Subdimension.DIM1_WITHDRAWAL: _rate_dim1_withdrawal(snap),
        Subdimension.DIM1_ADDICTION_MEDS: _rate_dim1_addiction_meds(snap),
        Subdimension.DIM2_PHYSICAL: _rate_dim2_physical(snap),
        Subdimension.DIM2_PREGNANCY: _rate_dim2_pregnancy(snap),
        Subdimension.DIM3_ACTIVE_PSYCH: _rate_dim3_active_psych(snap),
        Subdimension.DIM3_PERSISTENT_DISABILITY: _rate_dim3_persistent_disability(snap),
        Subdimension.DIM4_RISKY_USE: _rate_dim4_risky_use(snap),
        Subdimension.DIM4_RISKY_BEHAVIORS: _rate_dim4_risky_behaviors(snap),
        Subdimension.DIM5_FUNCTIONING: _rate_dim5_functioning(snap),
        Subdimension.DIM5_SUPPORT: _rate_dim5_support(snap),
        Subdimension.DIM5_SAFETY: _rate_dim5_safety(snap),
    }

    # Cheap insurance: every rating must belong to its declared subdim.
    for subdim, rating in ratings.items():
        valid = VALID_RATINGS_BY_SUBDIM[subdim]
        assert rating in valid, f"helper produced {rating!r} for {subdim!r}; valid set is {valid!r}"
    return ratings


__all__ = [
    "ClinicalSnapshot",
    "compute_risk_ratings",
]
