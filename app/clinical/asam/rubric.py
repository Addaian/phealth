"""
ASAM 4th-edition rubric — dimensions, subdimensions, symbolic risk-rating
tokens, and the rating → Min Level-of-Care mapping.

Reference: *The ASAM Criteria, Volume 1: Adults*, 4th edition (2023),
Chapter 6 (Dimensional Admission Criteria, pp. 212-278) and Chapter 10
(Level of Care Determination Rules, pp. 279-281). The UCLA ISAP
*Assessment Guide* (v4.1.0.0, Jan 2025) reproduces the Score Sheet.

ASAM IP boundary (phase_3_PRD.md §3, playbook §F)
-------------------------------------------------
ASAM publicly states "Inputting ASAM Criteria and other ASAM intellectual
property into artificial intelligence is strictly prohibited." This module
is the deterministic Python encoding of the Chapter 10 rules; **none of its
contents are ever sent to Claude**. The LLM sees only the engine's outputs
(per-subdimension ratings + recommended level + rules fired) plus the
project's own evidence spans — never this file.

This file:
  * Paraphrases the subdimension *names* in our own clinical language
    (descriptive labels, not verbatim Score Sheet entries).
  * Uses the Score Sheet's bare rating tokens ("0", "ANY", "EVAL",
    "3A", "3B", "1A", "1B", "A", "B", "C", "D", "E", "4") -- these are
    notations, not protected expression.
  * Maps each rating to its Min Level of Care -- this *is* clinical
    content; the mapping is derived from the published Determination
    Rules in Chapter 10 and our reading of the per-subdimension anchor
    descriptions. The anchor descriptions themselves are NOT reproduced
    in this codebase.

Design choices
--------------
1. ``RiskRating`` members are **namespaced by subdimension** (e.g.,
   ``DIM1_W_3A`` vs ``DIM3_APS_3A_COE``) because the same bare token
   carries different meaning across subdimensions: ``3A`` is "Min 3.7"
   in Dim 1 Withdrawal and "Min 3.7-COE" in Dim 3 Active Psychiatric
   Symptoms. Namespacing eliminates the ambiguity at the type level.

2. Ratings are ``StrEnum``, not numeric, per the playbook (§A1):
   "Risk-rating anchors are symbolic, not numeric 0-4. Treating these as
   integer scores loses information."

3. ``COE_BEARING_RATINGS`` and ``BIO_BEARING_RATINGS`` are flat sets;
   the rule engine in ``level_decision.py`` (M4) tests for membership
   rather than baking modifier semantics into RiskRating itself.

4. Coverage is **MVP-complete**: every rating Marcus's chart could surface
   is encoded, plus the edge cases the M4 decision tree needs to exercise
   (3B for BIO escalation, the Dim 3 APS COE family, Level 4). Per-rating
   anchor descriptions (the multi-paragraph clinical text in the Score
   Sheet) are intentionally absent -- those live in the source book.
"""

from enum import IntEnum, StrEnum


class Dimension(IntEnum):
    """The six ASAM 4th-edition dimensions (Chapter 6).

    Dim 6 (Person-Centered Considerations) does not carry risk ratings --
    it informs LoC *selection* (which 3.7 program to send the patient to)
    rather than LoC *recommendation* (whether 3.7 is indicated at all).
    The decision tree ignores Dim 6 by design.
    """

    DIM1 = 1  # Intoxication, Withdrawal, Addiction Medications
    DIM2 = 2  # Biomedical Conditions
    DIM3 = 3  # Psychiatric and Cognitive Conditions
    DIM4 = 4  # Substance Use-related Risks
    DIM5 = 5  # Recovery Environment Interactions
    DIM6 = 6  # Person-Centered Considerations (no ratings)


