"""
Phase 3 clinical-reasoning package.

This subtree carries the deterministic clinical reasoning that backs the two
Phase 3 endpoints documented in ``documents/phase_3_PRD.md``:

  * ``app.clinical.asam``  -- ASAM 4th-edition Level-of-Care prediction
                              (Chapter 10 Determination Rules as Python).
  * ``app.clinical.tjc``   -- Joint Commission compliance audit over a 13-EP
                              catalog (paraphrased from public R3 reports).
  * ``app.clinical.llm``   -- Claude client + Citations API + structured
                              output + post-hoc citation validation.
  * ``app.clinical.shared``-- evidence retrieval, evidence-hash, response
                              builders shared by both reasoning paths.

The package is import-only at this point in Phase 3 (M2 of M0-M13). Modules
are populated milestone-by-milestone per
``documents/phase_3_implementation_plan.md``.

Design intent (phase_3_PRD.md §1 thesis): the *recommendation* is
deterministic Python; the *rationale* is LLM-narrated. No ASAM or TJC
rubric text ever enters a Claude prompt; only the engine's outputs and the
project's own evidence spans do.
"""
