"""
ASAM 4th-edition Level-of-Care prediction.

Modules (populated milestone-by-milestone per
``documents/phase_3_implementation_plan.md``):

  * ``rubric``         -- subdimension StrEnums, symbolic risk-rating
                          tokens, Min-LoC mapping (M2 — this module).
  * ``risk_ratings``   -- ExtractedObservation + AsamEvidence rows ->
                          per-subdimension RiskRating (M3).
  * ``level_decision`` -- Chapter 10 Determination Rules
                          (ASAM 4th ed. pp. 279-281) as a pure-Python
                          decision tree (M4).
  * ``narration``      -- Claude prompt + parser; produces the cited
                          rationale around the engine's decision (M8).
  * ``schemas``        -- Pydantic response models matching the canonical
                          AsamAssessmentRead shape (phase_3_PRD.md §6.1).

The reasoning is split this way so the deterministic core (rubric +
risk_ratings + level_decision) can be unit-tested and run end-to-end
*without* ever calling Claude. The LLM only narrates the engine's
already-computed decision (phase_3_PRD.md §1 thesis).
"""
