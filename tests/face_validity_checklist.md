# Synthetic-Chart Face-Validity Checklist

A manual quality-control pass over the synthetic chart before it is treated as
the Phase-2 input contract. Structure follows the research playbook
(`documents/phase_1.md`, §Step 8). Where an item is enforced by an automated
test, the test is named so the check is repeatable.

**Run date:** 2026-05-14
**Chart:** Marcus J. Reyes — `data/synthetic_export/Marcus Reyes/` (1 BPS intake + 3 progress notes)
**Result:** PASS (with one documented limitation — item 6)

---

- [x] **1. Coherence — the clinical trajectory makes sense.**
  Admission 2026-05-04; progress notes 05-06 (SOAP), 05-09 (DAP), 05-12 (DSAP)
  — sequential and distinct. CIWA-Ar moves 12 → 8 → 2 over the 8-day stay, a
  plausible decline on a chlordiazepoxide taper; no score regresses without
  explanation. BP improves (158/96 → 142/88 → 128/82) after lisinopril is
  restarted. LoC recommendation steps 3.7 → 2.5 as Dimensions 1–2 resolve.

- [x] **2. Cross-document consistency — the golden thread holds (except by design).**
  The BPS diagnoses (Alcohol Use Disorder, Sedative-Hypnotic Use Disorder, MDD)
  recur in the progress-note Assessment sections. The treatment plan's
  interventions are referenced in later notes — *except* the deliberate G2
  break (the day-5 DAP documents a peer-support group that the plan never
  lists). That single inconsistency is intentional and is exactly what the TJC
  audit must catch.

- [x] **3. Scale plausibility — item scores sum to documented totals.**
  PHQ-9 = 18, GAD-7 = 15, AUDIT-C = 11, CIWA-Ar = 12 each equal the sum of
  their item-level scores. Enforced by
  `tests/test_persona.py::test_scale_item_scores_sum_to_totals`.

- [x] **4. Six-dimension coverage — every ASAM dimension has evidence.**
  All six ASAM 4th-edition dimensions carry ≥1 evidence span; the BPS alone
  covers all six, and the progress notes add more. Enforced by
  `tests/test_ingest.py::test_full_pipeline_ingests_synthetic_chart` and
  `tests/test_read_api.py::test_get_patient_asam_evidence`.

- [x] **5. TJC gap audit — both satisfied and missing EPs are present.**
  The coverage matrix produces 8 Element-of-Performance rows: 5 `gap` (the
  intentional G1–G5) and 3 `satisfied`. A chart that was "too clean" would not
  demonstrate the audit endpoint; this one has the contrast. Enforced by
  `tests/test_ingest.py` and `tests/test_read_api.py::test_get_patient_tjc_coverage`.

- [~] **6. Clinician sanity check — NOT performed (documented limitation).**
  No licensed counselor / social worker / psychiatrist was available to read
  the chart for this take-home. Mitigation: the persona and every note were
  authored against named ASAM 4th-edition dimension references and TJC
  CTS / NPSG.15.01.01 / R3-25 standards as researched in `documents/phase_1.md`;
  the seven scales are embedded verbatim with their validated item structures.
  This is the one item that could not be fully closed and is flagged honestly.

- [x] **7. De-identification — no real PHI.**
  The persona is wholly fictional. Name, DOB, address (412 Maplewood Ave,
  Springfield, IL — invented), phone (`(555) 010-xxxx`, in the NANP
  reserved-for-fiction `555-01xx` range), and NPI are synthetic. No field maps
  to any real person known to the author.
