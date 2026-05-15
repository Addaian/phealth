# Phase 3 — Implementation Plan

**Companion to:** `documents/phase_3_PRD.md`
**Source playbook:** `documents/phase_3.md`
**Date:** 2026-05-15
**Total estimated effort:** ~26–30 focused hours

This plan sequences Phase 3 into 14 milestones (M0 through M13) with file-level
scope and acceptance gates. Each milestone is independently runnable — at the
end of each, `ruff check`, `ruff format --check`, `mypy`, and `pytest -q` should
be green. Per `CLAUDE.md`, a sanity check (`ruff` + `mypy` + `pytest` +
`docker compose up` + `/health` + a manual curl against the new endpoint) gates
the close of every milestone, and a final phase-gate sanity check re-reads the
PRD §8 acceptance criteria before declaring Phase 3 complete.

---

## 1. Architecture at a glance

```
┌────────────────────────────────────────────────────────────────────────────┐
│                Phase 1 substrate + Phase 2 read surface                    │
│                          (unchanged)                                       │
│                                                                            │
│  Patient · Encounter · ClinicalDocument(raw_text + sections + fhir_json)   │
│  ExtractedObservation (char-offset provenance)                             │
│  AsamEvidence · TjcCoverage · AuditEvent                                   │
│  /api/v1/patients/{id}/{chart,intake,timeline,observations,…}              │
│  /fhir/{Patient,DocumentReference,Observation,Provenance,…}                │
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │ reads only (no schema changes to existing tables)
                                     ▼
┌────────────────────────────────────────────────────────────────────────────┐
│                          Phase 3 additions                                 │
│                                                                            │
│  app/clinical/asam/         (deterministic core)                           │
│    rubric.py                — subdimension StrEnums, Min-LoC tables        │
│    risk_ratings.py          — ExtractedObservation + AsamEvidence → rating │
│    level_decision.py        — Ch.10 Determination Rules (pp 279–281)       │
│    narration.py             — Claude prompt + parser                       │
│    schemas.py               — Pydantic v2 response models                  │
│                                                                            │
│  app/clinical/tjc/                                                         │
│    ep_catalog.py            — 13-EP table (paraphrased)                    │
│    audit_functions.py       — one Python predicate per EP                  │
│    narration.py             — surveyor-RFI prompt + batch parser           │
│    schemas.py                                                              │
│                                                                            │
│  app/clinical/llm/                                                         │
│    claude_client.py         — singleton; retries; circuit-breaker; logs    │
│    prompts.py               — XML-tagged templates                         │
│    citation_validator.py    — re-check raw_text[start:end] == cited_text   │
│    structured_output.py     — native SO + tool-use fallback                │
│                                                                            │
│  app/clinical/shared/                                                      │
│    evidence_retrieval.py    — pulls evidence rows by patient               │
│    evidence_hash.py         — xxhash64 over canonical row-set              │
│    response_builder.py      — builds AsamAssessmentRead / TjcAuditRead     │
│                                                                            │
│  app/db/models.py           — append AsamAssessment, TjcAuditResult,       │
│                                LlmInvocation tables                        │
│                                                                            │
│  app/api/v1/                                                               │
│    asam_loc.py              — POST /asam-loc + GET /asam-assessments/{id}  │
│    tjc_audit.py             — POST /tjc-audit + GET /tjc-audits/{id}       │
│                                                                            │
│  app/api/fhir.py            — extend with ClinicalImpression and           │
│                                DetectedIssue search routes                 │
│                                                                            │
│  app/fhir/mappers.py        — to_clinical_impression(), to_detected_issue()│
└────────────────────────────────────┬───────────────────────────────────────┘
                                     │
                                     ▼   POST request
                          ┌──────────────────────┐
                          │ Rule engine (Python) │  ← deterministic; the level
                          └──────────┬───────────┘
                                     │ rules_fired + dimensional findings
                                     ▼
                          ┌──────────────────────┐
                          │ Claude (Sonnet 4.6)  │  ← narrates; cited
                          └──────────┬───────────┘   citations via Citations API
                                     │
                                     ▼
                       persist AsamAssessment / TjcAuditResult
                                     │
                                     ▼
                        return AsamAssessmentRead / TjcAuditRead
                              + ETag W/"{hash}-{model}"
```