class Subdimension(StrEnum):
    """Every subdimension carrying a risk rating across Dims 1-5.

    Names paraphrase the Score Sheet labels in plain clinical language;
    the value strings are stable snake_case keys safe to use in JSON keys
    and DB columns. Dim 6 is intentionally omitted (see ``Dimension``).
    """

    # Dim 1 -- Intoxication, Withdrawal, Addiction Medications
    DIM1_INTOXICATION = "dim1_intoxication"  # Intoxication and Associated Risks
    DIM1_WITHDRAWAL = "dim1_withdrawal"  # Withdrawal and Associated Risks
    DIM1_ADDICTION_MEDS = "dim1_addiction_meds"  # Addiction Medication Needs

    # Dim 2 -- Biomedical Conditions
    DIM2_PHYSICAL = "dim2_physical"  # Physical Health Concerns
    DIM2_PREGNANCY = "dim2_pregnancy"  # Pregnancy-related Concerns

    # Dim 3 -- Psychiatric and Cognitive Conditions
    DIM3_ACTIVE_PSYCH = "dim3_active_psych"  # Active Psychiatric Symptoms
    DIM3_PERSISTENT_DISABILITY = "dim3_persistent_disability"  # Persistent Disability

    # Dim 4 -- Substance Use-related Risks
    DIM4_RISKY_USE = "dim4_risky_use"  # Likelihood of Risky Substance Use
    DIM4_RISKY_BEHAVIORS = "dim4_risky_behaviors"  # Likelihood of Risky SUD-related Behaviors

    # Dim 5 -- Recovery Environment Interactions
    DIM5_FUNCTIONING = "dim5_functioning"  # Ability to Function Effectively
    DIM5_SUPPORT = "dim5_support"  # Support in Current Environment
    DIM5_SAFETY = "dim5_safety"  # Safety in Current Environment


class MinLoc(StrEnum):
    """Symbolic Minimum Level of Care.

    The value strings are the ASAM level numbers as they appear in the
    book and in the canonical /asam-loc response (phase_3_PRD.md §6.1).
    Members are ordered ascending by clinical intensity so the rule
    engine can compare via ``max()`` over the ``_INTENSITY`` rank map
    below.

    The ``-BIO`` suffix on L3_7_BIO marks the biomedical-enhanced
    variant of 3.7 (triggered by Dim 1 or Dim 2 = 3B). ``-COE`` and
    ``-PSY`` suffixes are applied dynamically by the decision tree
    rather than baked into MinLoc, because COE is an *escalation* over
    any base level (a 2.1 + COE flag → 2.5-COE) and 4.0-PSY is a
    distinct destination, not a modifier.
    """

    L0_5 = "0.5"  # Early Intervention (subclinical / preventive)
    L1_0 = "1.0"  # Long-Term Remission Management
    L1_5 = "1.5"  # Outpatient -- low intensity
    L1_7 = "1.7"  # Medically Managed Outpatient
    L2_1 = "2.1"  # Intensive Outpatient (IOP)
    L2_5 = "2.5"  # High-Intensity Outpatient (HIOP / Partial Hospitalization)
    L2_7 = "2.7"  # Medically Managed Intensive Outpatient
    L3_1 = "3.1"  # Clinically Managed Low-Intensity Residential
    L3_5 = "3.5"  # Clinically Managed High-Intensity Residential
    L3_7 = "3.7"  # Medically Monitored Intensive Inpatient
    L3_7_BIO = "3.7-BIO"  # 3.7 + Biomedical-Enhanced
    L4 = "4.0"  # Medically Managed Inpatient
    L4_PSY = "4.0-PSY"  # Medically Managed Inpatient -- Psychiatric


# Ascending intensity rank, for ``max()`` over MinLoc values. Two BIO/PSY
# entries share the rank of their base level so the decision tree picks
# them via the explicit modifier rules in ``level_decision`` (M4) rather
# than via raw max-by-rank.
MIN_LOC_INTENSITY: dict[MinLoc, int] = {
    MinLoc.L0_5: 0,
    MinLoc.L1_0: 1,
    MinLoc.L1_5: 2,
    MinLoc.L1_7: 3,
    MinLoc.L2_1: 4,
    MinLoc.L2_5: 5,
    MinLoc.L2_7: 6,
    MinLoc.L3_1: 7,
    MinLoc.L3_5: 8,
    MinLoc.L3_7: 9,
    MinLoc.L3_7_BIO: 9,  # same rank as 3.7 -- BIO is a flag, not an intensity bump
    MinLoc.L4: 10,
    MinLoc.L4_PSY: 10,  # same rank as 4.0 -- PSY is a destination, not an intensity bump
}


