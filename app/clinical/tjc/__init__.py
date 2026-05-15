"""
Joint Commission compliance-audit package.

Modules (populated per documents/phase_3_implementation_plan.md M5/M9):

  * ``ep_catalog``        -- the 13-EP table (paraphrased from public R3
                             reports; never verbatim CAMBHC text). M5.
  * ``audit_functions``   -- one Python predicate per EP, returning an
                             ``EpFinding``. Reads ``TjcCoverage`` rows
                             for the 8 EPs the Phase 1 ingester already
                             classified, and ``ExtractedObservation`` /
                             ``ClinicalDocument`` directly for the 5
                             additional EPs the PRD requires. M5.
  * ``runner``            -- ``run_audit(patient_id, db)`` fans out the
                             13 predicates and returns the list. M5.
  * ``narration``         -- Claude batch prompt + parser for the
                             surveyor-RFI rationales. M9.
  * ``schemas``           -- Pydantic models matching the canonical
                             TjcAuditRead shape (phase_3_PRD.md §6.2). M9.

The catalog is designed so Marcus's chart produces exactly the
distribution the PRD success-metrics section requires: 5 gaps (G1-G5),
1 n/a (no opioid use disorder), 7 satisfied (phase_3_PRD.md §6.2).
"""