Both endpoints share the same caching strategy and the same Claude client. The
rule engine is the determinism boundary; the LLM is the narration layer. No
ASAM/TJC rubric text is ever sent to Claude.

---

## 2. Milestones

### M0 — Anthropic SDK wiring + secrets + smoke test (1 h)

Plumb the Claude SDK and verify a "hello world" call against the live API
before any clinical code is written.

**Deliverables:**
- `pyproject.toml` — add `anthropic>=0.40` and `tenacity>=8` to dependencies.
- `app/core/config.py` — add `ANTHROPIC_API_KEY: SecretStr` and
  `PHEALTH_LLM_MODEL: str = "claude-sonnet-4-6"` settings.
- `.env.example` — append the new env vars with placeholder values.
- `scripts/smoke_anthropic.py` — one-shot script that sends a minimal Citations-API
  call ("Say hello in JSON") and prints the response + token counts. Manual-run
  only; not part of CI.
- `README.md` — short "Phase 3 prerequisites" section: how to obtain a key,
  expected cost ceiling, the `force_recompute` flag.

**Acceptance:** `python scripts/smoke_anthropic.py` returns a structured response
and prints non-zero token usage. `ruff check` clean. No live LLM in CI yet.

---

### M1 — Alembic migration: AsamAssessment, TjcAuditResult, LlmInvocation (1 h)

The three new tables, one migration.

**Deliverables:**
- `app/db/models.py` — append the three SQLModel classes per PRD §6.3, with
  module-top comments anchoring them to the PRD sections.
- `alembic/versions/phase3_001_clinical_assessments.py` — new revision creating
  the three tables and their unique constraints + JSONB columns.
- `tests/test_schema.py` — extended with row-creation tests for each new table
  (no FK violations, defaults populate correctly).

**Acceptance:** `alembic upgrade head` succeeds on a fresh DB; `alembic downgrade -1`
also succeeds. `pytest -q tests/test_schema.py` green. ruff + mypy clean.

---

### M2 — ASAM rubric encoding (2 h)

The subdimension enums, symbolic rating types, and the Risk Rating Form data
needed by the engine. Pure data; no logic.

**Deliverables:**
- `app/clinical/__init__.py`, `app/clinical/asam/__init__.py` — package layout.
- `app/clinical/asam/rubric.py`:
  - `class Dimension(IntEnum)` 1–6.
  - `class Subdimension(StrEnum)` listing every subdimension across all six dims.
  - `class RiskRating(StrEnum)` with `_0`, `ANY`, `EVAL`, `_2`, `_3A`, `_3B`,
    `_4` (and the COE-flagged variants where applicable; document the symbolic
    semantics in module docstrings).
  - `MIN_LOC_BY_RATING: dict[RiskRating, MinLoc]` mapping to symbolic
    `MinLoc.L1_5 | L1_7 | L2_1 | L2_5 | L2_7 | L3_1 | L3_5 | L3_7 | L3_7_BIO | L4`.
  - `COE_BEARING_RATINGS: set[RiskRating]` for the Dim 3 (and select Dim 1)
    ratings that escalate to COE.
- Module docstring cites *ASAM Criteria 4th ed., Chapter 10 pp. 212–278* by
  page range per the project's commented-code convention; explicitly notes that
  this file paraphrases the subdimension *labels* but does not copy
  rubric-anchor text.

**Acceptance:** `from app.clinical.asam.rubric import Subdimension, RiskRating,
MIN_LOC_BY_RATING, MinLoc` imports cleanly. ruff + mypy clean. No tests yet —
this is data only.

---

### M3 — ASAM risk-ratings computer (3 h)

The function that turns Phase 1's `ExtractedObservation` + `AsamEvidence` rows
into per-subdimension symbolic ratings.