class RiskRating(StrEnum):
    """All per-subdimension risk-rating tokens used by the engine.

    Members are namespaced ``<DIM>_<SUBDIM>_<TOKEN>`` so the same bare
    Score-Sheet token (``3A``) cannot accidentally cross subdimensions
    in the typed Python code. The value string carries the namespace too
    (``"dim1_w:3A"``), which is what gets persisted in the
    ``AsamAssessment.dimensions`` JSONB column.

    Coverage scope: every rating Marcus's seeded chart could surface,
    plus the edge cases the M4 decision tree needs (Dim 1/2 = 3B for
    BIO; the Dim 3 APS COE family; Level 4 / 4-PSY). Other subdimensions
    are encoded with their minimal-rating set; extending them is a
    purely additive change.
    """

    # ─── Dim 1 -- Intoxication, Withdrawal, Addiction Medications ──────
    # Withdrawal (Score Sheet 1A): 0 / ANY / EVAL / 2 / 3A / 3B / 4.
    # 3A = "needs Min Level 3.7"; 3B = "needs Min Level 3.7-BIO".
    DIM1_W_0 = "dim1_w:0"
    DIM1_W_ANY = "dim1_w:any"
    DIM1_W_EVAL = "dim1_w:eval"
    DIM1_W_2 = "dim1_w:2"
    DIM1_W_3A = "dim1_w:3A"
    DIM1_W_3B = "dim1_w:3B"
    DIM1_W_4 = "dim1_w:4"

    # Intoxication (Score Sheet 1B): 0 / 1 / 2 / 3 / 4.
    DIM1_I_0 = "dim1_i:0"
    DIM1_I_1 = "dim1_i:1"
    DIM1_I_2 = "dim1_i:2"
    DIM1_I_3 = "dim1_i:3"
    DIM1_I_4 = "dim1_i:4"

    # Addiction Medication Needs (Score Sheet 1C): 0 / A / B / C.
    # A = Min 1.7; B = Min 2.7; C = Min 3.7 (the medication-dispensing levels).
    DIM1_AM_0 = "dim1_am:0"
    DIM1_AM_A = "dim1_am:A"
    DIM1_AM_B = "dim1_am:B"
    DIM1_AM_C = "dim1_am:C"

    # ─── Dim 2 -- Biomedical Conditions ────────────────────────────────
    # Physical Health (Score Sheet 2A): 0 / 1 / 2 / 3A / 3B / 4.
    DIM2_PH_0 = "dim2_ph:0"
    DIM2_PH_1 = "dim2_ph:1"
    DIM2_PH_2 = "dim2_ph:2"
    DIM2_PH_3A = "dim2_ph:3A"
    DIM2_PH_3B = "dim2_ph:3B"
    DIM2_PH_4 = "dim2_ph:4"

    # Pregnancy (Score Sheet 2B): NA (not pregnant; default for male patients)
    # / 0 / 1 / 2 / 3 -- a slimmer scale than Physical Health.
    DIM2_PR_NA = "dim2_pr:na"
    DIM2_PR_0 = "dim2_pr:0"
    DIM2_PR_1 = "dim2_pr:1"
    DIM2_PR_2 = "dim2_pr:2"
    DIM2_PR_3 = "dim2_pr:3"

    # ─── Dim 3 -- Psychiatric and Cognitive Conditions ─────────────────
    # Active Psychiatric Symptoms (Score Sheet 3A): the COE-tracking axis.
    # 0 / 1A-COE / 1B (non-COE) / 2A-COE / 2B / 3A-COE / 3B-COE / 4-PSY.
    # Per playbook §A2 "most ... ratings (1A through 3B) map to COE
    # levels" but a *non*-COE 1B exists for "suicidal thoughts without
    # significant impulses" -- this is Marcus's rating (PHQ-9=18 +
    # GAD-7=15 + C-SSRS Q2 positive, no plan/intent).
    DIM3_APS_0 = "dim3_aps:0"
    DIM3_APS_1A_COE = "dim3_aps:1A_coe"
    DIM3_APS_1B = "dim3_aps:1B"  # non-COE; Marcus admission
    DIM3_APS_2A_COE = "dim3_aps:2A_coe"
    DIM3_APS_2B = "dim3_aps:2B"
    DIM3_APS_3A_COE = "dim3_aps:3A_coe"
    DIM3_APS_3B_COE = "dim3_aps:3B_coe"
    DIM3_APS_4_PSY = "dim3_aps:4_psy"  # routes to Level 4-Psychiatric

    # Persistent Disability (Score Sheet 3B): 0 / 1 / 2.
    DIM3_PD_0 = "dim3_pd:0"
    DIM3_PD_1 = "dim3_pd:1"
    DIM3_PD_2 = "dim3_pd:2"

    # ─── Dim 4 -- Substance Use-related Risks ──────────────────────────
    # Likelihood of Risky Substance Use (Score Sheet 4A): A/B/C/D/E.
    # Alphabetic intensity; A is "no risk", E is "very high risk".
    # D corresponds to Min 3.1; E to Min 3.5 per Chapter 10 mapping.
    DIM4_RSU_A = "dim4_rsu:A"
    DIM4_RSU_B = "dim4_rsu:B"
    DIM4_RSU_C = "dim4_rsu:C"
    DIM4_RSU_D = "dim4_rsu:D"
    DIM4_RSU_E = "dim4_rsu:E"

    # Likelihood of Risky SUD-related Behaviors (Score Sheet 4B): A-E.
    DIM4_RSB_A = "dim4_rsb:A"
    DIM4_RSB_B = "dim4_rsb:B"
    DIM4_RSB_C = "dim4_rsb:C"
    DIM4_RSB_D = "dim4_rsb:D"
    DIM4_RSB_E = "dim4_rsb:E"

    # ─── Dim 5 -- Recovery Environment Interactions ────────────────────
    # All three Dim 5 subdimensions use the A/B/C scale (Score Sheet 5A-C).
    # C is "highest risk / strongest indication for residential or higher".
    DIM5_F_A = "dim5_f:A"  # Ability to Function Effectively
    DIM5_F_B = "dim5_f:B"
    DIM5_F_C = "dim5_f:C"

    DIM5_S_A = "dim5_s:A"  # Support in Current Environment
    DIM5_S_B = "dim5_s:B"
    DIM5_S_C = "dim5_s:C"

    DIM5_SF_A = "dim5_sf:A"  # Safety in Current Environment
    DIM5_SF_B = "dim5_sf:B"
    DIM5_SF_C = "dim5_sf:C"


