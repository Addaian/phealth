# Phase 1 — PRD: Synthetic Chart + FHIR-Shaped Ingestion Substrate

**Project:** Perspectives Health Intern Technical Assessment
**Phase:** 1 of 3 (corresponds to Task 1 in the brief, plus the schema/ingestion substrate that Tasks 2 and 3 will consume)
**Status:** Approved
**Owner:** Adrian
**Date:** 2026-05-11
**Last reviewed:** 2026-05-13
**Reference:** `documents/phase_1.md` (research playbook), `documents/Technical task 5-8.pdf` (assessment brief)

---

## 1. Context

The assessment evaluates the ability to bridge a non-FHIR EMR (SimplePractice) with clinical-intelligence engines that compute ASAM 4th-edition Level of Care and Joint Commission CTS compliance findings. The brief explicitly warns: *"Don't try to just create a simple wrapper; you will quickly learn you can't do that."*

Research confirms why: **SimplePractice has no public chart-data API.** The only sanctioned programmatic path is the Data Export ZIP (CSV demographics + per-client PDF chart notes). Phase 1 must therefore (a) generate a realistic synthetic chart inside SimplePractice, (b) export it as the contract artifact for downstream ingestion, and (c) stand up a FHIR-R4-shaped storage and extraction substrate that makes Phases 2 and 3 retrieval problems rather than full-document inference problems.

## 2. Goals

1. Produce a **single-patient synthetic chart** in SimplePractice that contains a comprehensive BPS intake plus three progress notes in SOAP, DAP, and DSAP formats — with clinically valid scale results embedded verbatim and intentional, auditable TJC compliance gaps.
2. Export the chart via SimplePractice's Data Export and commit the ZIP as the Phase-2 input contract.
3. Stand up a **FastAPI + PostgreSQL** service with a FHIR-R4-shaped schema, a deterministic ingestion pipeline (ZIP → PDF → sectioned JSON + raw text), and pre-computed extraction scaffolds (scale observations, ASAM-evidence index, TJC-coverage matrix, embeddings) — all with **char-offset provenance**.
4. Expose enough HTTP surface area that Phase 2 (Data Extraction API) is a thin response-shaping layer over already-ingested rows, and Phase 3 (Clinical Intelligence Endpoints) is narration over retrieved evidence.

## 3. Non-Goals (Phase 1)

- No ASAM Level-of-Care prediction logic (Phase 3).
- No TJC compliance reasoning beyond status flags on a pre-defined EP list (Phase 3).
- No LLM-generated clinical reasoning at runtime (Phase 3). LLMs may be used at ingest *only* as a regex fallback for section detection.
- No UI. (`/docs` Swagger is sufficient.)
- No multi-tenant, auth-beyond-API-key, or production HIPAA controls.
- No scraping, browser automation, or use of the "SimplePractice Enterprise API" (scheduling-only).

## 4. Users / Personas

- **Primary:** the assessment reviewers (Kyle Hyun Woo Jung, CTO; Eshan Dosani, co-founder). They read the README, run `docker compose up`, hit `/docs`, and inspect a sample JSON response.
- **Secondary (informs design):** a clinical informaticist who would extend this stack to additional patients / EMR sources. The schema must read as something that scales beyond one synthetic patient.

## 5. Functional Requirements

### 5.1 Synthetic Clinical Chart (in SimplePractice)
- One patient (persona: Marcus J. Reyes, 34M, alcohol + benzodiazepine use disorder, see `app/synthetic/persona.yaml`).
- One **BPS intake** containing the sections enumerated in §6.1 of this PRD, with these scales embedded **verbatim with item-level scores and totals**: PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, C-SSRS. COWS included as a "not administered (no opioid use)" record for completeness.
- **Three progress notes** on three distinct dates:
  - Note 1 — SOAP format, MD author, individual session.
  - Note 2 — DAP format, peer specialist author, peer-support group.
  - Note 3 — DSAP format, LCSW author, individual + Level-of-Care reassessment.
