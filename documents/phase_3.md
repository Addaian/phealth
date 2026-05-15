# Phase 3 — Research Playbook: Cited-Rationale Clinical Endpoints

**Project:** Perspectives Health Intern Technical Assessment
**Phase:** 3 of 3 (corresponds to Task 3 in the brief)
**Date:** 2026-05-15
**Companion documents:** `phase_3_PRD.md`, `phase_3_implementation_plan.md`

This playbook captures the background research, citations, model-choice rationale,
and risk landscape that feed the Phase 3 PRD and implementation plan. It is the
*why* document; the PRD is the *what* document; the plan is the *how* document.

---

## TL;DR

- **Use the ASAM 4th-edition Level-of-Care Determination Rules verbatim** (*ASAM
  Criteria, Vol. 1: Adults*, Chapter 10, pp. 279–281) as a hard-coded decision
  tree in Python; the LLM (Claude Sonnet 4.6 at $3 / $15 per MTok, with Opus 4.7
  reserved for narration-quality fallback) only narrates. This survives ASAM's
  explicit prohibition on "inputting ASAM Criteria and other ASAM intellectual
  property into artificial intelligence" because no proprietary ASAM text is sent
  to the LLM — only your own clinical findings and the level the engine already
  decided.
- **Anthropic's Citations API + Native Structured Outputs are the right
  primitives**: feed each evidence span as a `custom_content` document chunk
  with `citations.enabled=true`; Claude returns content blocks with verified
  `citations[]` arrays. Per Anthropic's Citations launch post: "Our internal
  evaluations show that Claude's built-in citation capabilities outperform most
  custom implementations, increasing recall accuracy by up to 15%." Combine with
  a post-hoc validator that re-checks `raw_text[start:end] == cited_text`.
  Structured Outputs are GA on Sonnet 4.5 / Opus 4.5 / Haiku 4.5 as of
  Feb 4 2026 (per Anthropic's release notes) so schema compliance is
  *guaranteed*, not best-effort.
- **Both endpoints are POST returning 201 + cached `AsamAssessment` /
  `TjcAuditResult` rows keyed by `evidence_hash`**; ETag derives from that hash;
  GET retrieval and `If-None-Match` work exactly like Phase 2. Marcus Reyes will
  deterministically resolve to **Level 3.7** on admission and step down to **2.5**
  by day 8; the TJC endpoint will surface the five planted gaps plus 7–8
  satisfied EPs. Estimated build: **22–28 engineering hours**.

---

## Key Findings

### A. ASAM 4th-Edition Level-of-Care Prediction

**A1 — The 4th edition's decision logic is a deterministic decision tree, not a
scoring algorithm.**

Per the *ASAM Criteria 4th Ed. Level of Care Assessment Guide* (UCLA ISAP,
v4.1.0.0, Jan 2025), Chapter 10 of Volume 1 (pp. 279–281) publishes explicit
**Level of Care Determination Rules** in nested-conditional form (verbatim
summary from the Guide):

> *Inpatient Care (Level 4 / 4-Psychiatric):* If any subdimension requires Level 4
> → refer to Level 4. If meets 3.7 BIO AND any COE LoC → refer to Level 4. If
> meets Level 4 Psychiatric but NOT Level 4 or 3.7 BIO → refer to Level 4
> Psychiatric.
>
> *Medically Managed Care (1.7 / 2.7 / 3.7):* If not Level 4, and any subdimension
> requires medically managed care: if any subdim requires Min Level 3 → Level 3.7
> (or 3.7 BIO if 3B is rated in Dim 1 or 2); else if any Min Level 2 → 2.7; else
> 1.7.
>
> *Clinically Managed Residential (3.1 / 3.5):* If no medically managed care
> needed: if any subdim requires Min 3.5 → 3.5; else 3.1.
>
> *Clinically Managed Outpatient (1.5 / 2.1 / 2.5):* Recommend the most intensive
> subdimensional rating (2.5 > 2.1 > 1.5).

The logic is **highest-rating-wins across subdimensions** — implementable as a
30-line Python `match` statement, not a model. The 4th edition also publishes
**COE escalation rules** (any subdim flagged COE → final recommendation must be
COE; a 3.1 recommendation upgraded by COE becomes 3.5-COE; a 2.1 becomes
2.5-COE).

**Subdimensions are explicit and finite** (encode as `enum`s):

| Dim | Name | Subdimensions | Book pp |
|---|---|---|---|
| 1 | Intoxication, Withdrawal, Addiction Meds | Intoxication and Associated Risks; Withdrawal and Associated Risks; Addiction Medication Needs | 212–229 |
| 2 | Biomedical Conditions | Physical Health Concerns; Pregnancy-related Concerns | 230–239 |
| 3 | Psychiatric and Cognitive Conditions | Active Psychiatric Symptoms; Persistent Disability | 240–254 |
| 4 | Substance Use-related Risks | Likelihood of Risky Substance Use; Likelihood of Risky SUD-related Behaviors | 255–271 |
| 5 | Recovery Environment Interactions | Ability to Function Effectively; Support in Current Environment; Safety in Current Environment | 272–278 |
| 6 | Person-Centered Considerations | (no ratings — informs LoC *selection*, not *recommendation*) | — |

**Risk-rating anchors are symbolic, not numeric 0–4.** Example: Dim 1 Withdrawal
uses `{0, ANY, EVAL, 2, 3A, 3B, 4}` where `3A = Min 3.7` and `3B = Min 3.7-BIO`.
Treating these as integer scores loses information; encode them as `StrEnum`s.

**A2 — COE and BIO modifiers are rule-driven.** BIO triggers when Dim 1 or Dim 2
hits 3B (patient needs IV fluids, IV meds, or advanced wound care). COE triggers
when **any** subdimension carries a COE flag — most Dim 3 Active Psychiatric
Symptoms ratings (1A through 3B) map to COE levels. The Guide (p. iii) clarifies
that "suicidal thoughts without significant impulses" remains appropriate for
*standard* co-occurring-capable programs (non-COE). Combined 3.7-BIO + any COE
→ escalates to Level 4 per the Determination Rules (p. 280).

**A3 — Marcus Reyes resolves deterministically to Level 3.7 on admission.**
With CIWA-Ar=12 + concurrent alcohol/benzo dependence, Dim 1 Withdrawal = **3A
(Min 3.7)** — the Guide Score Sheet item 1A (p. 27) explicitly lists "withdrawing
from multiple substances and require frequent monitoring, including after-hours"
as a 3.7 trigger. PHQ-9=18 + GAD-7=15 + C-SSRS positive Q2 (passive ideation) →
Dim 3 = **1B (Min 1.7, non-COE)** per the "suicidal thoughts without significant
impulses" guidance. No biomedical complications → Dim 2 = 0. Dim 4 = D or E
(Min 3.1–3.5). Applying the rules: Medically managed care needed = yes
(Dim 1 = 3A); Min Level 3 met in any subdim = yes → **Recommend Level 3.7
(non-COE, non-BIO)**. The Day-8 step-down to 2.5 follows when Dim 1 Withdrawal
stabilizes to 0/ANY and Dim 4 drops to C (Min 2.5).

**A4 — Claude model choice (verified May 2026): Sonnet 4.6 ($3 in / $15 out per
MTok) is the right default.** Per Anthropic's pricing page (last updated April
2026 per finout.io/blog/anthropic-api-pricing): "Claude Opus 4.7 costs
$5.00/$25.00 per MTok. Claude Sonnet 4.6 costs $3.00/$15.00. Claude Haiku 4.5
costs $1.00/$5.00." Sonnet 4.6 launched Feb 17 2026 at 79.6% on SWE-bench
Verified — within 1.2 points of Opus 4.6. For a narration task constrained by
structured output and pre-computed evidence, Sonnet 4.6 is sufficient. Reserve
Opus 4.7 (87.6% SWE-bench Verified per benchlm.ai/blog/posts/claude-api-pricing)
for hard cases where narration quality matters more than cost. Avoid Haiku 4.5:
73.3% SWE-bench (per anthropic.com/claude/haiku: "Claude Haiku 4.5 scores 73.3%
on SWE-bench Verified"); knowledge cutoff July 2025 per Artificial Analysis —
clinical narration quality drops materially.

**Anthropic Citations API** (GA on Claude API and Vertex AI since Jan 23 2025;
on Amazon Bedrock since June 30 2025 per the update note on Anthropic's launch
post; supported on all active Claude models except Haiku 3) is the single most
important Phase-3 primitive. Per Anthropic's Citations docs: "For plain text
documents: Citations include the character index range (0-indexed)." The shape
for each AsamEvidence snippet:

```python
{"type": "document",
 "source": {"type": "text", "media_type": "text/plain", "data": snippet},
 "title": f"doc:{document_id}#chars:{char_start}-{char_end}",
 "context": json.dumps({"doc_id": str(doc_id), "evidence_row_id": str(row_id),
                        "char_start": char_start, "char_end": char_end}),
 "citations": {"enabled": True}}
```

Claude returns content blocks whose `citations` arrays reference document
indices and `cited_text`; you reverse-resolve via `context` and re-validate
against your DB.

**Native Structured Outputs** are **GA** (not beta) as of Feb 4 2026: per
Anthropic's release notes (platform.claude.com), "Structured outputs are now
generally available on the Claude API for Claude Sonnet 4.5, Claude Opus 4.5,
and Claude Haiku 4.5. GA adds support for more complex schemas." (The feature
launched in public beta on Nov 14 2025.) Anthropic guarantees the model's
output will adhere to a specified JSON schema. Use this for the rationale
envelope. If for any reason a deployment pins an older model where SO is
unsupported, fall back to tool-use with a single forced tool whose
`input_schema` is your Pydantic model dumped via `.model_json_schema()`.