# ────────────────────────────────────────────────────────────────────────
# Rating → Min Level of Care mapping.
#
# Derived from ASAM 4th ed. Chapter 6 (per-subdimension anchor tables)
# and Chapter 10 (Level of Care Determination Rules, pp. 279-281). The
# rule engine in ``level_decision.py`` takes the MAX across populated
# subdimensions and then applies BIO/COE/PSY escalation rules.
#
# "Not rated" subdimensions (no signal in the chart) are absent from the
# input dict to ``decide()`` -- they are NOT mapped to L0_5/L1_0 here.
# Defaulting on missing happens in ``risk_ratings.py`` (M3).
# ────────────────────────────────────────────────────────────────────────
MIN_LOC_BY_RATING: dict[RiskRating, MinLoc] = {
    # Dim 1 Withdrawal
    RiskRating.DIM1_W_0: MinLoc.L1_0,
    RiskRating.DIM1_W_ANY: MinLoc.L1_0,
    RiskRating.DIM1_W_EVAL: MinLoc.L1_5,
    RiskRating.DIM1_W_2: MinLoc.L2_7,
    RiskRating.DIM1_W_3A: MinLoc.L3_7,
    RiskRating.DIM1_W_3B: MinLoc.L3_7_BIO,
    RiskRating.DIM1_W_4: MinLoc.L4,
    # Dim 1 Intoxication
    RiskRating.DIM1_I_0: MinLoc.L1_0,
    RiskRating.DIM1_I_1: MinLoc.L1_5,
    RiskRating.DIM1_I_2: MinLoc.L2_1,
    RiskRating.DIM1_I_3: MinLoc.L3_1,
    RiskRating.DIM1_I_4: MinLoc.L4,
    # Dim 1 Addiction Medication Needs
    RiskRating.DIM1_AM_0: MinLoc.L1_0,
    RiskRating.DIM1_AM_A: MinLoc.L1_7,
    RiskRating.DIM1_AM_B: MinLoc.L2_7,
    RiskRating.DIM1_AM_C: MinLoc.L3_7,
    # Dim 2 Physical Health
    RiskRating.DIM2_PH_0: MinLoc.L1_0,
    RiskRating.DIM2_PH_1: MinLoc.L1_5,
    RiskRating.DIM2_PH_2: MinLoc.L2_1,
    RiskRating.DIM2_PH_3A: MinLoc.L3_7,
    RiskRating.DIM2_PH_3B: MinLoc.L3_7_BIO,
    RiskRating.DIM2_PH_4: MinLoc.L4,
    # Dim 2 Pregnancy
    RiskRating.DIM2_PR_NA: MinLoc.L0_5,
    RiskRating.DIM2_PR_0: MinLoc.L1_0,
    RiskRating.DIM2_PR_1: MinLoc.L1_5,
    RiskRating.DIM2_PR_2: MinLoc.L2_1,
    RiskRating.DIM2_PR_3: MinLoc.L3_1,
    # Dim 3 Active Psychiatric Symptoms
    RiskRating.DIM3_APS_0: MinLoc.L1_0,
    RiskRating.DIM3_APS_1A_COE: MinLoc.L1_7,
    RiskRating.DIM3_APS_1B: MinLoc.L1_7,
    RiskRating.DIM3_APS_2A_COE: MinLoc.L2_5,
    RiskRating.DIM3_APS_2B: MinLoc.L2_5,
    RiskRating.DIM3_APS_3A_COE: MinLoc.L3_5,
    RiskRating.DIM3_APS_3B_COE: MinLoc.L3_7,
    RiskRating.DIM3_APS_4_PSY: MinLoc.L4_PSY,
    # Dim 3 Persistent Disability
    RiskRating.DIM3_PD_0: MinLoc.L1_0,
    RiskRating.DIM3_PD_1: MinLoc.L1_5,
    RiskRating.DIM3_PD_2: MinLoc.L2_1,
    # Dim 4 Likelihood of Risky Substance Use.
    # C → L2_5 per playbook §A3 ("Dim 4 drops to C (Min 2.5)"): the C
    # anchor is the clinically-managed-outpatient HIOP threshold that
    # drives Marcus's day-8 step-down to Level 2.5. Earlier mappings
    # had C → L2_1 which would route via Level 2.1 and miss the demo.
    RiskRating.DIM4_RSU_A: MinLoc.L0_5,
    RiskRating.DIM4_RSU_B: MinLoc.L1_0,
    RiskRating.DIM4_RSU_C: MinLoc.L2_5,
    RiskRating.DIM4_RSU_D: MinLoc.L3_1,
    RiskRating.DIM4_RSU_E: MinLoc.L3_5,
    # Dim 4 Likelihood of Risky SUD-related Behaviors -- mirrors RSU.
    RiskRating.DIM4_RSB_A: MinLoc.L0_5,
    RiskRating.DIM4_RSB_B: MinLoc.L1_0,
    RiskRating.DIM4_RSB_C: MinLoc.L2_5,
    RiskRating.DIM4_RSB_D: MinLoc.L3_1,
    RiskRating.DIM4_RSB_E: MinLoc.L3_5,
    # Dim 5 Functioning / Support / Safety -- A/B/C across all three.
    # Dim 5 informs the recovery-environment side of the LoC decision;
    # the highest Dim 5 rating across the three subdimensions adds at
    # most a "recovery residence" indication, capped at L3_1 by Chapter
    # 10's logic. C is the strongest indication for residential.
    RiskRating.DIM5_F_A: MinLoc.L1_0,
    RiskRating.DIM5_F_B: MinLoc.L2_1,
    RiskRating.DIM5_F_C: MinLoc.L3_1,
    RiskRating.DIM5_S_A: MinLoc.L1_0,
    RiskRating.DIM5_S_B: MinLoc.L2_1,
    RiskRating.DIM5_S_C: MinLoc.L3_1,
    RiskRating.DIM5_SF_A: MinLoc.L1_0,
    RiskRating.DIM5_SF_B: MinLoc.L2_1,
    RiskRating.DIM5_SF_C: MinLoc.L3_1,
}


