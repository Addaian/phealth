# Phase 3 — PRD: Cited-Rationale Clinical Decision Endpoints

**Project:** Perspectives Health Intern Technical Assessment
**Phase:** 3 of 3 (corresponds to Task 3 in the brief, layered on the Phase 1 substrate + Phase 2 read surface)
**Status:** Approved
**Owner:** Adrian
**Date:** 2026-05-15
**Last reviewed:** 2026-05-15
**Reference:** `documents/phase_3.md` (research playbook), `documents/phase_2_PRD.md` (read surface this builds on), `documents/phase_1_PRD.md` (the substrate), `documents/Technical task 5-8.pdf` (assessment brief)

---

## 1. Context

Phase 1 shipped the FHIR-R4-shaped ingestion substrate: a `ClinicalDocument` table with `raw_text` + sectioned JSONB, `ExtractedObservation` rows carrying char-offset provenance, pre-computed `AsamEvidence` per ASAM dimension, and a `TjcCoverage` matrix flagging all five planted compliance gaps (G1–G5). Phase 2 surfaced that substrate over a dual API: `/api/v1/*` (ergonomic) and `/fhir/*` (strict R4) with audit-on-read, ETag, RFC 7807 / OperationOutcome errors, and provenance round-trip baked into every response.

The brief's Task 3 asks for *two reasoning endpoints*: an ASAM Level-of-Care prediction and a TJC compliance audit. Both must produce **cited, defensible clinical rationale** — not opaque LLM output.

**Phase 3's thesis:** the *recommendation* is deterministic; the *rationale* is LLM-narrated. The ASAM 4th-edition Chapter 10 Determination Rules (pp. 279–281) are encoded as a pure-Python decision tree over symbolic risk ratings; the TJC audit is a battery of Python predicates over `TjcCoverage` + `ExtractedObservation`. Claude Sonnet 4.6 narrates the already-computed decision via Anthropic's Citations API + Native Structured Outputs (both GA as of Feb 2026), with the LLM seeing only the evidence the system retrieved — never proprietary ASAM or TJC rubric text. The result is auditable, reproducible, and respects ASAM's explicit prohibition on inputting their IP into AI systems.

This is a **reasoning phase**, not a parsing phase. No re-extraction; both endpoints consume Phase 1 rows as inputs.

## 2. Goals

1. Ship **`POST /api/v1/patients/{id}/asam-loc`** — ASAM 4th-edition Level-of-Care recommendation with per-dimension rationale, citations into source spans, and a deterministic rule-engine trace.
2. Ship **`POST /api/v1/patients/{id}/tjc-audit`** — Joint Commission compliance audit over a 13-EP behavioral-health catalog, surfacing the five planted gaps + 7–8 satisfied EPs with surveyor-grade RFI-style narration.
3. Achieve the demo-defining clinical outcomes: Marcus Reyes deterministically resolves to **Level 3.7** on admission and **Level 2.5** by day 8; the TJC endpoint surfaces all five planted gaps (G1–G5) with citations and rule traces.
4. **Cache aggressively** — every assessment persists to a new table keyed by `(patient_id, evidence_hash)`; repeat POSTs return the cached row with the same ETag; `If-None-Match` short-circuits to 304. Determinism survives Claude's run-to-run jitter.
5. Expose FHIR mappings: **`/fhir/ClinicalImpression`** for ASAM assessments and **`/fhir/DetectedIssue`** per TJC gap, both consistent with the Phase 2 Bundle conventions.
6. Ship the Phase 3 sample JSON artifacts (`examples/marcus_reyes_asam_admission.json`, `..._asam_day8.json`, `..._tjc.json`) and a README addendum framing the "deterministic core + LLM narration" architecture.

## 3. Non-Goals (Phase 3)

- **No LLM-driven recommendations.** The LoC and EP-status calls are made by Python rule engines; the LLM only narrates. Reviewers who try to extract a different level by prompting Claude differently will get the same level back.
- **No ingestion of proprietary ASAM or TJC text** into prompts. The LLM sees the engine's outputs (dimensional ratings, rules fired, satisfied/gap status) plus the project's own evidence spans — never copyrighted rubric text.
- **No async job queue.** Both endpoints are synchronous POSTs returning 201 (newly computed) or 200 (cached). Claude latency on the seeded chart is 8–12 s; documented in the README.
- **No multi-model ensembling** or self-critique loops. One Claude call per endpoint (TJC batches all 13 EPs).
- **No PDF / report generation.** JSON only; the surveyor-style narration is structured text, not a Word document.
- **No SMART on FHIR / OAuth flow.** `X-API-Key` continues from Phase 2.
- **No retraining of any kind.** No fine-tunes, no embeddings used at request time (Phase 1 embeddings remain dormant — retrieval is by structured query, not vector similarity, for this MVP).
- **No new ingestion path.** If a chart needs re-ingestion to fix evidence, that's a Phase 1 concern; Phase 3 reads what's there.

## 4. Users / Personas