- Note templates prefixed `[SOAP]` / `[DAP]` / `[DSAP]` in the title so the downstream parser has a reliable format dispatcher.
- **DSAP definition for this project:** `Data` (measurable / observable — vitals, scale scores, UDS, group attendance) / `Subjective` (client narrative) / `Assessment` (clinical interpretation) / `Plan` (next steps). The format is a minority convention and clinicians genuinely disagree on what "Data" means in DSAP — we pin this definition in the template so the parser, the reviewer, and the chart author all share one contract (see playbook §F).
- Persona-driven evidence across all six ASAM 4th-edition dimensions.
- Intentional, documented compliance gaps spanning all five EPs in §6.3: CTS.03.01.09 (measurement-based care), CTS.03.01.03 (treatment-plan / golden-thread alignment), NPSG.15.01.01 (suicide re-screening), R3-25 (transitions of care, no 72-h discharge plan), and RC.01.02.01 (documentation authentication / co-signature). See §6.3 for the full G1–G5 table.

### 5.2 Data Export Artifact
- A **SimplePractice "Complete" Data Export ZIP** committed under `data/synthetic_export/<date>.zip`.
- The ZIP must contain `Contacts/` (CSV demographics) and `Medical_Records/<Client>/` (PDF chart notes).
- README documents an explicit **deletion path** for the ZIP and clarifies that production integration would require a BAA with SimplePractice.

### 5.3 Ingestion Service
- HTTP endpoint `POST /ingest/simplepractice-zip` accepts the ZIP and returns `202 Accepted`; processing is backgrounded.
- For each PDF in the ZIP, the ingester must:
  1. Extract `raw_text` via `pdfplumber`.
  2. Classify document type (`bps_intake` | `soap` | `dap` | `dsap`) using regex on title/headers; LLM fallback only on regex miss.
  3. Split into format-specific sections (e.g., SOAP → `subjective`/`objective`/`assessment`/`plan`) with **char-offset spans** on each section.
  4. Extract every scale result (PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS, C-SSRS) as an `ExtractedObservation` row with LOINC codes where available and **char-offset provenance** to the source span.
  5. Tag substances, medications, and diagnoses → additional `ExtractedObservation` rows.
  6. Build per-document ASAM-evidence index (`AsamEvidence` rows, one per (dimension, span)).
  7. Update the patient-level TJC coverage matrix (`TjcCoverage` rows, one per EP, status `satisfied`/`gap`/`ambiguous`).
  8. Compute embeddings on each section (pgvector) for Phase 3 retrieval.
  9. Persist canonical FHIR R4 JSON for Patient, Encounter, ClinicalImpression, DocumentReference, and **Observation** (one per `ExtractedObservation` that carries a LOINC code — i.e., every scale extraction, not just headline totals) resources alongside relational rows.
- Every `ExtractedObservation` row records its `extraction_method` as one of `regex` | `llm` | `manual`. The flag is **per-field**, not per-document: regex is the default, and the value flips to `llm` only when regex misses on that specific field. This makes the regex-first / LLM-fallback policy auditable at the row level.
- Every state-changing operation appends an `AuditEvent`.

### 5.4 Read API (Phase-2 contract scaffolding)
The following endpoints must exist and return well-shaped JSON (Phase 2 will refine response payloads):
- `GET /patients/{id}` — demographics + FHIR Patient.
- `GET /patients/{id}/intake` — full BPS text + sectioned JSON.
- `GET /patients/{id}/timeline` — progress notes ordered by `authored_on`, each with `{date, author, type, sections, raw_text}`.
- `GET /patients/{id}/observations` — flat list of `ExtractedObservation` rows.
- `GET /patients/{id}/asam-evidence` — evidence spans grouped by dimension.
- `GET /patients/{id}/tjc-coverage` — EP-keyed status with evidence pointers.
- `GET /patients/{id}/fhir-bundle` — FHIR R4 transaction Bundle assembled via `fhir.resources`. For the seeded chart, the Bundle contains: 1 `Patient`, ≥1 `Encounter`, 4 `DocumentReference` (1 BPS + 3 progress notes), ≥3 `ClinicalImpression` (one per progress note), and N `Observation` resources — one for every `ExtractedObservation` carrying a LOINC code (all extracted scales, not only the headline totals).
- `GET /health` — liveness probe.