# ────────────────────────────────────────────────────────────────────────
# COE escalation: any rating in this set, when assigned to its
# subdimension, makes the engine apply the Co-Occurring Enhanced
# modifier to the final recommendation (phase_3_PRD.md §5.4 step 5).
#
# The Dim 3 APS scale is the only axis whose tokens are inherently
# COE-bearing (per the Score Sheet's COE flags). Marcus's 1B is
# explicitly *non*-COE, which is why he resolves to non-COE 3.7 even
# though he has a positive C-SSRS.
# ────────────────────────────────────────────────────────────────────────
COE_BEARING_RATINGS: frozenset[RiskRating] = frozenset(
    {
        RiskRating.DIM3_APS_1A_COE,
        RiskRating.DIM3_APS_2A_COE,
        RiskRating.DIM3_APS_3A_COE,
        RiskRating.DIM3_APS_3B_COE,
    }
)


# ────────────────────────────────────────────────────────────────────────
# BIO escalation: per Chapter 10, BIO triggers when Dim 1 OR Dim 2 hits
# 3B (the "needs IV fluids / IV meds / advanced wound care" anchor).
# Combined 3.7-BIO + any COE -> Level 4 (phase_3_PRD.md §5.4 step 6).
# ────────────────────────────────────────────────────────────────────────
BIO_BEARING_RATINGS: frozenset[RiskRating] = frozenset(
    {
        RiskRating.DIM1_W_3B,
        RiskRating.DIM2_PH_3B,
    }
)