**A5 — Determinism strategy: cache, don't pray.** Even at `temperature=0`,
Claude is not bit-stable. Persist every assessment in an `AsamAssessment` table
keyed by `(patient_id, evidence_hash)` where `evidence_hash =
xxhash64(sorted(evidence_row_ids + extracted_obs_ids))`. POST
computes-or-fetches; subsequent POSTs with the same hash return the cached row.
ETag = `W/"{evidence_hash}-{model_version}"`. Re-ingestion changes the hash and
naturally invalidates the cache.

**A6 — FHIR mapping: `ClinicalImpression` is the correct resource, not
`RiskAssessment`.** Per HL7 R4B: "A record of a clinical assessment performed
to determine what problem(s) may affect the patient and before planning the
treatments or management strategies." `ClinicalImpression.finding.itemCodeableConcept`
carries the recommended level; `summary` carries the rationale;
`investigation.item` references the underlying ExtractedObservation rows.
`GuidanceResponse` is conceptually fine but designed for CDS Hooks
request/response (HL7 FHIR R4: "aligned with the CDS Hooks response
structure"); `ClinicalImpression` is the closer semantic match for "a clinician
(or rule engine) examined the chart and reached this conclusion." For TJC
findings use `DetectedIssue` per gap (see B4) plus an overall `MeasureReport`.

### B. TJC Compliance Audit

**B1 — A defensible 13-EP behavioral-health audit catalog** (paraphrased; never
quote verbatim from the proprietary CAMBHC manual):

| EP code | Domain | Chart-auditable signal | Marcus mapping |
|---|---|---|---|
| **CTS.02.03.07 EP 1** | SUD history collection (R3-25) | Patterns, route, frequency documented | satisfied |
| **CTS.02.03.07 EP 7** | Withdrawal/intoxication risk in assessment (R3-25) | CIWA-Ar / COWS scored | satisfied |
| **CTS.02.01.07 EP 1** | H&P within 24h of inpatient crisis-stabilization admission | Signed, timestamped H&P | satisfied |
| **CTS.03.01.03 EP 28** | Individualized treatment plan at admission (R3-25) | Plan dated day-of-admission with assessed needs | **G2 (peer-support not on plan)** |
| **CTS.03.01.09** | Measurement-based care | Repeat administration of validated instrument | **G1 (no repeat PHQ-9)** |
| **CTS.04.02.33** | MOUD offered for OUD | MOUD offered/declined documented | n/a (no OUD) |
| **CTS.04.02 (TOC)** | Care transition / discharge plan (R3-25) | Discharge plan present prior to discharge | **G4 (no 72-h plan)** |
| **NPSG.15.01.01 EP 2** | Suicide risk screening with validated tool | C-SSRS at admission | satisfied |
| **NPSG.15.01.01 EP 3** | Suicide risk *assessment* on positive screen, with reassessment | Documented severity + reassessment | **G3 (no C-SSRS reassessment)** |
| **NPSG.15.01.01 EP 4** | Documented overall risk + mitigation plan | Plan present, risk level explicit | satisfied |
| **RC.01.01.01 EP 7** | Entries authenticated, dated, timed | Every note signed + timestamped | satisfied |
| **RC.01.02.01 EP 3** | Co-signatures by qualified physician for delegated H&P | Countersignature timestamp consistent | **G5 (inconsistent timestamp)** |
| **RC.01.03.01** | Timeliness of record completion | Notes filed within org-defined window | satisfied |

This maps cleanly to all five planted gaps; the catalog also supports the 7–8
"satisfied" findings needed to demonstrate the auditor's affirmative-coverage
capability.

**B2 — Audit functions are Python predicates over `TjcCoverage` +
`ExtractedObservation`.** One function per EP returns `EPFinding(ep_code,
status, evidence_pointers, finding_template)`. The `TjcCoverage` matrix from
Phase 1 already encodes satisfied/gap/ambiguous status; audit functions add the
surveyor-grade reasoning (e.g., "PHQ-9 administered at intake on day 1 but not
readministered during the remaining 7 days of admission"). For
**absence-of-evidence** findings (G1, G3, G4), the audit function returns a
`negative_finding=True` flag and the LLM is prompted with "Documentation review
confirms the absence of [requirement]" rather than asked to cite a span.

**B3 — Surveyor-style narration uses a tighter, template-driven register.** TJC
RFI ("Requirement For Improvement") language follows a near-template structure:
*"Standard [code] not met. Documentation review of [patient identifier] revealed
[specific finding]. [Time/context]. This does not meet the requirement that
[paraphrased EP]."* Pre-template this; have the LLM fill three slots only:
`specific_finding`, `time_context`, `paraphrased_EP`. Collapses prompt cost to
~200 tokens per finding. Batch all 13 EPs into a single Claude call using
structured outputs returning a `findings: List[Finding]` schema — one
round-trip, ~$0.077.

### C. Shared Architecture

**C1 — Module layout** (additions only):

```
app/
  clinical/
    asam/
      rubric.py            # subdim → symbolic rating enums; Risk Rating Form data
      risk_ratings.py      # ExtractedObservation + AsamEvidence → per-subdim rating
      level_decision.py    # 4th-ed Chapter 10 decision tree (pp 279–281)
      narration.py         # Claude prompt + parser
      schemas.py           # Pydantic v2 response models
    tjc/
      ep_catalog.py        # 13-EP table, paraphrased requirements
      audit_functions.py   # one predicate per EP
      narration.py         # surveyor-template prompt
      schemas.py
    llm/
      claude_client.py     # singleton w/ retry/backoff/circuit-breaker
      prompts.py           # XML-tagged prompt templates
      citation_validator.py
      structured_output.py # native Anthropic SO + tool-use fallback
    shared/
      evidence_retrieval.py
      response_builder.py  # builds FHIR ClinicalImpression / DetectedIssue
  models/
    asam_assessment.py
    tjc_audit_result.py
    llm_invocation.py
  api/v1/{asam_loc.py, tjc_audit.py}
  api/fhir/{clinical_impression.py, detected_issue.py}
```

**C2 — The Claude client.** A single `ClaudeClient` class wrapping
`anthropic.Anthropic()` with `max_retries=3` (SDK default per PyPI docs:
"Certain errors will be automatically retried 2 times by default, with a short
exponential backoff. Connection errors, 409 Conflict, 429 Rate Limit, and >=500
Internal errors will all be retried by default"). Per the SitePoint production
guide, parse `anthropic-ratelimit-*` headers on every response — not just 429s
— to enable proactive throttling. Add a circuit breaker that opens after 3
consecutive 429s within 60s. Request timeout = 60s (Sonnet 4.6 P95 latency for
~10k input / 3k output is ~8–12s).

Sketch:
```python
class ClaudeClient:
    def __init__(self, model="claude-sonnet-4-6"):
        self._client = anthropic.Anthropic(max_retries=3, timeout=60)
        self.model = model
        self._breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60)

    @retry(reraise=True, stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, max=20),
           retry=retry_if_exception_type(anthropic.APIStatusError))
    def messages_create(self, **kwargs):
        with self._breaker:
            return self._client.messages.create(model=self.model, **kwargs)
```

**C3 — Prompt engineering, the four non-negotiables** (per Anthropic's "Use XML
tags to structure your prompts" docs):
1. **XML-tagged sections**: `<system_role>`, `<dimensional_findings>`,
   `<evidence>`, `<rule_engine_decision>`, `<task>`, `<output_format>`. Per the
   Anthropic docs: "XML tags can be a game-changer. They help Claude parse your
   prompts more accurately."
2. **Documents first, question last** — place evidence corpus before the task
   instruction (standard long-context guidance).
3. **Pre-fill the assistant turn** with `<rationale>` or `{` to constrain
   format.
4. **Explicit refusal clause**: "If the evidence does not support a claim, do
   not make it. Output `EVIDENCE_INSUFFICIENT` for that field." — repeated
   across Anthropic's anti-hallucination guidance.

**C4 — New DB tables** (Alembic migration `phase3_001_clinical_assessments`):

```python
class AsamAssessment(SQLModel, table=True):
    id: UUID = Field(primary_key=True)
    patient_id: UUID = Field(foreign_key="patient.id", index=True)
    evidence_hash: str = Field(index=True)          # xxhash64
    recommended_level: str                           # "3.7" | "2.5" | "3.7-COE" ...
    dimensions: dict = Field(sa_column=Column(JSONB))
    rules_fired: list = Field(sa_column=Column(JSONB))
    rationale: str
    model_version: str                               # "claude-sonnet-4-6"
    confidence: str                                  # high|moderate|low
    computed_at: datetime
    __table_args__ = (UniqueConstraint("patient_id", "evidence_hash"),)

class TjcAuditResult(SQLModel, table=True):
    id: UUID = Field(primary_key=True)
    patient_id: UUID = Field(foreign_key="patient.id", index=True)
    evidence_hash: str = Field(index=True)
    findings: list = Field(sa_column=Column(JSONB))
    summary: dict = Field(sa_column=Column(JSONB))
    model_version: str
    computed_at: datetime

class LlmInvocation(SQLModel, table=True):
    id: UUID = Field(primary_key=True)
    endpoint: str
    patient_id: UUID = Field(index=True)
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int = 0
    latency_ms: int
    response_hash: str
    citation_validation_passed: bool
    occurred_at: datetime
```

**C5 — Endpoint contracts:**

```
POST /api/v1/patients/{patient_id}/asam-loc
  Headers: Idempotency-Key (optional), Authorization
  Body:    { "force_recompute": false }
  Returns: 201 + body  (newly computed; Location: /api/v1/asam-assessments/{id})
           200 + cached body (hash matches existing row)
           422 + RFC 7807 (no AsamEvidence)
           503 + RFC 7807 problem (Claude unreachable; rule-engine-only fallback)
  ETag: W/"{evidence_hash}-{model_version}"

GET  /api/v1/asam-assessments/{id}         → 200, 304 on If-None-Match
GET  /fhir/ClinicalImpression?subject=...  → FHIR Bundle

POST /api/v1/patients/{patient_id}/tjc-audit  (same semantics)
GET  /fhir/DetectedIssue?subject=...&category=tjc-audit
```

**C6 — Error handling tiers:**
- **Claude 429** → SDK auto-retries; on final failure → return 503 +
  `application/problem+json` (RFC 7807) with `type: /errors/llm-rate-limited`
  and `Retry-After` header.
- **Claude 5xx** → SDK retries; on final fail return **200 with rule-engine-only
  output** and headers `x-rationale-status: unavailable`, `x-rule-engine-only:
  true`. The recommended level is deterministic and still valid.
- **Malformed citations** from Claude → post-hoc validate (`raw_text[start:end]
  == cited_text`); if any citation fails, retry once with a corrective prompt;
  on second failure, return rule-engine-only with `rationale_warnings:
  ["citation_validation_failed"]` in the response body.
- **Evidence-hash mismatch** (re-ingestion) → cache miss → recompute.

**C7 — Cost analysis (Sonnet 4.6 at $3 / $15 per MTok, May 2026):**
- Marcus chart: ~25 KB raw text ≈ 6.5 K tokens. We do **not** send the full
  chart — only retrieved evidence spans (~30 spans × ~150 tokens = 4.5 K) +
  system prompt + schema overhead (~1 K) → **~6 K input tokens**.
- Output: ~2.5 K tokens (per-dimension paragraph + overall rationale).
- **Per ASAM assessment**: 6 K × $3/MTok + 2.5 K × $15/MTok = **$0.056**.
- **Per TJC audit** (13 EPs batched): ~8 K in + ~3.5 K out ≈ **$0.077**.
- **Demo run** (5 + 5): ≈ **$0.67**. Production: 10 charts/day ≈ $1.30/day;
  1 000/day ≈ $130/day. Enable prompt caching for the system prompt + EP
  catalog (cache reads bill at 0.1× input rate per Anthropic pricing) and the
  marginal cost drops ~30%.

---

### D. Prompt Engineering Deep Dive

**D1 — Full ASAM rationale prompt** (~600 tokens): see `phase_3_PRD.md` §6.6 for
the canonical template. Key constraints:
- The LLM is *narrating an already-decided recommendation*; it must not invent
  findings or alter the level.
- If a dimension lacks evidence, the response must say so explicitly — the model
  is told to output the literal phrase "Documentation does not establish
  findings for this dimension."
- The rule engine's `rules_fired` list is included in the prompt so the narration
  can mirror the engine's reasoning chain.

**D2 — TJC surveyor prompt** uses a tighter RFI-style template, batches all 13
EP findings into one Claude call, and supplies a paraphrased EP catalog as part
of the prompt so the LLM never has to recall standard text from training data.

**D3 — Citation validator.** The Citations API guarantees the API-boundary
contract, but we re-validate against the DB because the canonical truth lives
there, not in the prompt payload:

```python
class CitationValidator:
    def __init__(self, db: Session): self.db = db

    def validate(self, claim: ClaimWithCitations) -> ValidationResult:
        failures = []
        for cite in claim.citations:
            # 1. Document exists and belongs to this patient
            doc = self.db.get(ClinicalDocument, cite.document_id)
            if not doc or doc.patient_id != claim.patient_id:
                failures.append(("unknown_doc", cite)); continue
            # 2. Char range yields a real substring
            if cite.char_end > len(doc.raw_text) or cite.char_start < 0:
                failures.append(("out_of_range", cite)); continue
            # 3. The cited snippet matches actual raw_text slice
            actual = doc.raw_text[cite.char_start:cite.char_end]
            if actual != cite.snippet:
                failures.append(("snippet_mismatch", cite))
        return ValidationResult(passed=not failures, failures=failures)
```

---

### E. Recommendations (Decision-Ready)

1. **Start with `claude-sonnet-4-6`** for both endpoints. Switch to
   `claude-opus-4-7` only if reviewer rationale quality on the demo chart is
   judged unacceptable.
2. **Use Anthropic Citations API + GA Structured Outputs.** Do not roll your own
   JSON-mode prompt engineering — the GA path is documented, schema-guaranteed,
   and cuts ~200 lines of fragile parsing code.
3. **Cache aggressively** via `evidence_hash` + `AsamAssessment` /
   `TjcAuditResult` rows. ETag = `W/"{hash}-{model}"`.
4. **Frame Phase 3 in the README as "deterministic clinical reasoning with
   LLM-narrated rationale."** This is the architecturally defensible story;
   reviewers will recognize it as the responsible approach versus "ask Claude
   for a diagnosis."
5. **Add a one-paragraph "Why not LLM-only?" section to the README** citing
   ASAM's explicit IP-protection notice and explaining how the design respects
   it: no ASAM proprietary rubric text is ever sent to Claude — only your
   derived clinical findings and the engine's already-computed decision.
6. **Threshold to revise approach**: if rule-engine deterministic output for
   Marcus *doesn't* produce 3.7-on-admission and 2.5-by-day-8 from the existing
   AsamEvidence rows, the bug is in the risk-rating computer, not in the
   decision tree — Chapter 10's rules are pure data.

---

### F. Caveats and Pitfalls

- **ASAM IP**: ASAM publicly states "Inputting ASAM Criteria and other ASAM
  intellectual property into artificial intelligence is strictly prohibited."
  This playbook respects that — the LLM only sees your derived findings and the
  engine's decision. Do not paste passages from the ASAM Criteria book or
  Assessment Guide into prompts. Paraphrase or restate as your own clinical
  language in the EP catalog and dimension descriptions.
- **TJC manual is also proprietary**: paraphrase all EP requirements; never
  quote verbatim from CAMBHC. Keep the paraphrased catalog in `ep_catalog.py`
  with a comment noting "Paraphrased from public R3 reports and standard FAQs;
  not a verbatim reproduction of the CAMBHC manual."
- **Model version drift**: Anthropic deprecates older models on rolling cycles.
  The 4.5 generation already has GA structured outputs; assume Sonnet 4.6 /
  Opus 4.7 are current at submission but pin via env var `ASAM_LLM_MODEL` so
  switching is a config change, not a code change. Log `model_version` on every
  `AsamAssessment` row.
- **Latency**: Sonnet 4.6 on ~6K input / ~2.5K output averages 8–12s. For a
  synchronous POST that's acceptable; document the expected response time in
  the README. Do *not* introduce 202+polling for the assessment endpoint —
  unjustified complexity at this stage.
- **Claude is not bit-stable at `temperature=0`**: the cache-on-hash strategy is
  mandatory if reviewers will rerun the demo and expect identical text.
- **Citation API quirks**: Citations work best with sentence-chunked plain-text
  documents. For longer evidence (e.g., a paragraph spanning the patient's
  environment), pass each sentence as its own document; otherwise Claude may
  cite the entire paragraph when only one sentence supports the claim.
- **TJC "absence" findings cannot be cited**. Build the negative-finding
  template into the audit functions; do not ask Claude to invent a span for
  "nothing was found." The validator will fail otherwise.
- **The five planted gaps must be findable from `TjcCoverage`** — if any of
  G1–G5 is not pre-encoded in the matrix, the rule engine cannot fire and the
  LLM will not surface it. Sanity-check the `TjcCoverage` matrix against the
  13-EP catalog before writing audit functions.
- **Confidence labeling**: report `confidence: "high"` only when every
  dimensional rating was determined from at least one ExtractedObservation with
  `confidence >= 0.8`; `moderate` if any dimension fell back on free-text
  AsamEvidence only; `low` if any dimension required default-on-missing.

---

### G. Submission Polish

- **README updates** (new top-level section, after Phase 2): "Phase 3 — Clinical
  Decision Endpoints." Frame as: "Rule-based deterministic core + LLM-narrated
  cited rationale. ASAM 4th-edition Chapter 10 Determination Rules are
  implemented as Python; Claude Sonnet 4.6 writes the human-readable rationale
  using only evidence the system retrieved."
- **New sample JSON files**: `examples/marcus_reyes_asam_admission.json` (Level
  3.7), `examples/marcus_reyes_asam_day8.json` (step-down to 2.5),
  `examples/marcus_reyes_tjc.json` (5 gaps + 7 satisfied).
- **Demo recording add-on (3–5 min)**: (1) POST `/api/v1/patients/{marcus}/asam-loc`
  → 201 + cited rationale; (2) POST again → cached 200 with same ETag;
  (3) GET `/fhir/ClinicalImpression?subject=Patient/{marcus}`; (4) POST
  `/api/v1/patients/{marcus}/tjc-audit` → walk through the 5 gap findings;
  (5) show one `LlmInvocation` row demonstrating cost/latency tracking.
- **Architectural decision record** (new file `docs/adr/003-llm-narration.md`):
  one page documenting "Why rule-based core + LLM narration?" — cites ASAM IP
  notice, FDA SaMD principles around deterministic reasoning, and
  reproducibility of clinical decision support. The single highest-leverage
  submission artifact for signaling production-grade thinking.