**Deliverables:**
- `app/clinical/asam/risk_ratings.py`:
  - `compute_risk_ratings(patient_id, db) -> dict[Subdimension, RiskRating]`.
  - One private helper per subdimension (10 helpers), each documented with
    explicit clinical reasoning citing the relevant scale or evidence shape
    (CIWA-Ar ≥ 10 + concurrent benzo+alcohol → `_3A`; PHQ-9 ≥ 15 + C-SSRS Q2
    positive → `_1B`; etc.).
  - Reads only — never writes.
  - Default-on-missing strategy: when no evidence, return the conservative
    rating (`_0` for non-medical dims; the lowest meaningful rating for
    psychiatric/withdrawal).
- `tests/clinical/test_risk_ratings.py` — golden-fixture tests:
  - Marcus admission → Dim 1 Withdrawal = `_3A`, Dim 3 APS = `_1B`, etc.
  - Marcus day-8 → Dim 1 Withdrawal = `_0`/`ANY`, Dim 4 = `_C`.
  - Empty patient → all defaults, no exceptions.

**Acceptance:** the golden-fixture tests pass. ruff + mypy clean. The Phase 1
+ Phase 2 test suite (132 tests) remains green.

---

### M4 — ASAM Chapter-10 decision tree (2 h)

The pure-Python implementation of the Level-of-Care Determination Rules
(pp. 279–281).

**Deliverables:**
- `app/clinical/asam/level_decision.py`:
  - `class LevelDecision(BaseModel)`: `level: str`, `modifiers: list[str]`,
    `co_occurring_enhanced: bool`, `biomedical_enhanced: bool`,
    `rules_fired: list[str]`.
  - `decide(ratings: dict[Subdimension, RiskRating]) -> LevelDecision`.
  - Implementation as a single ~80-line `match`/`if-elif` block that mirrors
    the rule structure (Inpatient → Medically Managed → Clinically Managed
    Residential → Clinically Managed Outpatient → COE escalation → BIO
    escalation → Level-4 escalation on combined 3.7-BIO + COE).
  - Every rule that fires appends a human-readable string to `rules_fired`
    (these surface in the API response and the LLM prompt).
- `tests/clinical/test_level_decision.py` — exhaustive table-driven tests:
  - Marcus admission ratings → `level == "3.7"`, non-COE, non-BIO.
  - Marcus day-8 ratings → `level == "2.5"`.
  - 3B in Dim 1 → `level == "3.7-BIO"`.
  - 3B in Dim 1 + COE flag → `level == "4.0"`.
  - Edge cases: all-zero ratings → `level == "1.0"` or the lowest-level OP
    recommendation (document the chosen default in the rubric module).
  - One test per published rule (10+ cases).

**Acceptance:** all decision-tree tests pass. 100% line coverage on
`level_decision.py`. ruff + mypy clean.

---

### M5 — TJC EP catalog + audit functions (3 h)

The 13-EP catalog and one predicate per EP.

**Deliverables:**
- `app/clinical/tjc/__init__.py`.
- `app/clinical/tjc/ep_catalog.py`:
  - `EP_CATALOG: list[EpDefinition]` of 13 entries with `code`, `domain`,
    `paraphrased_requirement`, `evidence_signal` (what the predicate looks for).
  - Module-top docstring explicitly states the catalog is paraphrased from
    public R3 reports and FAQs; NOT a verbatim reproduction of CAMBHC.
- `app/clinical/tjc/audit_functions.py`:
  - One function per EP, all the same signature:
    `audit_<code>(patient_id, db) -> EpFinding`.
  - `EpFinding`: `ep_code`, `status` (`satisfied|gap|not_applicable|ambiguous`),
    `severity` (`high|moderate|low|none`), `evidence_pointers: list[Citation]`,
    `finding_template: str`, `negative_finding: bool`, `linked_planted_gap: str | None`.
  - The five planted gaps (G1–G5) wire to:
    - `audit_CTS_03_01_09` → G1 (no repeat PHQ-9).
    - `audit_CTS_03_01_03_EP28` → G2 (peer-support not on plan).
    - `audit_NPSG_15_01_01_EP3` → G3 (no C-SSRS reassessment).
    - `audit_CTS_04_02_TOC` → G4 (no 72-h discharge plan).
    - `audit_RC_01_02_01_EP3` → G5 (inconsistent co-signature timestamp).
  - For absence findings (G1, G3, G4): `negative_finding=True`, no citation
    span is invented; the LLM is prompted with a literal "absence of X"
    template.