- **Primary:** the assessment reviewers (Kyle Hyun Woo Jung, CTO; Eshan Dosani, co-founder). They will (a) read `examples/marcus_reyes_asam_admission.json` and `..._tjc.json`, (b) hit `/docs`, (c) run `curl -X POST` against both endpoints, (d) re-run to verify the cached 200 + same ETag, (e) GET `/fhir/ClinicalImpression?subject=...` to see the FHIR mapping, (f) read the README "Why not LLM-only?" paragraph, (g) skim `docs/adr/003-llm-narration.md`.
- **Secondary (informs design):** a utilization-review clinician who would extend this stack to additional patients. They need to (a) trust that the recommendation is reproducible — no LLM jitter on the level itself, (b) audit a finding by clicking through to the cited source span, (c) re-run after a chart amendment and see the new findings surfaced because the `evidence_hash` changed.
- **Secondary (informs design):** a TJC surveyor. They need RFI-style language they recognize ("Standard X, EP n: not met. Documentation review revealed..."), and they need every finding tied to a specific document.

## 5. Functional Requirements

### 5.1 Two new endpoints

| Method | Path | Purpose |
|--------|------|---------|
| **POST** | `/api/v1/patients/{id}/asam-loc` | Compute (or return cached) ASAM Level-of-Care assessment for the patient. |
| GET | `/api/v1/asam-assessments/{id}` | Read a prior assessment by id. |
| **POST** | `/api/v1/patients/{id}/tjc-audit` | Compute (or return cached) TJC compliance audit. |
| GET | `/api/v1/tjc-audits/{id}` | Read a prior audit by id. |
| GET | `/fhir/ClinicalImpression` | FHIR Bundle of `ClinicalImpression` resources (search by `subject`). |
| GET | `/fhir/ClinicalImpression/{id}` | Bare `ClinicalImpression` resource. |
| GET | `/fhir/DetectedIssue` | FHIR Bundle of `DetectedIssue` resources (search by `subject` + `category=tjc-audit`). |
| GET | `/fhir/DetectedIssue/{id}` | Bare `DetectedIssue` resource. |

All `/api/v1/*` and `/fhir/*` routes continue to require `X-API-Key`, write `AuditEvent(action="read")` (or `"compute"` for the POSTs — see §5.6) via BackgroundTasks, attach an ETag, and emit RFC 7807 / OperationOutcome on errors per Phase 2 conventions.

### 5.2 ASAM endpoint contract

```
POST /api/v1/patients/{patient_id}/asam-loc
  Headers:
    X-API-Key:          required
    Idempotency-Key:    optional (free-form client-supplied)
  Body:
    { "force_recompute": false }    # default false; true bypasses the cache
  Returns:
    201 Created    — newly computed assessment
                     Location: /api/v1/asam-assessments/{id}
                     ETag:     W/"{evidence_hash}-{model_version}"
                     Body:     AsamAssessmentRead (§6.1)
    200 OK         — cache hit (same patient + evidence_hash); body and ETag identical
                     to the 201 that originally created the row
    304 Not Modified — If-None-Match matches the current ETag (no body)
    422 Unprocessable Entity — patient has no AsamEvidence rows (cannot compute)
                                RFC 7807 with type=/errors/no-asam-evidence
    503 Service Unavailable  — Claude unreachable AND no rule-engine fallback was
                                requested via force_rule_engine_only
                                RFC 7807 with Retry-After header
    200 OK (with degraded body) — Claude unreachable, rule-engine output returned
                                   with x-rationale-status: unavailable header
```

The endpoint is **idempotent by evidence hash** (§5.7): identical inputs produce identical outputs (including byte-identical rationale text on a cache hit). The cache is per-patient, per-evidence-set, per-model-version.

### 5.3 TJC endpoint contract

```
POST /api/v1/patients/{patient_id}/tjc-audit
  Same headers, body shape, error envelopes, and caching semantics as §5.2.
  Returns:
    201 Created   — newly computed audit
                    Body: TjcAuditRead (§6.2)
                    Headers: Location, ETag identical pattern to §5.2.
    200 OK        — cache hit.
    304 Not Modified — If-None-Match matches.
    422           — no eligible TjcCoverage rows.
    503 / 200-degraded — Claude unreachable.
```

### 5.4 Deterministic core — ASAM rule engine

The rule engine (`app/clinical/asam/level_decision.py`) implements the ASAM 4th-edition Chapter 10 Determination Rules (pp. 279–281) as pure Python. Inputs: per-subdimension symbolic risk ratings. Outputs: `(recommended_level, modifiers, rules_fired)`.

**Subdimension enumeration** (encoded as `StrEnum`s in `app/clinical/asam/rubric.py`):

| Dim | Name | Subdimensions |
|---|---|---|
| 1 | Intoxication, Withdrawal, Addiction Meds | Intoxication; Withdrawal; Addiction Medication Needs |
| 2 | Biomedical Conditions | Physical Health Concerns; Pregnancy-related Concerns |
| 3 | Psychiatric and Cognitive Conditions | Active Psychiatric Symptoms; Persistent Disability |
| 4 | Substance Use-related Risks | Likelihood of Risky Substance Use; Likelihood of Risky SUD-related Behaviors |
| 5 | Recovery Environment Interactions | Ability to Function Effectively; Support in Current Environment; Safety in Current Environment |
| 6 | Person-Centered Considerations | (no ratings — informs LoC *selection*, not *recommendation*) |

**Risk-rating anchors are symbolic, not numeric.** Dim 1 Withdrawal uses `{0, ANY, EVAL, 2, 3A, 3B, 4}` (where `3A = Min 3.7`, `3B = Min 3.7-BIO`). The engine never collapses these to integers.

**Determination logic** (highest-rating-wins across subdimensions, with COE/BIO escalation):

