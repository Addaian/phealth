# ADR-003: Rule-based clinical reasoning + LLM-narrated rationale

**Status:** Accepted
**Date:** 2026-05-15
**Phase:** 3

## Context

The Phase 3 endpoints (`POST /api/v1/patients/{id}/asam-loc` and
`POST /api/v1/patients/{id}/tjc-audit`) must produce two things at the same
time, with conflicting design pressures:

1. **A defensible clinical recommendation.** ASAM Level-of-Care and TJC
   compliance verdicts are quasi-regulatory artifacts. They have to be
   reproducible — the same chart should produce the same level across
   re-runs and across reviewers. Reproducibility is also an FDA SaMD
   principle: clinical decision-support systems whose outputs vary
   non-deterministically face a much steeper validation path. The
   submission reviewer is going to test "what happens when I POST
   twice?" — if the level changes, the trust signal collapses.
2. **A human-readable explanation that reads like clinical prose,**
   not a JSON dump of rule-engine output. The brief and the playbook
   both call for a narrative the reviewer can follow without holding
   the ASAM book open.

These pressures pull in opposite directions: deterministic engines
produce stable but boring output; LLMs produce fluent prose but are
not bit-stable (per Anthropic's own docs, even `temperature=0`
responses can drift). On top of that, ASAM publicly states:

> "Inputting ASAM Criteria and other ASAM intellectual property into
> artificial intelligence is strictly prohibited."

So we cannot just paste the Score Sheet anchor descriptions into a
Claude prompt and ask for a level. The TJC manual (CAMBHC) is similarly
proprietary.

## Decision

**Split the work**: the recommendation is computed by deterministic
Python; the rationale is written by Claude. Specifically:

1. **Rule engine** (`app/clinical/asam/level_decision.py`,
   `app/clinical/tjc/audit_functions.py`):
   - ASAM Chapter 10 Determination Rules (pp. 279–281) as a pure
     Python cascade over `MinLoc` membership sets.
   - 13 chart-auditable Joint Commission EPs as Python predicates over
     `TjcCoverage` + `ExtractedObservation` rows.
   - Output: `LevelDecision { level, modifiers, rules_fired }` and
     `list[EpFinding]`.

2. **Claude narration** (`app/clinical/asam/narration.py`,
   `app/clinical/tjc/narration.py`):
   - Forced tool-use against a Pydantic schema (`AsamRationale` /
     `TjcAuditResponse`). The schema has no field for "level" or
     "status" — Claude *cannot* change the recommendation.
   - Prompt is XML-tagged: `<dimensional_findings>`, `<evidence>`,
     `<rules_fired>`, `<recommendation>`, `<task>`. Evidence rides as
     Anthropic Citations-API document blocks; we re-validate every
     emitted citation against the local DB before persisting.

3. **Cache layer** (`AsamAssessment` / `TjcAuditResult` tables,
   keyed by `(patient_id, evidence_hash, model_version)`):
   - The cache is the determinism boundary. The engine is bit-stable;
     Claude isn't. A re-POST hits the cache and returns the byte-for-
     byte same response, ETag included. Re-ingestion of an amended
     chart changes the `evidence_hash` and invalidates the cache.

## What the LLM never sees

- No ASAM Score Sheet anchor text. No Chapter 10 rule text. No CAMBHC
  EP wording. Only the project's *derived clinical findings* (per-
  subdimension ratings, the engine's rules-fired list, the EP
  catalog paraphrased from public R3 reports).
- No FullText cross-patient evidence — only the AsamEvidence /
  EpFinding citation pointers we explicitly attached to this patient's
  assessment.

## Consequences

**Positive:**
- **Reproducible levels.** Marcus → 3.7 on every run, regardless of
  Claude's run-to-run drift. Tests pin this (`tests/clinical/
  test_deterministic_core.py`).
- **Auditable reasoning.** Every recommendation carries a
  `rules_fired` list documenting exactly which rules contributed.
  Reviewers can read the trace alongside the narrative.
- **Cited rationale.** Every span the LLM cites round-trips through
  `CitationValidator.validate_claims` — `raw_text[start:end] ==
  snippet` is asserted before the response ships.
- **ASAM / TJC IP respected.** Documented and tested. No copyrighted
  rubric text enters the prompt.
- **Graceful degradation.** When Claude is unreachable (rate-limited,
  5xx after retries, breaker open), the endpoint returns the engine's
  recommendation with `rationale_status: unavailable` and `x-rule-
  engine-only: true` headers. The clinical answer is still right; it's
  just missing the narrative.

**Negative / accepted trade-offs:**
- **Less natural-feeling rationale than a pure LLM might produce.**
  The LLM is constrained to narrate facts the engine derived; it
  can't speculate or fill in gaps. For some readers the prose will
  feel mechanical compared to free-form LLM output. We consider this a
  feature, not a bug.
- **Engine-rule encoding effort.** Chapter 10's rules and the 13-EP
  audit catalog each take ~200 lines of Python to encode faithfully.
  If a 5th edition ships, this is a real cost — but the cost is
  bounded and one-time.
- **Process-local circuit breaker.** Documented; a horizontally-scaled
  deployment would need a Redis-backed breaker. MVP-acceptable.

## Alternatives considered

- **LLM-only (Claude makes the level call directly).** Rejected: ASAM
  IP boundary, FDA SaMD reproducibility, and the unbounded validation
  surface that comes with letting an LLM emit a regulatory artifact.
- **Rule engine only, no narration.** Rejected: the brief calls for a
  cited rationale, and "level: 3.7, rules_fired: [...]" alone fails
  the reviewer-experience bar.
- **Two-pass LLM (Claude proposes a level, Claude critiques the
  level).** Rejected: doubles cost + latency, and still leaves Claude
  on the critical path of the recommendation.

## References

- `documents/phase_3_PRD.md` §1, §3, §5.6, §6.6 — design.
- `documents/phase_3.md` §A, §F — research, ASAM IP notice.
- FDA *Software as a Medical Device (SaMD): Clinical Evaluation*
  guidance — the reproducibility principle.
- Anthropic Citations API docs — the verbatim-span retrieval primitive
  we lean on.