- `app/clinical/tjc/runner.py`: `run_audit(patient_id, db) -> list[EpFinding]`
  — invokes all 13 predicates and returns the list.
- `tests/clinical/test_audit_functions.py` — per-EP golden tests on Marcus,
  asserting status and `linked_planted_gap` where applicable.

**Acceptance:** for Marcus, the 13 predicates return exactly 5 gap findings
with the correct `linked_planted_gap` mapping. At least 7 EPs return
`satisfied`. ruff + mypy clean.

---

### M6 — Claude client (retries, circuit-breaker, headers, LlmInvocation logging) (2 h)

The single entry point for any Claude API call from the application.

**Deliverables:**
- `app/clinical/llm/__init__.py`.
- `app/clinical/llm/claude_client.py`:
  - `class ClaudeClient`: wraps `anthropic.Anthropic(max_retries=3, timeout=60)`.
  - `messages_create_structured(...)` method that:
    - Issues the request.
    - Captures `anthropic-ratelimit-*` headers from the response.
    - On success, writes one `LlmInvocation` row (own DB session).
    - On final failure after SDK retries, raises a typed exception with the
      Anthropic request id.
  - Embedded inline `CircuitBreaker` (~30 lines): opens after 3 consecutive 429s
    in 60 s; closes after a 60 s cooldown; check at the top of every call.
  - Configurable model + temperature via the Settings object.
- `app/clinical/llm/prompts.py`: shared XML-tagged prompt fragments
  (`<system_role>`, `<task>`, `<output_format>`, the refusal clause).