1. If any subdim requires Level 4 → recommend **Level 4** (medically managed inpatient).
2. Else if any subdim is medically managed (3A/3B/etc.):
   - 3B in Dim 1 or Dim 2 → **3.7-BIO**.
   - Any subdim Min Level 3 → **3.7**.
   - Else if any Min Level 2 → **2.7**.
   - Else → **1.7**.
3. Else if any subdim requires clinically managed residential:
   - Any Min 3.5 → **3.5**.
   - Else → **3.1**.
4. Else clinically managed outpatient: max(2.5, 2.1, 1.5) by intensity.
5. **COE escalation**: any subdim flagged COE → recommendation carries a COE suffix; a 3.1 upgraded by COE becomes `3.5-COE`; a 2.1 becomes `2.5-COE`.
6. Combined `3.7-BIO + any COE` → **escalate to Level 4** per the rules.

`rules_fired: list[str]` records each rule that contributed to the decision (e.g., `"Medically managed care required (Dim 1 = 3A → Min Level 3.7)"`).

**Risk-rating computation** (`app/clinical/asam/risk_ratings.py`): one function per subdimension that consumes the relevant `ExtractedObservation` and `AsamEvidence` rows and returns the symbolic rating. Reasoning is explicit and documented in code (CIWA-Ar ≥ 10 + concurrent benzo + alcohol dependence → Dim 1 Withdrawal = 3A; PHQ-9 ≥ 15 + C-SSRS Q2-positive → Dim 3 Active Psychiatric Symptoms = 1B, non-COE; etc.).

### 5.5 Deterministic core — TJC audit functions

13 chart-auditable EPs (paraphrased from public R3 reports; never quoted verbatim from CAMBHC):

| EP code | Domain |
|---|---|
| CTS.02.03.07 EP 1 | SUD history collection |
| CTS.02.03.07 EP 7 | Withdrawal/intoxication risk assessment |
| CTS.02.01.07 EP 1 | H&P within 24h of admission |
| CTS.03.01.03 EP 28 | Individualized treatment plan at admission |
| CTS.03.01.09 | Measurement-based care (repeat instrument administration) |
| CTS.04.02.33 | MOUD offered for OUD |
| CTS.04.02 (TOC) | Care transition / discharge plan |
| NPSG.15.01.01 EP 2 | Suicide risk screening with validated tool |
| NPSG.15.01.01 EP 3 | Suicide risk assessment + reassessment |
| NPSG.15.01.01 EP 4 | Documented risk + mitigation plan |
| RC.01.01.01 EP 7 | Entries authenticated, dated, timed |
| RC.01.02.01 EP 3 | Co-signatures for delegated H&P |
| RC.01.03.01 | Timeliness of record completion |

One Python predicate per EP in `app/clinical/tjc/audit_functions.py`, returning `EPFinding(ep_code, status, evidence_pointers, finding_template, negative_finding)`. The Phase 1 `TjcCoverage` matrix already encodes the satisfied / gap / ambiguous status for each EP; audit functions add the surveyor-grade detail and the absence-of-evidence flag for negative findings.

The five planted gaps map deterministically:
- **G1** → CTS.03.01.09 (no repeat PHQ-9 across 8-day admission).
- **G2** → CTS.03.01.03 EP 28 (peer-support not on treatment plan).
- **G3** → NPSG.15.01.01 EP 3 (no C-SSRS reassessment).
- **G4** → CTS.04.02 TOC (no 72-h discharge plan).
- **G5** → RC.01.02.01 EP 3 (inconsistent co-signature timestamp).

### 5.6 LLM narration via Anthropic Citations API + Structured Outputs

**Model:** `claude-sonnet-4-6` by default; configurable via env var `PHEALTH_LLM_MODEL` (Opus 4.7 as documented fallback for narration-quality issues). The chosen model is logged on every `AsamAssessment` / `TjcAuditResult` row (`model_version` column).

**Per call**, each piece of evidence is supplied as a Citations-API document block:

```python
{"type": "document",
 "source": {"type": "text", "media_type": "text/plain", "data": snippet},
 "title": f"doc:{document_id}#chars:{char_start}-{char_end}",
 "context": json.dumps({"doc_id": ..., "evidence_row_id": ..., "char_start": ..., "char_end": ...}),
 "citations": {"enabled": True}}
```

Claude's response carries `citations[]` arrays on each content block. The application **re-validates** every citation against the local DB (`raw_text[char_start:char_end] == cited_text`) before persisting — the Citations API guarantees the API-boundary contract, but the canonical truth lives in our DB.

**Structured Outputs** (GA Feb 4 2026 on Sonnet 4.5/Opus 4.5/Haiku 4.5; presumed GA on the 4.6/4.7 generation by submission): the response is schema-constrained to the `AsamRationaleResponse` / `TjcAuditResponse` Pydantic models. If a deployment pins an older model where SO is unsupported, fall back to forced tool-use with the same Pydantic schema (`.model_json_schema()`).

**Prompt engineering** follows Anthropic's four non-negotiables: XML-tagged sections (`<dimensional_findings>`, `<evidence>`, `<rule_engine_decision>`, `<task>`, `<output_format>`); documents-first, question-last ordering; assistant-turn pre-fill (`{`); explicit refusal clause for un-evidenced claims (output `EVIDENCE_INSUFFICIENT` for that field).

**Critical constraint:** the LLM is told *the recommendation has already been made*. It must not invent findings or alter the level. The full prompt template is in §6.6.

### 5.7 Caching & determinism