# ────────────────────────────────────────────────────────────────────────
# Human-readable display names for each MinLoc, used in the API response
# under ``recommendation.level_display`` (phase_3_PRD.md §6.1).
# Names follow ASAM 4th-edition Chapter 5 LoC naming.
# ────────────────────────────────────────────────────────────────────────
MIN_LOC_DISPLAY: dict[MinLoc, str] = {
    MinLoc.L0_5: "Early Intervention",
    MinLoc.L1_0: "Long-Term Remission Management",
    MinLoc.L1_5: "Outpatient",
    MinLoc.L1_7: "Medically Managed Outpatient",
    MinLoc.L2_1: "Intensive Outpatient",
    MinLoc.L2_5: "High-Intensity Outpatient",
    MinLoc.L2_7: "Medically Managed Intensive Outpatient",
    MinLoc.L3_1: "Clinically Managed Low-Intensity Residential",
    MinLoc.L3_5: "Clinically Managed High-Intensity Residential",
    MinLoc.L3_7: "Medically Monitored Intensive Inpatient",
    MinLoc.L3_7_BIO: "Medically Monitored Intensive Inpatient (Biomedical-Enhanced)",
    MinLoc.L4: "Medically Managed Inpatient",
    MinLoc.L4_PSY: "Medically Managed Inpatient (Psychiatric)",
}