- `tests/clinical/test_claude_client.py` — uses `respx` (or `httpx.MockTransport`
  via the Anthropic SDK's transport hook) to assert:
  - A 429 response triggers a retry.
  - 3 consecutive 429s open the breaker.
  - Successful calls write one `LlmInvocation` row with non-zero tokens.

**Acceptance:** the client's tests pass without ever making a live LLM call.
ruff + mypy clean. The Phase 2 test suite remains green.

---

### M7 — Citations API integration + structured output + citation validator (2 h)

The two Claude primitives the playbook calls non-negotiable.

**Deliverables:**
- `app/clinical/llm/structured_output.py`:
  - `def call_with_schema(client, messages, response_model: type[BaseModel],
    documents: list[dict]) -> response_model`.
  - Uses Anthropic's native Structured Outputs (`response_format={"type":
    "json_schema", "json_schema": {...}}`) on supported models.
  - Tool-use fallback path (one forced tool with `input_schema = model_json_schema()`)
    gated by a feature-flag in `Settings` (`PHEALTH_LLM_USE_STRUCTURED_OUTPUTS: bool = True`).
- `app/clinical/llm/citation_validator.py`:
  - `class CitationValidator`: `validate(claim, patient_id) -> ValidationResult`.
  - Re-checks `raw_text[char_start:char_end] == snippet` for every citation
    against `ClinicalDocument.raw_text` in the local DB.
  - Returns the `failures` list for downstream retry logic.
- `app/clinical/shared/evidence_retrieval.py`:
  - `build_evidence_documents(patient_id, db) -> list[dict]` returning the
    Citations-API document blocks per playbook §A4.
- `tests/clinical/test_citation_validator.py` — unit tests for each failure
  mode (unknown doc, out-of-range, snippet mismatch).
- `tests/clinical/test_structured_output.py` — assert that the SO call path
  packages messages correctly; assert the tool-use fallback path is structurally
  equivalent.

**Acceptance:** citation validator tests pass. structured-output path
correctly encodes the JSON schema in the request. ruff + mypy clean.

---

### M8 — ASAM narration prompt + parser (2 h)

The full prompt template + the response parser that produces an
`AsamAssessmentRead`.

**Deliverables:**
- `app/clinical/asam/schemas.py`:
  - `AsamRationale` (the LLM response model — overall_rationale, per-dimension
    rationale, confidence).
  - `AsamAssessmentRead` (the final API response model per PRD §6.1).
  - `DimensionRead`, `SubdimensionRead`, `Citation` sub-models.
- `app/clinical/asam/narration.py`:
  - `def narrate(rule_engine_output, ratings, evidence_documents, model_version)
    -> AsamRationale` using `ClaudeClient` + `call_with_schema`.
  - The full prompt template per PRD §6.6, built via the shared XML-tagged
    fragments in `app/clinical/llm/prompts.py`.
  - On citation-validation failure → one corrective retry; on second failure →
    return a degraded `AsamRationale` with `rationale_status="degraded"` +
    warnings.
- `app/clinical/shared/response_builder.py`:
  - `build_asam_response(decision, ratings, rationale, ...) -> AsamAssessmentRead`
    — assembles the final response from the rule engine + LLM outputs.
- `tests/clinical/test_asam_narration.py` — uses a mocked `ClaudeClient` that
  returns a canned `AsamRationale`; asserts:
  - Citation validator runs.
  - Degraded path triggers on validation failure.
  - The assembled `AsamAssessmentRead` matches the canonical shape.

**Acceptance:** mocked end-to-end produces a valid `AsamAssessmentRead` for
Marcus. ruff + mypy clean.

---

### M9 — TJC narration prompt + parser (batch) (1.5 h)

Same pattern as M8, but batches all 13 EPs into one Claude call.

**Deliverables:**
- `app/clinical/tjc/schemas.py`: `TjcFinding`, `TjcAuditResponse`,
  `TjcAuditRead`, `TjcSummary`.
- `app/clinical/tjc/narration.py`:
  - `def narrate(findings, evidence_documents, model_version) -> TjcAuditResponse`
    — single batch call.
  - Surveyor-RFI template per playbook §B3 and PRD §5.5.
  - Negative-finding rows are NOT cited; the template fills "Documentation
    review did not identify [requirement]…" without citations[].
- `app/clinical/shared/response_builder.py`: `build_tjc_response(...)`.
- `tests/clinical/test_tjc_narration.py` — mocked Claude:
  - All 13 EPs make it into the response.
  - Summary counts add up (`satisfied + gap + not_applicable + ambiguous == 13`).
  - The 5 planted gaps are linked correctly via `linked_planted_gap`.

**Acceptance:** mocked end-to-end produces a valid `TjcAuditRead` for Marcus
with G1–G5 surfaced. ruff + mypy clean.

---

### M10 — REST endpoints: POST `/asam-loc`, POST `/tjc-audit` + caching + GETs (2.5 h)

The actual HTTP surface, wired into the FastAPI app.

**Deliverables:**
- `app/clinical/shared/evidence_hash.py`:
  - `compute_evidence_hash(patient_id, db, kind: "asam"|"tjc") -> str` —
    xxhash64 over the sorted contributing-row PKs.
- `app/api/v1/asam_loc.py`:
  - `POST /api/v1/patients/{patient_id}/asam-loc`:
    - Compute `evidence_hash`.
    - Look up cached `AsamAssessment` by `(patient_id, evidence_hash, model_version)`;
      return 200 if found and `force_recompute` is false.
    - Else: `compute_risk_ratings` → `decide` → `narrate` → persist → return 201.
    - Headers: `Location: /api/v1/asam-assessments/{id}`, `ETag: W/"{hash}-{model}"`.
    - 422 on missing evidence; 503 on Claude unreachable (with rule-engine-only
      fallback when configured).
  - `GET /api/v1/asam-assessments/{id}` — bare read, ETag-aware, 304 on match.
- `app/api/v1/tjc_audit.py`: same pattern.
- `app/main.py`: include the new routers under the existing `/api/v1/` mount
  and the existing auth + audit dependencies.
- `app/api/errors.py` (carried over from Phase 2): no changes needed — the
  unified handler already covers the new endpoints.
- `tests/clinical/test_endpoints_asam.py`, `tests/clinical/test_endpoints_tjc.py`:
  - First POST → 201, with ETag.
  - Second POST → 200, `cached: true`, identical ETag.
  - GET with `If-None-Match` → 304.
  - 422 on a patient with no evidence.
  - 503 + rule-engine-only fallback on mocked Claude failure.

**Acceptance:** the live stack accepts the canonical curl flow:
1. `curl -X POST .../asam-loc` → 201 with Marcus = Level 3.7.
2. `curl -X POST .../asam-loc` again → 200 cached, same ETag.
3. `curl -H "If-None-Match: <etag>" .../asam-assessments/{id}` → 304.

ruff + mypy clean. 100% coverage on the rule-engine modules.

---

### M11 — FHIR mappings: ClinicalImpression + DetectedIssue (2 h)

The strict-FHIR surface for the new assessments.

**Deliverables:**
- `app/fhir/mappers.py`:
  - `to_clinical_impression(assessment: AsamAssessment, patient) -> ClinicalImpression`
    per PRD §5.10.
  - `to_detected_issue(finding: dict, audit: TjcAuditResult, patient) -> DetectedIssue`
    per PRD §5.10.
  - Custom CodeSystem URLs:
    `http://perspectiveshealth.ai/CodeSystem/asam-level` and `.../tjc-ep`.
  - The per-dimension rationale and cited spans ride on the same `PH_OFFSET_EXT`
    extension URL Phase 2 introduced for `Provenance`.