Claude is not bit-stable at `temperature=0`. Every assessment persists to a cache table keyed by `(patient_id, evidence_hash)`.

**`evidence_hash`** = `xxhash.xxh64(canonical_evidence_set)`, where `canonical_evidence_set` is the sorted list of contributing `AsamEvidence` row PKs + contributing `ExtractedObservation` ids (for ASAM) or `TjcCoverage` row PKs + relevant `ExtractedObservation` ids (for TJC). The hash is deterministic and changes only when the underlying evidence does — re-ingestion of an amended chart naturally invalidates the cache.

**POST behavior:**
1. Compute `evidence_hash` from current rows.
2. If `(patient_id, evidence_hash, model_version)` row exists in `AsamAssessment` / `TjcAuditResult` and `force_recompute` is false → return cached row with **200 OK** and the original ETag.
3. Else compute (rule engine + LLM narration), persist, return **201 Created**.

**ETag:** `W/"{evidence_hash}-{model_version}"`. Subsequent requests with `If-None-Match: <etag>` return **304 Not Modified** with no body, exactly like Phase 2 reads.

`force_recompute: true` in the request body bypasses the cache and creates a new row — useful for testing and for forcing re-narration after a model swap.

### 5.8 Audit & observability

Every POST writes an `AuditEvent(action="compute", resource_type="asam_assessment"|"tjc_audit", payload={evidence_hash, model_version, cached: bool, latency_ms})`. Cache hits are audited as `action="read"` (consistent with Phase 2 GET semantics).

Every LLM call writes one `LlmInvocation` row capturing `model`, `prompt_tokens`, `completion_tokens`, `cache_read_tokens` (Anthropic prompt-cache reads), `latency_ms`, `response_hash`, and `citation_validation_passed`. This is the cost / latency / reliability ground truth for the demo recording.

### 5.9 Error handling tiers

| Failure mode | Behavior |
|---|---|
| **Patient has no AsamEvidence / TjcCoverage rows** | 422 + RFC 7807 (`type: /errors/no-asam-evidence`). |
| **Claude 429** | SDK retries (3×, exp backoff); on final failure → 503 + `Retry-After` header + RFC 7807. |
| **Claude 5xx** | SDK retries; on final fail → **200 with rule-engine-only output**, headers `x-rationale-status: unavailable`, `x-rule-engine-only: true`. Recommendation is still valid because it's deterministic. |
| **Citation validation failure** (raw_text[start:end] ≠ cited_text) | Retry once with a corrective prompt; on second failure → rule-engine-only output with `rationale_warnings: ["citation_validation_failed"]` in the body. |
| **Structured-output schema violation** | One automatic retry; on second failure → 502 + RFC 7807 (`type: /errors/llm-malformed`). |
| **Circuit breaker open** (3+ consecutive 429s in 60 s) | 503 immediately, do not even attempt the LLM call. |

### 5.10 FHIR mappings

**ASAM** → `ClinicalImpression` per HL7 R4B (definition: "A record of a clinical assessment performed to determine what problem(s) may affect the patient and before planning the treatments or management strategies").