# ────────────────────────────────────────────────────────────────────────
# Inverse lookup: a Subdimension to the set of RiskRating values that
# are valid for it. The risk-rating computer (M3) uses this to assert
# its outputs against the rubric at runtime, catching bugs where a
# helper accidentally assigns (say) DIM3_APS_1B to Dim 1 Withdrawal.
# ────────────────────────────────────────────────────────────────────────
VALID_RATINGS_BY_SUBDIM: dict[Subdimension, frozenset[RiskRating]] = {
    Subdimension.DIM1_WITHDRAWAL: frozenset(
        {
            RiskRating.DIM1_W_0,
            RiskRating.DIM1_W_ANY,
            RiskRating.DIM1_W_EVAL,
            RiskRating.DIM1_W_2,
            RiskRating.DIM1_W_3A,
            RiskRating.DIM1_W_3B,
            RiskRating.DIM1_W_4,
        }
    ),
    Subdimension.DIM1_INTOXICATION: frozenset(
        {
            RiskRating.DIM1_I_0,
            RiskRating.DIM1_I_1,
            RiskRating.DIM1_I_2,
            RiskRating.DIM1_I_3,
            RiskRating.DIM1_I_4,
        }
    ),
    Subdimension.DIM1_ADDICTION_MEDS: frozenset(
        {
            RiskRating.DIM1_AM_0,
            RiskRating.DIM1_AM_A,
            RiskRating.DIM1_AM_B,
            RiskRating.DIM1_AM_C,
        }
    ),
    Subdimension.DIM2_PHYSICAL: frozenset(
        {
            RiskRating.DIM2_PH_0,
            RiskRating.DIM2_PH_1,
            RiskRating.DIM2_PH_2,
            RiskRating.DIM2_PH_3A,
            RiskRating.DIM2_PH_3B,
            RiskRating.DIM2_PH_4,
        }
    ),
    Subdimension.DIM2_PREGNANCY: frozenset(
        {
            RiskRating.DIM2_PR_NA,
            RiskRating.DIM2_PR_0,
            RiskRating.DIM2_PR_1,
            RiskRating.DIM2_PR_2,
            RiskRating.DIM2_PR_3,
        }
    ),
    Subdimension.DIM3_ACTIVE_PSYCH: frozenset(
        {
            RiskRating.DIM3_APS_0,
            RiskRating.DIM3_APS_1A_COE,
            RiskRating.DIM3_APS_1B,
            RiskRating.DIM3_APS_2A_COE,
            RiskRating.DIM3_APS_2B,
            RiskRating.DIM3_APS_3A_COE,
            RiskRating.DIM3_APS_3B_COE,
            RiskRating.DIM3_APS_4_PSY,
        }
    ),
    Subdimension.DIM3_PERSISTENT_DISABILITY: frozenset(
        {
            RiskRating.DIM3_PD_0,
            RiskRating.DIM3_PD_1,
            RiskRating.DIM3_PD_2,
        }
    ),
    Subdimension.DIM4_RISKY_USE: frozenset(
        {
            RiskRating.DIM4_RSU_A,
            RiskRating.DIM4_RSU_B,
            RiskRating.DIM4_RSU_C,
            RiskRating.DIM4_RSU_D,
            RiskRating.DIM4_RSU_E,
        }
    ),
    Subdimension.DIM4_RISKY_BEHAVIORS: frozenset(
        {
            RiskRating.DIM4_RSB_A,
            RiskRating.DIM4_RSB_B,
            RiskRating.DIM4_RSB_C,
            RiskRating.DIM4_RSB_D,
            RiskRating.DIM4_RSB_E,
        }
    ),
    Subdimension.DIM5_FUNCTIONING: frozenset(
        {RiskRating.DIM5_F_A, RiskRating.DIM5_F_B, RiskRating.DIM5_F_C}
    ),
    Subdimension.DIM5_SUPPORT: frozenset(
        {RiskRating.DIM5_S_A, RiskRating.DIM5_S_B, RiskRating.DIM5_S_C}
    ),
    Subdimension.DIM5_SAFETY: frozenset(
        {RiskRating.DIM5_SF_A, RiskRating.DIM5_SF_B, RiskRating.DIM5_SF_C}
    ),
}


__all__ = [
    "Dimension",
    "Subdimension",
    "MinLoc",
    "MIN_LOC_INTENSITY",
    "RiskRating",
    "MIN_LOC_BY_RATING",
    "COE_BEARING_RATINGS",
    "BIO_BEARING_RATINGS",
    "MIN_LOC_DISPLAY",
    "VALID_RATINGS_BY_SUBDIM",
]