- `app/api/fhir.py`:
  - `GET /fhir/ClinicalImpression` (search by `subject`) + `/{id}`.
  - `GET /fhir/DetectedIssue` (search by `subject` + `category=tjc-audit`) + `/{id}`.
  - Use the existing `app/api/pagination.py` cursor encoder.
- `app/fhir/mappers.py::build_capability_statement` — extend with the two new
  resources + their search params.
- `tests/clinical/test_fhir_clinical_impression.py`,
  `tests/clinical/test_fhir_detected_issue.py`:
  - Every emitted resource round-trips through `fhir.resources.R4B.<Resource>.model_validate`.
  - The `ClinicalImpression.finding[]` carries the recommended level.
  - The `DetectedIssue` Bundle for Marcus contains exactly 5 `DetectedIssue`s
    (one per planted gap).
  - `CapabilityStatement` (`GET /fhir/metadata`) lists both new resources.

**Acceptance:** every new FHIR endpoint validates; the Phase 2 FHIR test suite
remains green. ruff + mypy clean.

---

### M12 — Tests: full Marcus golden fixtures + Claude-unreachable + citation-failure paths (2.5 h)

The acceptance suite for Phase 3 — every PRD §8 metric becomes a test.

**Deliverables:**
- `tests/clinical/test_marcus_admission_golden.py`:
  - End-to-end mocked: `POST /asam-loc` returns Level 3.7 with all expected
    dimensional ratings.
- `tests/clinical/test_marcus_day8_golden.py`:
  - With the day-8 fixture: Level 2.5 (this fixture may require simulating an
    additional progress note in the test DB; document the simulation clearly).
- `tests/clinical/test_marcus_tjc_golden.py`:
  - All 5 planted gaps surfaced via `linked_planted_gap`.
  - ≥ 7 EPs satisfied.
- `tests/clinical/test_provenance_phase3.py`:
  - Every citation in every response round-trips
    (`raw_text[char_start:char_end] == snippet`).
- `tests/clinical/test_failure_paths.py`:
  - Claude 5xx after retries → 200 with `rationale_status: "unavailable"`,
    `x-rule-engine-only: true` header.
  - Citation-validation failure → 200 with `rationale_warnings: ["citation_validation_failed"]`.
  - Structured-output schema violation → 502.
- `pytest.ini` (or `pyproject.toml`): add a `manual` marker for live-LLM tests
  so CI can `--m "not manual"` skip them.