- `ClinicalImpression.status = "completed"`.
- `ClinicalImpression.subject` → `Patient/{id}`.
- `ClinicalImpression.effectiveDateTime` → `AsamAssessment.computed_at`.
- `ClinicalImpression.summary` → the narration (overall rationale).
- `ClinicalImpression.finding[].itemCodeableConcept` → the recommended level (custom CodeSystem `http://perspectiveshealth.ai/CodeSystem/asam-level`, codes `1.0 | 1.5 | 1.7 | 2.1 | 2.5 | 2.7 | 3.1 | 3.5 | 3.7 | 3.7-BIO | 4.0 | 4.0-PSY` with optional `-COE` suffix).
- `ClinicalImpression.investigation[].item[]` → references to the contributing `Observation` resources (Phase 1's LOINC-coded scale extractions).
- `ClinicalImpression.extension` → custom extension carrying the per-dimension rationale + cited spans, using the same `PH_OFFSET_EXT` URL pattern Phase 2 introduced for `Provenance`.

**TJC** → one `DetectedIssue` per gap finding + an overall `MeasureReport` (optional, see §6.4):

- `DetectedIssue.status = "final"`.
- `DetectedIssue.code` → `{system: "http://perspectiveshealth.ai/CodeSystem/tjc-ep", code: "<EP code>"}`.
- `DetectedIssue.severity` → `high` for hard gaps, `moderate` for ambiguous.
- `DetectedIssue.patient` → `Patient/{id}`.
- `DetectedIssue.identifiedDateTime` → `TjcAuditResult.computed_at`.
- `DetectedIssue.detail` → the surveyor-narrated finding text.
- `DetectedIssue.evidence[].detail[]` → references to the cited documents / observations.
- `category=tjc-audit` query param distinguishes Phase 3 findings from any other future use of `DetectedIssue`.

### 5.11 Idempotency-Key header (optional, scope-controlled)

Clients may supply an `Idempotency-Key: <opaque-string>` header. If present and not the first time seen, the previous response is replayed verbatim (status, headers, body). Phase 3 implements the simplest form: hash the key into the cache lookup so that two POSTs with the same key always resolve to the same row, regardless of `evidence_hash` collisions. Documented in §5.7; not required for the demo.

### 5.12 Carry-over and contract decisions

These confirm Phase 2 conventions apply unchanged to Phase 3:
- Auth: `X-API-Key` on every endpoint (no SMART OAuth).
- Errors: RFC 7807 on `/api/v1/*`, OperationOutcome on `/fhir/*`.
- ETag / If-None-Match → 304.
- Audit-on-read writes the same `AuditEvent` row pattern; POSTs additionally write `action="compute"`.
- Timestamps with `America/Chicago` offset on response boundaries.

## 6. Detailed Requirements

### 6.1 ASAM assessment response shape (`AsamAssessmentRead`)

```json
{
  "id": "<uuid>",
  "patient_id": "<uuid>",
  "computed_at": "2026-05-15T14:22:08-05:00",
  "evidence_hash": "8f3a2c1b9e7d4a06",
  "model_version": "claude-sonnet-4-6",
  "cached": false,
  "recommendation": {
    "level": "3.7",
    "level_display": "Medically Monitored Intensive Inpatient",
    "modifiers": [],
    "co_occurring_enhanced": false,
    "biomedical_enhanced": false
  },
  "dimensions": [
    {
      "dimension": 1,
      "name": "Intoxication, Withdrawal, Addiction Medications",
      "subdimensions": [
        {"name": "Withdrawal and Associated Risks", "rating": "3A", "min_level": "3.7",
         "rationale": "CIWA-Ar 12 plus concurrent alcohol and benzodiazepine dependence; clinical monitoring required after-hours.",
         "citations": [{"document_id": "...", "char_start": 12397, "char_end": 12587, "snippet": "CIWA-Ar administered..."}]}
      ],
      "rationale": "Dimension 1 narration (≤40 words).",
      "confidence": "high"
    }
  ],
  "rules_fired": [
    "Medically managed care required (Dim 1 = 3A → Min Level 3.7)",
    "Any subdimension Min Level 3 → recommend Level 3.7",
    "3B not present in Dim 1/2 → not BIO",
    "Dim 3 = 1B (non-COE) → not COE"
  ],
  "rationale": "Overall narration (≤120 words).",
  "confidence": "high",
  "rationale_status": "ok",
  "rationale_warnings": [],
  "links": {
    "self": "/api/v1/asam-assessments/<id>",
    "fhir": "/fhir/ClinicalImpression/<id>"
  }
}
```

`level_display` carries the textual level name (`"Medically Monitored Intensive Inpatient"` for 3.7, `"Clinically Managed Medium-Intensity Residential"` for 3.5, etc.) per ASAM 4th-edition naming. `modifiers` is a list of any suffixes applied (e.g., `["COE"]`).

### 6.2 TJC audit response shape (`TjcAuditRead`)

```json
{
  "id": "<uuid>",
  "patient_id": "<uuid>",
  "computed_at": "2026-05-15T14:22:08-05:00",
  "evidence_hash": "9b1c4f2e8d3a5067",
  "model_version": "claude-sonnet-4-6",
  "cached": false,
  "summary": {
    "total_eps_audited": 13,
    "satisfied": 7,
    "gap": 5,
    "not_applicable": 1,
    "overall_status": "non-compliant"
  },
  "findings": [
    {
      "ep_code": "CTS.03.01.09",
      "ep_domain": "Measurement-based care",
      "status": "gap",
      "severity": "high",
      "negative_finding": true,
      "narrative": "Standard CTS.03.01.09: not met. Documentation review revealed a single PHQ-9 administered on the day of admission with no subsequent administration during the 8-day inpatient stay. This does not meet the requirement that validated measurement-based-care instruments be readministered at clinically meaningful intervals.",
      "citations": [
        {"document_id": "...", "char_start": 12397, "char_end": 12587, "snippet": "PHQ-9 administered 2026-05-04..."}
      ],
      "linked_planted_gap": "G1"
    }
  ],
  "rationale_status": "ok",
  "rationale_warnings": [],
  "links": {
    "self": "/api/v1/tjc-audits/<id>",
    "fhir_bundle": "/fhir/DetectedIssue?subject=Patient/<patient_id>&category=tjc-audit"
  }
}
```

`linked_planted_gap` is populated when a finding maps to one of the five planted gaps G1–G5; reviewers should see all five in the demo run.

### 6.3 New DB tables (one Alembic migration)

```python
class AsamAssessment(SQLModel, table=True):
    __tablename__ = "asam_assessment"
    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4)
    patient_id: UUID = Field(foreign_key="patient.id", index=True)
    evidence_hash: str = Field(index=True)            # xxhash64 hex
    recommended_level: str                            # "3.7" | "2.5-COE" | "3.7-BIO" | ...
    modifiers: list[str] = Field(sa_column=Column(JSONB))
    dimensions: dict = Field(sa_column=Column(JSONB))  # per-dim ratings + rationale + citations
    rules_fired: list = Field(sa_column=Column(JSONB))
    rationale: str
    confidence: str                                   # high | moderate | low
    rationale_status: str                             # ok | unavailable | degraded
    rationale_warnings: list = Field(default_factory=list, sa_column=Column(JSONB))
    model_version: str
    computed_at: datetime
    __table_args__ = (UniqueConstraint("patient_id", "evidence_hash", "model_version"),)

class TjcAuditResult(SQLModel, table=True):
    __tablename__ = "tjc_audit_result"
    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4)
    patient_id: UUID = Field(foreign_key="patient.id", index=True)
    evidence_hash: str = Field(index=True)
    findings: list = Field(sa_column=Column(JSONB))
    summary: dict = Field(sa_column=Column(JSONB))
    rationale_status: str
    rationale_warnings: list = Field(default_factory=list, sa_column=Column(JSONB))
    model_version: str
    computed_at: datetime
    __table_args__ = (UniqueConstraint("patient_id", "evidence_hash", "model_version"),)

class LlmInvocation(SQLModel, table=True):
    __tablename__ = "llm_invocation"
    id: UUID = Field(primary_key=True, default_factory=uuid.uuid4)
    endpoint: str                                     # "asam-loc" | "tjc-audit"
    patient_id: UUID = Field(index=True)
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    latency_ms: int
    response_hash: str
    citation_validation_passed: bool
    error_class: str | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
```

No changes to Phase 1 / Phase 2 tables.

### 6.4 Optional: `MeasureReport` for the overall TJC audit

The `DetectedIssue`-per-gap mapping is sufficient for the brief. As a stretch artifact, a single `MeasureReport` per audit can summarize the 13/13 → 7-satisfied / 5-gap / 1-N/A counts. This is M11.5 in the implementation plan (optional, scope-controlled by Q3).

### 6.5 ASAM-level CodeSystem + LOINC mapping

`ClinicalImpression.finding.itemCodeableConcept.coding` uses the custom system `http://perspectiveshealth.ai/CodeSystem/asam-level` (codes per §5.10). Optionally include a LOINC `coding` if a suitable LOINC code exists (e.g., `LP216241-7` "Level of care"). Decided in M9: prefer the custom system; LOINC is a stretch.

### 6.6 ASAM narration prompt (canonical)

```
System:
You are a clinical reviewer narrating an ASAM Criteria 4th Edition Level-of-Care
recommendation that has ALREADY been determined by a deterministic rule engine
applying Chapter 10 Dimensional Admission Criteria. Your sole job is to write
a concise, professionally-toned rationale that:
  (1) restates the engine's per-subdimension risk ratings,
  (2) cites ONLY evidence snippets provided in <evidence> blocks,
  (3) connects ratings to the recommended level via the rules in <rules_fired>.

DO NOT introduce new clinical findings. DO NOT speculate about diagnoses absent
from the evidence. If a dimension lacks evidence, write exactly:
"Documentation does not establish findings for this dimension."

You are not making the recommendation — the rule engine did. You are documenting
its reasoning.

User:
<patient_summary>...</patient_summary>
<dimensional_findings>...</dimensional_findings>
<evidence>... (Citations API document blocks) ...</evidence>
<rules_fired>...</rules_fired>
<recommendation>Level 3.7 (Medically Monitored Intensive Inpatient), non-COE, non-BIO</recommendation>
<task>
Produce JSON matching the AsamRationale schema:
  - overall_rationale (≤120 words)
  - dimensions[*].rationale (≤40 words each, citing evidence by index)
  - confidence ("high" | "moderate" | "low")
</task>
<output_format>
Respond ONLY with valid JSON matching the supplied schema. Begin with `{`.
</output_format>

Assistant (prefilled): {
```

Full prompt details + the TJC equivalent are in `documents/phase_3.md` §D.

### 6.7 Confidence labeling

Reported on every `AsamAssessment` and per dimension:
- **`high`** — every dimensional rating was determined from at least one `ExtractedObservation` with `confidence >= 0.8`.
- **`moderate`** — at least one dimension fell back on free-text `AsamEvidence` only (no LOINC-coded observation).
- **`low`** — at least one dimension required default-on-missing (insufficient evidence; the rule engine chose the conservative rating).

## 7. Non-Functional Requirements

- **Stack additions:** `anthropic>=0.40` (Python SDK with Citations API + Structured Outputs), `tenacity` (retry/backoff), `xxhash` (already on the project from Phase 2). Optionally `pybreaker` for the circuit-breaker; an inline 30-line implementation is acceptable too.
- **Determinism:** the rule engine is byte-stable across runs; the LLM is not, so the cache layer is the determinism boundary. Repeat POSTs return identical bytes.
- **Performance:** p95 for a *fresh* POST is ≤ 15 s (dominated by Claude latency on Sonnet 4.6 with ~6K input / ~2.5K output). p95 for a *cached* POST or GET is ≤ 250 ms. Documented in the README.
- **Cost:** ≤ $0.10 per fresh ASAM assessment, ≤ $0.10 per fresh TJC audit on Sonnet 4.6 (current pricing). Enabled prompt-cache reduces marginal cost ~30%. `LlmInvocation` rows make this auditable.
- **Pydantic / SQLModel discipline:** continue separating `db/` (table=True) from `schemas/` (response Pydantic models); FHIR-shaped schemas use the camelCase alias generator from Phase 2's `app/api/fhir_schemas.py`.
- **OpenAPI:** every new operation has an `operation_id`, a description, and at least one example.
- **Coverage target:** 90% line, **100%** on the rule-engine modules (`level_decision.py`, `risk_ratings.py`, `audit_functions.py`) — these are the safety-critical surfaces.
- **Logging:** the LLM client logs `anthropic-ratelimit-*` headers from every Claude response (proactive throttling signal); errors include the exception class and the request id.
- **Secrets:** `ANTHROPIC_API_KEY` lives in `.env`; never committed; the README documents the env var and how to obtain a key. CI uses a mocked Claude client (no live LLM calls in test).

## 8. Success Metrics

| # | Metric | Target |
|---|---|---|
| M1 | Marcus Reyes admission POST returns recommended_level == "3.7" | exact |
| M2 | A simulated "Day 8" POST (with the day-8 progress note ingested) returns recommended_level == "2.5" | exact |
| M3 | Marcus TJC audit surfaces all 5 planted gaps (G1–G5) in `findings[].linked_planted_gap` | 5/5 |
| M4 | The 13-EP catalog yields 7+ satisfied EPs on Marcus (demonstrates affirmative-coverage) | ≥ 7 |
| M5 | Every citation in every response round-trips: `raw_text[char_start:char_end] == snippet` | 100% |
| M6 | A second POST with no chart changes returns the cached row (`cached: true`, identical `id`, identical ETag) | 100% |
| M7 | `If-None-Match: <etag>` on the cached endpoint returns 304 | 100% |
| M8 | Every `ClinicalImpression` and `DetectedIssue` validates against `fhir.resources.R4B` | 100% |
| M9 | Every successful POST writes one `AsamAssessment` / `TjcAuditResult` row, one `LlmInvocation` row, and one `AuditEvent(action="compute")` row | 100% |
| M10 | Claude-unreachable path returns rule-engine-only output with `rationale_status: "unavailable"` (tested via mocked SDK that raises) | 100% |
| M11 | Citation-validation-failure path returns rule-engine-only output with `rationale_warnings: ["citation_validation_failed"]` | 100% |
| M12 | `examples/marcus_reyes_asam_admission.json` is the live admission response and re-validates round-trip | refreshed |
| M13 | README leads with the "deterministic core + LLM narration" framing and reviewers can read it end-to-end in <12 minutes | qualitative |
| M14 | `docs/adr/003-llm-narration.md` exists and is one page | qualitative |

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Rule-engine produces the wrong LoC for Marcus (off-by-one in the determination tree) | Med | High | M3 has dedicated unit tests against the Chapter 10 rules using a curated set of synthetic ratings; the engine module is 100%-covered. |
| Risk-rating computer misreads the existing `ExtractedObservation` / `AsamEvidence` rows and produces wrong subdimension ratings | Med | High | M2 has golden fixtures for Marcus's expected per-subdimension ratings; if the engine + ratings produce the correct level for Marcus, both modules are correct. |
| Claude returns hallucinated citations that pass the API-boundary check but fail the DB cross-check | Med | Med | Citation validator (§5.6); on failure retry once with corrective prompt; second failure → rule-engine-only output with warning. |
| Anthropic SDK retries deplete quota faster than expected during demo | Low | Med | Circuit breaker opens after 3 consecutive 429s; documented in the README; demo runs on a fresh API key. |
| Structured Outputs feature is not available on the model pinned at submission | Low | Med | Tool-use fallback with the same Pydantic schema; one-line config flip; documented as a degraded path in `claude_client.py`. |
| Latency causes reviewer to think the endpoint is broken | Med | Low | README documents 8–12s expected response; `/health` endpoint stays fast; `LlmInvocation` row exposes the real number. |
| FHIR ClinicalImpression / DetectedIssue mappings fail `fhir.resources.R4B` validation due to missing required fields | Med | Med | M11 validates every Bundle against the library before persisting; CI fails fast. |
| ASAM IP boundary accidentally violated (e.g., a paraphrased rubric snippet that's too close to the original) | Low | High | Code-review checklist in M2 + M5: every paraphrase carries a comment noting it's the project's own clinical language, not the ASAM source. |
| Cost on demo run blows out (re-running the demo loop 20×) | Low | Low | Cache-on-hash means re-runs are free after the first; `force_recompute: true` requires explicit opt-in. |
| `pyproject.toml` lock pins `anthropic` to a version without Citations API | Low | High | Pin floor as `anthropic>=0.40` (or whichever version listed Citations support on the GA path); document min version in the README. |
| Tests become flaky due to live LLM calls in CI | Med | Med | CI uses a mocked `ClaudeClient` (response fixtures committed under `tests/fixtures/llm/`); live LLM only runs in a `manual` pytest marker that the demo flow uses. |
| The same `evidence_hash` collides for two genuinely different evidence sets | Negligible | High | xxhash64 has ~1-in-1.8×10^19 collision odds; for an MVP this is not worth a sha256 upgrade. Documented. |

## 10. Open Questions

*All Phase 3 draft-stage questions resolved 2026-05-15 during PRD review:*

- **Q1 (resolved):** Default Claude model is `claude-sonnet-4-6`; configurable via `PHEALTH_LLM_MODEL`; Opus 4.7 documented as the escalation path. (Playbook §A4; PRD §5.6.)
- **Q2 (resolved):** FHIR mapping uses `ClinicalImpression` (ASAM) + per-gap `DetectedIssue` (TJC). `RiskAssessment` and `GuidanceResponse` were considered and rejected — ClinicalImpression's R4 definition is the closer semantic match. (Playbook §A6; PRD §5.10.)
- **Q3 (resolved):** `MeasureReport` per TJC audit is optional / out-of-scope for M0–M13; can be added as a stretch artifact in M11.5 if time permits.
- **Q4 (resolved):** `force_recompute` is the explicit opt-in for cache bypass; `Idempotency-Key` is the optional replay header. Neither is required to satisfy the brief; both are documented for the surveyor / utilization-review persona. (PRD §5.7, §5.11.)
- **Q5 (resolved):** Cache key includes `model_version`; a model swap creates a new cache namespace, so reviewers can compare Sonnet 4.6 vs Opus 4.7 outputs side-by-side by changing `PHEALTH_LLM_MODEL` and re-running. (PRD §5.7, §6.3.)
- **Q6 (resolved):** Test strategy uses a mocked Claude client by default; a `pytest -m manual` marker gates the live-LLM tests. CI never makes live calls. (PRD §7.)
- **Q7 (resolved):** No async / 202+polling. Synchronous POST with documented 8–12s latency. Reviewer time saved is worth more than the architectural purity of an async kick-off pattern at this scale. (PRD §3.)

No questions remain open for Phase 3. New questions surfaced during implementation should be tracked here or in `documents/phase_3.md`.

## 11. Out of Scope (deferred to Phase 3+ / production)

- **Async kick-off + status + manifest** pattern for the assessment endpoints (not required at MVP scale).
- **Real SMART on FHIR OAuth flow** (`X-API-Key` continues).
- **Multi-model ensembling** or self-critique loops.
- **Real-time webhook notifications** when a new assessment is computed.
- **An end-user UI**. `/docs` is the discoverability surface.
- **The 4th-edition "Co-Occurring Enhanced" (COE) program-capability flag at the *facility* level** — the recommendation engine can output COE-modifier levels, but pairing a patient to a facility is out of scope.
- **Real ASAM Continuing-Service / Discharge criteria** (Chapter 10 covers admission rules; service-continuation rules are separate and not required for the brief).
- **TJC `MeasureReport` resource** (optional stretch artifact M11.5).
- **Multi-tenant / multi-practice** — the schema and code assume a single clinic.

## 12. Deliverables & Submission

This phase produces the final artifacts the brief asks for:

- The Git repository URL — same repo, with M0–M13 commits visible.
- **`examples/marcus_reyes_asam_admission.json`** — the canonical `POST /asam-loc` response for Marcus on admission (Level 3.7), regenerated from the live endpoint.
- **`examples/marcus_reyes_asam_day8.json`** — the step-down response (Level 2.5).
- **`examples/marcus_reyes_tjc.json`** — the canonical `POST /tjc-audit` response, showing all 5 planted gaps + 7+ satisfied EPs.
- Optionally: `examples/marcus_reyes_clinical_impression_bundle.json` (the FHIR `/fhir/ClinicalImpression?subject=...` Bundle) and `..._detected_issue_bundle.json`.
- Updated README with a new top-level "Phase 3 — Clinical Decision Endpoints" section: the "deterministic core + LLM narration" framing, the "Why not LLM-only?" paragraph citing ASAM's IP notice, the two endpoint contracts, the cost / latency / cache story, the citation round-trip line.
- **`docs/adr/003-llm-narration.md`** — one-page Architectural Decision Record documenting "Why rule-based core + LLM narration?" (citing ASAM IP, FDA SaMD principles, reproducibility of CDS).
- **Demo recording add-on (3–5 min)**: per playbook §G.

Submission contract — per the brief, page 3:
- Email both `kyle@perspectiveshealth.ai` and `eshan@perspectiveshealth.ai`.
- Paste the `/chart` shape from Phase 2 + the `/asam-loc` response shape inline (truncated to ~80 lines each); attach the FHIR Bundles as separate files.

---

## Appendix A — Glossary

- **ASAM 4th edition** — *The ASAM Criteria, Volume 1: Adults*, 4th edition (2023). Chapter 10 carries the Level of Care Determination Rules (pp. 279–281) implemented in this phase.
- **COE** — Co-Occurring Enhanced. A modifier indicating the patient requires a program with enhanced capability for co-occurring mental-health and SUD treatment.
- **BIO** — Biomedical Enhanced. A modifier indicating biomedical complications (IV fluids, IV meds, advanced wound care) require enhanced biomedical capability at the LoC.
- **Subdimension** — a finer-grained rating axis within an ASAM dimension; e.g., Dim 1 has Intoxication, Withdrawal, and Addiction Medication Needs as subdimensions.
- **Risk-rating anchor** — the symbolic rating per subdimension (`0`, `ANY`, `EVAL`, `2`, `3A`, `3B`, `4`, etc.) that maps to a Minimum Level of Care.
- **`evidence_hash`** — `xxhash64` of the sorted contributing-row PKs; the cache key for `AsamAssessment` and `TjcAuditResult`.
- **Citations API** — Anthropic's primitive that ties a Claude response back to verbatim spans of supplied documents. GA on Claude API and Vertex AI since Jan 23 2025; on Amazon Bedrock since June 30 2025.
- **Structured Outputs** — Anthropic's schema-guaranteed JSON output mode. GA on Sonnet 4.5 / Opus 4.5 / Haiku 4.5 as of Feb 4 2026.
- **RFI** — Requirement For Improvement; the TJC term for a non-compliance finding. The TJC narration template mirrors RFI register.
- **EP** — Element of Performance; the most granular TJC requirement. The audit catalog covers 13 chart-auditable EPs.
- **G1–G5** — the five planted compliance gaps in Marcus Reyes's chart (from Phase 1 §6.3): no repeat PHQ-9 (G1), peer-support missing from treatment plan (G2), no C-SSRS reassessment (G3), no 72-h discharge plan (G4), inconsistent co-signature timestamp (G5).
- **`ClinicalImpression`** — the FHIR R4 resource carrying the ASAM assessment.
- **`DetectedIssue`** — the FHIR R4 resource carrying one TJC gap finding.
