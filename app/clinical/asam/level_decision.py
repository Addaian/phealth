"""
ASAM 4th-edition Chapter 10 Level-of-Care Determination Rules.

Pure-Python implementation of the rules published in *The ASAM Criteria,
Volume 1: Adults*, 4th ed., Chapter 10 (pp. 279-281). Input: a dict from
``Subdimension`` to ``RiskRating`` (the output of M3's
``compute_risk_ratings``). Output: a ``LevelDecision`` carrying the
recommended level, the modifiers applied, and the human-readable list of
rules that fired during the cascade.

Why a hand-coded cascade and not a scoring algorithm?
-----------------------------------------------------
The 4th edition's logic is structurally a **decision tree**, not a sum
or weighted score. Highest-rating-wins across subdimensions, with
modifier rules layered on top (BIO from Dim 1/2 = 3B; COE from
COE-bearing Dim 3 APS ratings; Level-4 escalation when BIO + COE
co-occur). This is small enough to express as an ~80-line cascade and
big enough that "if-elif" is genuinely easier to read than any data
structure.

Cascade order (Chapter 10 pp. 279-281, summarized)
---------------------------------------------------
1. **Inpatient** (Level 4 / 4-PSY):
   a. Any subdim requires Level 4 -> recommend Level 4.
   b. 3.7-BIO indication AND any COE-bearing rating -> escalate to Level 4.
   c. Any subdim requires Level 4-PSY (and 1a/1b did not fire) -> Level 4-PSY.

2. **Medically managed** (1.7 / 2.7 / 3.7 / 3.7-BIO):
   The "medically managed" levels are 1.7, 2.7, 3.7, 3.7-BIO. If any
   subdim's Min LoC is one of these, this branch wins over any
   clinically-managed level.
   - 3B in Dim 1 or Dim 2 -> 3.7-BIO (biomedical-enhanced)
   - Any subdim Min Level 3 -> 3.7
   - Any subdim Min Level 2 -> 2.7
   - Else lowest -> 1.7

3. **Clinically managed residential** (3.1 / 3.5):
   - Any Min 3.5 -> 3.5
   - Else -> 3.1

4. **Clinically managed outpatient** (1.5 / 2.1 / 2.5):
   - Highest of {2.5, 2.1, 1.5} wins.

5. **Below outpatient**: 1.0 (Long-Term Remission Management) or 0.5
   (Early Intervention).

After the base level is determined, two modifier passes apply:

  * **BIO modifier**: already baked into the base level (e.g., 3.7-BIO).
    The ``biomedical_enhanced`` flag reflects this.
  * **COE escalation**: if any COE-bearing rating is present:
      * 3.1 upgraded by COE -> 3.5
      * 2.1 upgraded by COE -> 2.5
      * Append "-COE" suffix to the final level token
    The ``co_occurring_enhanced`` flag reflects this.

The combination BIO + COE never co-occurs in the output: rule 1b
escalates that case to Level 4 before the modifier pass.

ASAM IP boundary (phase_3_PRD.md §3, playbook §F)
-------------------------------------------------
The contents of this module — including the rule strings appended to
``rules_fired`` — are paraphrased project-internal language. None of
them quote the ASAM Score Sheet or Chapter 10 verbatim. They are sent
to Claude as part of the narration prompt (phase_3_PRD.md §5.6), which
is allowed because they are *our* derived clinical decisions, not the
proprietary rubric text.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.clinical.asam.rubric import (
    BIO_BEARING_RATINGS,
    COE_BEARING_RATINGS,
    MIN_LOC_BY_RATING,
    MinLoc,
    RiskRating,
    Subdimension,
)

# ────────────────────────────────────────────────────────────────────────
# Level-class membership sets.
#
# These map the ASAM Chapter 5 LoC taxonomy onto our MinLoc enum. The
# decision cascade dispatches on these set memberships rather than on
# magic-string comparisons — easier to read, easier to test.
# ────────────────────────────────────────────────────────────────────────

# "Medically managed" levels: the patient needs medical oversight, not
# just clinical management. 3.7-BIO is a sub-variant of 3.7.
MEDICALLY_MANAGED_LOCS: frozenset[MinLoc] = frozenset(
    {MinLoc.L1_7, MinLoc.L2_7, MinLoc.L3_7, MinLoc.L3_7_BIO}
)

# "Clinically managed residential": 24-hour clinical staffing without
# the medical-management overhead of 3.7.
CLINICALLY_MANAGED_RESIDENTIAL_LOCS: frozenset[MinLoc] = frozenset({MinLoc.L3_1, MinLoc.L3_5})

# "Clinically managed outpatient": non-residential outpatient tiers.
CLINICALLY_MANAGED_OUTPATIENT_LOCS: frozenset[MinLoc] = frozenset(
    {MinLoc.L1_5, MinLoc.L2_1, MinLoc.L2_5}
)


# ────────────────────────────────────────────────────────────────────────
# Output model.
# ────────────────────────────────────────────────────────────────────────


class LevelDecision(BaseModel):
    """The recommendation produced by ``decide`` -- ships into the API
    response under ``recommendation`` (phase_3_PRD.md §6.1).

    ``level`` is the canonical level token, possibly with suffixes:
        - Plain:   "1.0" | "1.5" | "1.7" | "2.1" | "2.5" | "2.7"
                 | "3.1" | "3.5" | "3.7" | "4.0"
        - BIO:     "3.7-BIO"
        - PSY:     "4.0-PSY"
        - COE:     any of the above with a "-COE" suffix appended
                   (e.g., "3.5-COE", "2.5-COE", "1.7-COE")

    ``modifiers`` contains "COE" when the COE escalation fired; BIO is
    NOT listed as a modifier here because it's already part of the level
    token (3.7 vs 3.7-BIO are distinct ASAM levels, not the same level
    with a flag).
    """

    level: str
    modifiers: list[str] = Field(default_factory=list)
    co_occurring_enhanced: bool
    biomedical_enhanced: bool
    # Ordered list of rule strings that contributed to the decision.
    # Surfaced in the API response and the LLM narration prompt
    # (phase_3_PRD.md §6.6) so reviewers can audit the engine's reasoning.
    rules_fired: list[str] = Field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────
# Helpers (kept private; decide() is the public entry point).
# ────────────────────────────────────────────────────────────────────────


def _determine_base_level(min_locs: set[MinLoc], any_bio: bool) -> tuple[str, str]:
    """Run the cascade and return ``(base_level, rule_string)``.

    The cascade is "fall through to the next tier when this tier has no
    members". Empty input or input below outpatient falls through to
    Level 1.0 or 0.5 at the bottom.

    ``any_bio`` is passed separately because the 3.7-BIO branch fires
    when EITHER ``MinLoc.L3_7_BIO`` is in min_locs OR a 3B Dim 1/2
    rating is present in the original ratings dict (which maps to
    L3_7_BIO via MIN_LOC_BY_RATING, so the second check is
    semantically redundant — kept for safety/explicitness).
    """
    # Medically managed wins over residential and outpatient because
    # "this patient needs medical oversight" is a stronger signal than
    # "this patient needs intensive treatment".
    if min_locs & MEDICALLY_MANAGED_LOCS:
        if MinLoc.L3_7_BIO in min_locs or any_bio:
            return ("3.7-BIO", "3B in Dim 1 or Dim 2 → recommend Level 3.7-BIO")
        if MinLoc.L3_7 in min_locs:
            return ("3.7", "Any subdimension Min Level 3 → recommend Level 3.7")
        if MinLoc.L2_7 in min_locs:
            return ("2.7", "Any subdimension Min Level 2 medically managed → recommend Level 2.7")
        return ("1.7", "Lowest medically managed level → recommend Level 1.7")

    if min_locs & CLINICALLY_MANAGED_RESIDENTIAL_LOCS:
        if MinLoc.L3_5 in min_locs:
            return (
                "3.5",
                "Any subdimension Min Level 3.5 → recommend Level 3.5",
            )
        return ("3.1", "Clinically managed residential indicated → recommend Level 3.1")

    if min_locs & CLINICALLY_MANAGED_OUTPATIENT_LOCS:
        if MinLoc.L2_5 in min_locs:
            return (
                "2.5",
                "Highest clinically managed outpatient rating = 2.5 → recommend Level 2.5",
            )
        if MinLoc.L2_1 in min_locs:
            return (
                "2.1",
                "Highest clinically managed outpatient rating = 2.1 → recommend Level 2.1",
            )
        return ("1.5", "Clinically managed outpatient → recommend Level 1.5")

    if MinLoc.L1_0 in min_locs:
        return ("1.0", "Below outpatient threshold → recommend Level 1.0 (Long-Term Remission)")
    return ("0.5", "No service indicated → recommend Level 0.5 (Early Intervention)")


# ────────────────────────────────────────────────────────────────────────
# Public entry point.
# ────────────────────────────────────────────────────────────────────────


def decide(ratings: dict[Subdimension, RiskRating]) -> LevelDecision:
    """Translate per-subdimension ratings into a LevelDecision.

    Pure function — no DB, no side effects, no randomness. Deterministic
    by construction; the same input dict ALWAYS produces the same
    LevelDecision (the basis for the Phase 3 cache contract,
    phase_3_PRD.md §5.7).

    Algorithm:
      1. Map every rating to its Min LoC via the rubric.
      2. Compute the BIO/COE/PSY/Level-4 flags from the original ratings.
      3. Run the four inpatient escalation rules first (Level 4 / 4-PSY).
      4. Run the cascade to determine the base level.
      5. Apply COE escalation (upgrades 3.1->3.5 and 2.1->2.5) and the
         "-COE" suffix.
      6. Build the LevelDecision.
    """
    # Step 1+2: flags + min_locs.
    min_locs: set[MinLoc] = {MIN_LOC_BY_RATING[r] for r in ratings.values()}
    any_coe = any(r in COE_BEARING_RATINGS for r in ratings.values())
    any_bio = any(r in BIO_BEARING_RATINGS for r in ratings.values())
    has_psy = MinLoc.L4_PSY in min_locs

    rules_fired: list[str] = []

    # Step 3: inpatient escalations.
    # 3a — explicit Level 4 anywhere wins outright.
    if MinLoc.L4 in min_locs:
        rules_fired.append("Any subdimension requires Level 4 → recommend Level 4")
        return LevelDecision(
            level="4.0",
            modifiers=[],
            co_occurring_enhanced=False,
            biomedical_enhanced=False,
            rules_fired=rules_fired,
        )

    # 3b — 3.7-BIO + COE escalates to Level 4 per Chapter 10 p. 280.
    if any_bio and any_coe:
        rules_fired.append("3.7-BIO indication AND COE-bearing rating → escalate to Level 4")
        return LevelDecision(
            level="4.0",
            modifiers=[],
            co_occurring_enhanced=False,
            biomedical_enhanced=False,
            rules_fired=rules_fired,
        )

    # 3c — Level 4-PSY when there's a psychiatric Level-4 indication and
    # neither 3a nor 3b fired. (If we got past 3a, no L4 in min_locs.)
    if has_psy:
        rules_fired.append(
            "Level 4-PSY indication; no Level 4 or 3.7-BIO → recommend Level 4.0-PSY"
        )
        return LevelDecision(
            level="4.0-PSY",
            modifiers=[],
            co_occurring_enhanced=False,
            biomedical_enhanced=False,
            rules_fired=rules_fired,
        )

    # Step 4: base-level cascade.
    base, base_rule = _determine_base_level(min_locs, any_bio)
    rules_fired.append(base_rule)
    biomedical_enhanced = base.endswith("-BIO")

    # Step 5: COE escalation.
    modifiers: list[str] = []
    co_occurring_enhanced = False
    if any_coe:
        # 3.1 upgrades to 3.5 with COE; 2.1 upgrades to 2.5.
        if base == "3.1":
            base = "3.5"
            rules_fired.append("COE escalation: 3.1 upgraded to 3.5 (Chapter 10 modifier rule)")
        elif base == "2.1":
            base = "2.5"
            rules_fired.append("COE escalation: 2.1 upgraded to 2.5 (Chapter 10 modifier rule)")
        modifiers.append("COE")
        co_occurring_enhanced = True
        rules_fired.append("COE-bearing subdimension rating present → apply COE modifier")

    # Build the final level token. BIO is already in `base` (e.g., "3.7-BIO");
    # COE is appended here as a suffix.
    suffix = "-COE" if co_occurring_enhanced else ""
    level = f"{base}{suffix}"

    return LevelDecision(
        level=level,
        modifiers=modifiers,
        co_occurring_enhanced=co_occurring_enhanced,
        biomedical_enhanced=biomedical_enhanced,
        rules_fired=rules_fired,
    )


__all__ = [
    "LevelDecision",
    "decide",
    "MEDICALLY_MANAGED_LOCS",
    "CLINICALLY_MANAGED_RESIDENTIAL_LOCS",
    "CLINICALLY_MANAGED_OUTPATIENT_LOCS",
]