### 5.5 Provenance Guarantee
Every row in `ExtractedObservation`, `AsamEvidence`, and every `evidence_*` field in `TjcCoverage` must store `(document_id, char_start, char_end)`. A round-trip test re-locates each extracted value in the source `raw_text`.

## 6. Detailed Requirements

### 6.1 BPS Intake Required Sections
Demographics · Presenting Problem / HPI · Past Psychiatric History · Substance Use History (with AUDIT-C, DAST-10) · Medical History (with vitals + LFT/UDS labs) · Family / Social History · Mental Status Exam · Risk Assessment (C-SSRS, CIWA-Ar, COWS) · Diagnosis (DSM-5-TR / ICD-10) · Treatment Plan (measurable goals, interventions, frequency, expected outcomes).

### 6.2 Embedded Scales — Verbatim Format
Every scale must appear in the chart as item-level scores plus the computed total, e.g.:
> *"PHQ-9 administered 2026-05-04: anhedonia 3, depressed mood 3, sleep 3, fatigue 2, appetite 2, self-worth 2, concentration 2, psychomotor 1, suicidal ideation 0 — total 18 (moderately severe)."*

This converts free text into a regex-extractable structured signal and is what `app/ingest/scale_extractor.py` consumes.

### 6.3 Intentional Compliance Gaps (must be present in the chart)
| ID | EP | Gap Embedded |
|---|---|---|
| G1 | CTS.03.01.09 | PHQ-9 collected at intake, never repeated in any progress note |
| G2 | CTS.03.01.03 | Treatment plan lists CBT + MI; Note 2 documents peer-support session not on the plan |
| G3 | NPSG.15.01.01 | C-SSRS at intake; Note 3 plan states "re-administer C-SSRS prior to transfer" but no result documented |
| G4 | R3-25 (transitions) | No discharge planning note within 72 h of admission |
| G5 | RC.01.02.01 | Co-signature timestamp inconsistent on one note |

## 7. Non-Functional Requirements