**Acceptance:** all PRD §8 metrics pass as automated tests. Total test count
reaches ~160 (Phase 2's 132 + ~28 new). 90% coverage on `app/`, 100% on
`app/clinical/asam/{rubric,risk_ratings,level_decision}.py` and on
`app/clinical/tjc/audit_functions.py`.

---

### M13 — README + sample JSON + ADR + Phase 3 changelog (1.5 h)

The reviewer-facing artifacts.

**Deliverables:**
- `examples/marcus_reyes_asam_admission.json` — regenerated from the live
  endpoint (admission).
- `examples/marcus_reyes_asam_day8.json` — regenerated (step-down).
- `examples/marcus_reyes_tjc.json` — regenerated.
- Optionally: `examples/marcus_reyes_clinical_impression_bundle.json` and
  `..._detected_issue_bundle.json`.
- `README.md` — new top-level "Phase 3 — Clinical Decision Endpoints" section:
  - The "deterministic core + LLM narration" framing in three sentences.
  - A diagram showing rule engine → Claude → cached `AsamAssessment` →
    response.
  - The two endpoint contracts inline.
  - The "Why not LLM-only?" paragraph citing the ASAM IP notice.
  - The cost / latency table (per-call cost, demo-run cost, cached vs fresh
    latency).
  - The provenance round-trip line extended to Phase 3 endpoints.
- `docs/adr/003-llm-narration.md` — one page:
  - **Context** — Two requirements pull opposite directions: clinical CDS must
    be reproducible (FDA SaMD principle), and reviewers want a human-readable
    rationale.
  - **Decision** — Rule engine for the recommendation; LLM narration for the
    rationale; ASAM/TJC rubric text never enters the prompt.
  - **Consequences** — Deterministic level; LLM cost capped; ASAM IP respected;
    cache layer becomes the determinism boundary.
- `CHANGELOG.md` — append the M0–M13 lines per the project convention.

**Acceptance:** a reviewer reads the new README section + ADR in < 12 minutes.
All three sample JSONs are the current live responses (regenerate after the
final phase-gate sanity check).

---

## 3. Sequencing & dependencies

```
M0 (SDK + secrets + smoke test)
   │
   ▼
M1 (Alembic migration: AsamAssessment, TjcAuditResult, LlmInvocation)
   │
   ▼
M2 (ASAM rubric)
   │
   ▼
M3 (ASAM risk-ratings)  ──┐
                          │
                          ▼
                       M4 (ASAM decision tree)  ──┐
                                                  │
                       M5 (TJC EP catalog + audit functions)  ──┤
                                                                │
                                                                ▼
                                                M6 (Claude client)
                                                                │
                                                                ▼
                                                M7 (Citations + SO + validator)
                                                                │
                                              ┌─────────────────┴─────────────────┐
                                              ▼                                   ▼
                                  M8 (ASAM narration)                  M9 (TJC narration)
                                              │                                   │
                                              └─────────────────┬─────────────────┘
                                                                ▼
                                                M10 (REST endpoints + caching + GETs)
                                                                │
                                                                ▼
                                                M11 (FHIR mappings)
                                                                │
                                                                ▼
                                                M12 (golden tests + failure paths)
                                                                │
                                                                ▼
                                                M13 (README + samples + ADR + changelog)
```

**M0 must precede everything** (SDK wiring is a prerequisite). M2–M5 are the
deterministic-core work and can ship without any LLM dependency at all — the
Phase 3 endpoints could return rule-engine-only responses after M5 and the
recommendation would still be valid. M6–M9 layer the LLM. M10 is the **Task 3
acceptance milestone** — at the end of M10 the brief's required deliverable is
shippable; M11–M13 deepen FHIR conformance and polish the submission.

The deterministic core (M2–M5) is the bug-finding milestone for the demo: if
Marcus's expected ratings don't produce Level 3.7 from M3 + M4 alone, fix that
*before* introducing LLM variability.

## 4. Tech stack additions

```toml
# additions to pyproject.toml dependencies
anthropic = ">=0.40"          # Citations API + Structured Outputs (M0)
tenacity = ">=8"              # retry decorator for the Claude client (M6)
# xxhash already on the project from Phase 2 — used for evidence_hash in M10.

# dev / optional
respx = ">=0.20"              # mocked Anthropic transport for tests (M6, M7, M12)
```

No new infrastructure. Postgres + pgvector + alembic from Phase 1, the dual
API surface from Phase 2 — all unchanged. One new env var
(`ANTHROPIC_API_KEY`), one new optional env var (`PHEALTH_LLM_MODEL`,
default `claude-sonnet-4-6`).

## 5. Risks & branch points

| Trigger | Branch |
|---|---|
| Marcus's golden ratings (M3) don't produce Level 3.7 from M4 | The bug is in `risk_ratings.py`, not `level_decision.py`. Fix M3 before continuing; the engine is pure data. |
| `fhir.resources.R4B.ClinicalImpression.model_validate` rejects the emitted resource | Inspect the validator output; most likely cause is missing `status` or `subject`. Patch `to_clinical_impression`; do NOT bypass validation. |
| Anthropic returns malformed citations on `>50%` of validations | Reduce the evidence-document granularity per playbook §F (sentence-chunked rather than paragraph-chunked); re-run. |
| Structured-output schema rejected by the SDK because the response_model has features Anthropic SO doesn't support | Flip `PHEALTH_LLM_USE_STRUCTURED_OUTPUTS=false` to enable the tool-use fallback; document the limitation in the README. |
| Live Claude latency exceeds 60 s timeout on Marcus | Bump the timeout in `ClaudeClient` to 90 s; if it persists, the prompt is too large — trim evidence-document count, prioritize the highest-confidence rows. |
| Demo cost overruns ($10+) | Cache-on-hash means re-runs are free; if `force_recompute: true` is used heavily, document this explicitly and re-run with `false` for the final demo. |
| Test flakiness from accidental live LLM calls | The `manual` pytest marker exists; CI runs `pytest -m "not manual"`. Audit `test_*.py` files for any uses of the real `ClaudeClient` constructor — they should all go through a fixture that injects a mock. |
| A planted gap (G1–G5) does not appear in the Phase 1 `TjcCoverage` matrix | Phase 3 cannot synthesize what Phase 1 didn't extract. Open a Phase 1 carry-over fix in M5 *before* writing the affected audit function; document the carry-over in `CHANGELOG.md`. |
| `LlmInvocation` table fills up during testing and slows down later test runs | The test fixture truncates the table between tests; if not, add `db_session.execute(delete(LlmInvocation))` in `conftest.py`. |

## 6. What ships at end of Phase 3

Repo with:

- ✅ `POST /api/v1/patients/{id}/asam-loc` and `POST /api/v1/patients/{id}/tjc-audit` returning cited, structured assessments — deterministic level, LLM-narrated rationale.
- ✅ Marcus Reyes resolves to Level 3.7 on admission and steps down to 2.5 by day 8; the TJC audit surfaces all 5 planted gaps (G1–G5) plus ≥ 7 satisfied EPs.
- ✅ FHIR mappings: `/fhir/ClinicalImpression` + `/fhir/DetectedIssue` with the custom char-offset extension carried forward from Phase 2.
- ✅ `AsamAssessment`, `TjcAuditResult`, `LlmInvocation` tables with `evidence_hash` caching, ETag, and `If-None-Match` → 304.
- ✅ Anthropic Citations API + native Structured Outputs with a post-hoc DB-side citation validator; tool-use fallback feature-flagged.
- ✅ Tiered error handling: Claude 429 → 503 + Retry-After; Claude 5xx → 200 with rule-engine-only output; citation-validation failure → 200 + warning.
- ✅ ~28 new tests (Phase 2's 132 + ~28), 100% coverage on the rule-engine modules.
- ✅ `examples/marcus_reyes_asam_admission.json`, `..._asam_day8.json`, `..._tjc.json` regenerated from the live endpoints.
- ✅ README "Phase 3 — Clinical Decision Endpoints" section + "Why not LLM-only?" paragraph respecting ASAM IP.
- ✅ `docs/adr/003-llm-narration.md` — the one-page architectural decision record.
- ✅ `CHANGELOG.md` updated through M13.

**The assessment is now end-to-end:**
- Task 1 (Phase 1): a synthetic chart in SimplePractice + the FHIR-shaped substrate.
- Task 2 (Phase 2): the dual-surface read API with provenance baked in.
- Task 3 (Phase 3): the cited-rationale clinical endpoints for ASAM and TJC.