- **Stack:** Python 3.11+, FastAPI, SQLModel/SQLAlchemy 2.x, Alembic, PostgreSQL 16 + pgvector, `fhir.resources`, `pdfplumber`, pytest.
- **Container:** `docker compose up` brings up Postgres + app; `pytest` is green on an empty schema and on the seeded synthetic chart.
- **Determinism:** Section detection is regex-first; LLM use is bounded by `MAX_LLM_CALLS_PER_INGEST`.
- **Embedding model:** `text-embedding-3-small` (OpenAI) is the default, configured via `EMBEDDING_MODEL`. The pipeline must read this env var rather than hard-code the model so an open-source embedder can be swapped in without code change (e.g., for a HIPAA-sensitive deployment that can't egress to OpenAI).
- **Idempotency:** Re-ingesting the same ZIP does not duplicate rows (keyed on `external_id` + content hash).
- **Secrets:** `.env.example` checked in with named-but-unvalued keys; `.env` gitignored. `pydantic-settings` fails fast on missing required values.
- **Audit trail:** Every ingest and extract action appends an `AuditEvent`.
- **Performance budget (Phase 1):** ingestion of the one-patient synthetic chart completes in <60s on a developer laptop with no GPU.

## 8. Success Metrics

| # | Metric | Target |
|---|---|---|
| M1 | All 7 embedded scales extracted from the chart with non-null char-offsets | 100% |
| M2 | All 3 progress-note formats parse into format-specific sections with non-empty bodies | 100% |
| M3 | Every ASAM dimension (1–6) has ≥1 evidence span across the chart | 6/6 |
| M4 | Every embedded compliance gap (G1–G5) is flagged with status `gap` in `TjcCoverage` | 5/5 |
| M5 | FHIR Bundle endpoint output validates against the FHIR R4 schema | passes |
| M6 | Provenance round-trip test passes — every extracted value re-locates in source `raw_text` | 100% |
| M7 | README reads end-to-end in <10 minutes and explains the "why not just wrap" answer | qualitative |

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| SimplePractice Starter blocks note-template editing | Med | High | Upgrade to Essential within free trial (free) |
| PDF text-layer extraction brittle on SimplePractice exports | Med | Med | `pdfplumber` positional region extraction; copy-paste-to-`.txt` fallback documented |
| LLM cost / nondeterminism in ingest | Med | Low | Regex-first, LLM only on miss, hard cap via env var |
| SimplePractice trial inaccessible at demo time | Low | High | Plan B: OpenEMR in Docker with equivalent templates; documented |
| Scope creep into Phase 2/3 reasoning | High | Med | This PRD's §3 enumerates non-goals; reviewer sees evidence rows, not LoC predictions |
| Inadvertent real PHI in persona | Low | High | Face-validity checklist (§Step 8 in playbook) includes de-id check before commit |

## 10. Open Questions

*All Phase 1 draft-stage questions resolved 2026-05-13 during PRD review:*

- **Q1 (resolved):** FHIR Bundle includes an `Observation` resource for every `ExtractedObservation` that carries a LOINC code — i.e., every extracted scale, not only headline totals. Codified in §5.3 step 9 and §5.4 `fhir-bundle`.
- **Q2 (resolved):** `extraction_method` is per-field, defaults to `regex`, flips to `llm` only on regex miss. Codified in §5.3 (final bullet).
- **Q3 (resolved):** Embedding model is `text-embedding-3-small` by default, behind the `EMBEDDING_MODEL` env var. Codified in §7.

No questions remain open for Phase 1. New questions surfaced during implementation should be tracked in `documents/` as Phase-2 / Phase-3 PRD inputs, not retro-added here.

## 11. Out of Scope (deferred to Phase 2 / Phase 3)

- Phase 2: response-shape refinement and the consolidated `extract` endpoint (single JSON object per the brief's Task 2).
- Phase 3: `POST /patients/{id}/asam-loc` and `POST /patients/{id}/tjc-audit` — both narrate over Phase-1 evidence tables with LLM-driven rationale, then return cited findings.
- Multi-patient ingestion, longitudinal reassessment workflows beyond a single export.
- Real-time webhook ingestion from SimplePractice (not supported by the platform).

---

## 12. Deliverables & Submission

The assessment brief specifies the submission contract; Phase 1 is responsible for producing the artifacts and Phase 3 (final submission) is responsible for sending them.

**Artifacts produced by end of Phase 1:**
- Public (or invite-shared) Git repository URL containing the FastAPI + Postgres service, ingestion pipeline, tests, and README per §5–§7.
- `examples/marcus_reyes.json` — a sample JSON response payload (the Phase-2 `/extract`-shaped envelope, or the Phase-1 `/patients/{id}/timeline` payload if Phase 2 hasn't finalized the shape yet) for the simulated patient.
- The SimplePractice Data Export ZIP committed under `data/synthetic_export/<date>.zip` (per §5.2).

**Submission recipients (per brief, page 3):**
- `kyle@perspectiveshealth.ai` (Kyle Hyun Woo Jung, CTO)
- `eshan@perspectiveshealth.ai` (Eshan Dosani, co-founder)

Both addresses must be on the same submission email. The repository link and the sample JSON are both required; sending only one is non-compliant with the brief.

---

## Appendix A — Glossary

- **BPS** — Biopsychosocial intake assessment.
- **ASAM** — American Society of Addiction Medicine; the Criteria define six dimensions and Levels of Care 0.5–4.0.
- **TJC / CTS** — The Joint Commission; CTS = Care, Treatment, and Services chapter of the Behavioral Health Care manual.
- **EP** — Element of Performance (a specific auditable requirement under a TJC standard).
- **Golden thread** — the documentation principle that assessed needs → treatment-plan goals → progress-note interventions form a traceable chain.
- **FHIR R4** — Fast Healthcare Interoperability Resources, release 4; the JSON resource standard used here as the canonical export shape.
