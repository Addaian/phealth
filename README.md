# Perspectives Health — Clinical Ingestion Substrate

A **FHIR-R4-shaped ingestion and extraction substrate** over the SimplePractice
Data Export, with a **dual-surface read API** on top: an ergonomic
`/api/v1/*` namespace and a strict `/fhir/*` namespace, with char-offset
provenance on every observation in every response.

> **This is not a wrapper.** SimplePractice has no public chart-data API; the
> only sanctioned programmatic path is the Data Export — a directory tree of
> text-layer PDFs and vCards. The substrate ingests that export into a
> FHIR-shaped row store with pre-computed evidence tables, so the downstream
> clinical-intelligence work (Task 2 extraction, Task 3 reasoning) becomes a
> *retrieval* problem rather than a full-document inference problem.

---

## The patient (TL;DR)

**Marcus J. Reyes**, 34 M — concurrent alcohol + benzodiazepine use disorder,
admitted 2026-05-04 for medically monitored withdrawal management (ASAM 3.7).
His synthetic chart is **one Biopsychosocial (BPS) intake plus three progress
notes** (SOAP, DAP, DSAP) across an 8-day admission, with **seven validated
scales embedded verbatim** (PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS,
C-SSRS) and **five intentional, auditable TJC compliance gaps**. The persona
is the single source of truth (`app/synthetic/persona.yaml`); every ASAM
dimension and every gap traces back to a field there.

> **Just want to see the output?**
> - [`examples/marcus_reyes_chart.json`](examples/marcus_reyes_chart.json) — the
>   canonical `GET /api/v1/patients/{id}/chart` response (Task 2 deliverable).
> - [`examples/marcus_reyes_everything.json`](examples/marcus_reyes_everything.json)
>   — the strict-FHIR `GET /fhir/Patient/{id}/$everything` Bundle.

---

## Architecture

```
SimplePractice (Essential trial)
  1 patient: Marcus J. Reyes — 1 BPS intake + 3 progress notes ([SOAP] [DAP] [DSAP])
        |  Settings → Practice → Data Export → Complete
        v
  data/synthetic_export/Marcus Reyes/   (vCard demographics + text-layer PDFs)
        |
        v   POST /ingest/simplepractice-zip   (202 Accepted; background task)
  ┌──────────────────────────────────────────────────────────────────┐
  │  Phase 1 substrate (PostgreSQL 16)                                 │
  │                                                                    │
  │   Patient · Encounter · ClinicalDocument                           │
  │   (raw_text + sectioned JSONB + fhir_json + tsvector)              │
  │   ExtractedObservation (char-offset provenance)                    │
  │   AsamEvidence · TjcCoverage (pre-computed evidence)               │
  │   AuditEvent (append-only event log)                               │
  └──────────────────────────────────────┬───────────────────────────┘
                                         │
        ┌────────────────────────────────┴────────────────────────────────┐
        │                                                                  │
        ▼                                                                  ▼
┌─────────────────────────────────────┐         ┌─────────────────────────────────────┐
│  /api/v1/*  (ergonomic snake_case)   │         │  /fhir/*  (strict FHIR R4)           │
│                                     │         │                                     │
│  /patients/{id}/chart  ← Task 2     │         │  /Patient/{id}                       │
│  /patients/{id}/intake              │         │  /Patient/{id}/$everything (Bundle)  │
│  /patients/{id}/timeline            │         │  /DocumentReference   (searchset)    │
│  /patients/{id}/observations        │         │  /Observation         (searchset)    │
│  /patients/{id}/asam-evidence       │         │  /Provenance          (searchset)    │
│  /patients/{id}/tjc-coverage        │         │  /metadata            (CapabilityStmt)│
│  /patients · /capabilities          │         │                                     │
│  errors: RFC 7807                   │         │  errors: OperationOutcome            │
│  pagination: cursor                 │         │  pagination: Bundle.link[next]       │
└─────────────────────────────────────┘         └─────────────────────────────────────┘
        │                                                                  │
        └──────────────────────────┬───────────────────────────────────────┘
                                   │
                                   ▼
                cross-cutting middleware / dependencies
                • X-API-Key auth (every endpoint except /health and /fhir/metadata)
                • audit-on-read (BackgroundTasks → AuditEvent row)
                • ETag + If-None-Match → 304 (deterministic over chart contents)
                • RFC 7807 / OperationOutcome unified exception handler
```

Both surfaces are thin views over the same row store: no new tables, no new
ingest path.

---

## Phase 2 deliverable — `GET /api/v1/patients/{id}/chart`

One consolidated JSON object containing the brief's literal ask (patient +
intake + timeline) plus the extraction surplus. **Every observation carries
char-offset provenance whose snippet round-trips
`raw_text[char_start:char_end] == snippet` exactly.**

```jsonc
{
  "meta": {
    "generated_at": "2026-05-12T17:00:00",        // latest doc authored_on, NOT now()
    "extraction_version": "phase2-v1.0.0",
    "completeness_score": 1.0,                    // |found| / (|found| + |looked_but_missing|)
    "etag": null,                                 // ETag HTTP header is the source of truth
    "audit_id": null, "compliance_check_id": null // pre-allocated for Phase 3
  },
  "patient": {
    "id": "<uuid>",
    "identifiers": [{"system": "simplepractice", "value": "106915126"}],
    "name": {"given": ["Marcus"], "family": "Reyes"},
    "birth_date": "1991-04-11", "gender": "unknown"
  },
  "intake": {
    "document_id": "<uuid>",
    "document_type": "biopsychosocial",
    "loinc_type": {"system": "http://loinc.org", "code": "11488-4", "display": "Consultation Note"},
    "sections": {
      "<key>": {"status": "found",              "text": "...", "provenance": {...}},
      "<key>": {"status": "looked_but_missing", "text": null,  "provenance": null},
      "<key>": {"status": "not_assessed",       "text": null,  "provenance": null}
    },
    "scales": [
      {"instrument": "PHQ-9 total score", "score": 18, "status": "found",
       "provenance": {"document_id": "<uuid>", "char_start": 12397, "char_end": 12410,
                      "snippet": "PHQ-9 = 18"}}
    ]
    // ...
  },
  "timeline": [ /* progress notes, each bound to LOINC 11506-3 Progress Note */ ],
  "observations": [ /* every LOINC-coded value, each with provenance */ ],
  "asam_summary": {"dim_1": 24, "dim_2": 14, "dim_3": 18, "dim_4": 11, "dim_5": 9, "dim_6": 7},
  "tjc_coverage": {"covered_eps": 3, "total_eps": 8,
                   "gap_eps": ["CTS.03.01.09", "CTS.03.01.03", "NPSG.15.01.01",
                                "R3-25", "RC.01.02.01"]}
}
```

See [`examples/marcus_reyes_chart.json`](examples/marcus_reyes_chart.json) for
the full live response.

---

## The dual-surface argument

- **`/api/v1/*`** is ergonomic snake_case. A reviewer hits `/chart` and gets
  one JSON with everything inline. Errors are RFC 7807 Problem Details
  (`application/problem+json`). ETag + `If-None-Match` → `304 Not Modified`.
- **`/fhir/*`** is strict FHIR R4 (via `fhir.resources.R4B`). Every response
  validates against the spec. Search results are `Bundle.searchset` with
  cursor pagination via `Bundle.link[rel=next]`. Errors are `OperationOutcome`
  (`application/fhir+json`). `/fhir/metadata` is the auth-free
  `CapabilityStatement` that self-describes the surface.

Reviewers grading the brief read `/chart`; FHIR clients (Synthea, OpenEMR
integrations) read `/fhir/Patient/{id}/$everything`. Same data, two shapes,
no parallel pipeline — the FHIR surface is a different projection of the same
row store.

---

## Custom Provenance extension

Every LOINC-coded Observation has a paired FHIR `Provenance` resource whose
`entity.extension` carries the **char-offset selector** for the source text:

```
URL: http://perspectiveshealth.ai/fhir/StructureDefinition/text-position-selector

Shape (complex extension):
  {
    "url": "<URL above>",
    "extension": [
      {"url": "start", "valueInteger": 12397},
      {"url": "end",   "valueInteger": 12410}
    ]
  }
```

Modelled on the FHIR R5 `text-position-selector` (derived from the W3C Web
Annotation spec), back-ported here as an R4 extension. When `fhir.resources`
upgrades to R5 this can be replaced with the standard slot in one
search-and-replace.

The round-trip invariant is asserted across every endpoint that surfaces
observation provenance — `/chart`, `/observations`, `/fhir/Observation`,
`/fhir/Patient/{id}/$everything`, `/fhir/Provenance` — by
`tests/test_provenance.py` (Success Metric M2, 100%).

---

## The five intentional compliance gaps

The synthetic chart is *engineered* to fail specific TJC Elements of
Performance, so the audit logic has something real to find. The coverage
matrix flags all five as `gap`, alongside three `satisfied` EPs for contrast.

| ID | Element of Performance | The embedded gap |
|----|------------------------|------------------|
| G1 | CTS.03.01.09 | PHQ-9 administered at intake (total 18); never re-administered in any progress note, despite the treatment plan specifying bi-weekly PHQ-9. |
| G2 | CTS.03.01.03 | The day-5 DAP note documents a peer recovery support group; the treatment plan never lists peer support — a golden-thread break. |
| G3 | NPSG.15.01.01 | The day-8 plan calls for C-SSRS re-administration before transfer; no C-SSRS result is ever documented. |
| G4 | R3-25 | No discharge / transition-of-care planning within 72 hours of admission. |
| G5 | RC.01.02.01 | The day-5 note is authored and signed solely by a non-clinical peer specialist (CPRS); no clinical co-signature. |

> G2 deliberately mirrors Perspectives Health's own landing-page audit example
> ("Progress Note documents a Peer Support session. However, the current
> Treatment Plan does not list Peer Support as an intervention").

---

## How to run

```bash
cp .env.example .env                              # dev defaults; .env is gitignored

docker compose up -d                              # Postgres 16 and the app
docker compose exec app alembic upgrade head      # build the schema

# Ingest the committed synthetic export (the endpoint takes a ZIP):
( cd data/synthetic_export && zip -rq /tmp/export.zip "Marcus Reyes" )
curl -X POST localhost:8000/ingest/simplepractice-zip \
     -H "X-API-Key: phealth_dev_ingest_key" \
     -F "file=@/tmp/export.zip"
# -> 202 Accepted; the patient id is logged: `docker compose logs app`

# Discover the patient id:
PID=$(curl -s -H "X-API-Key: phealth_dev_ingest_key" \
        localhost:8000/api/v1/patients | jq -r '.[0].id')

# The Task 2 deliverable -- one consolidated JSON:
curl -H "X-API-Key: phealth_dev_ingest_key" localhost:8000/api/v1/patients/$PID/chart

# The strict-FHIR surface:
curl localhost:8000/fhir/metadata                                            # auth-free
curl -H "X-API-Key: phealth_dev_ingest_key" \
     localhost:8000/fhir/Patient/$PID/\$everything

# Conditional read -- repeat with the ETag for a 304:
ETAG=$(curl -sI -H "X-API-Key: phealth_dev_ingest_key" \
            localhost:8000/api/v1/patients/$PID/chart | awk '/^etag:/ {print $2}' | tr -d '\r')
curl -sI -H "X-API-Key: phealth_dev_ingest_key" -H "If-None-Match: $ETAG" \
     localhost:8000/api/v1/patients/$PID/chart    # → HTTP/1.1 304 Not Modified
```

Postgres is published on host port **5433** (not 5432, to avoid colliding with
a local Postgres). To run the app or Alembic directly on the host instead of
in the container, the `.env` `DATABASE_URL` already points at `localhost:5433`.

Interactive docs at <http://localhost:8000/docs>; capability discovery at
<http://localhost:8000/fhir/metadata>.

**Tests:** `pytest` — 127 tests, `ruff check` and `mypy app/` clean. Coverage
includes golden tests for the section detector and scale extractor, the
provenance round-trip across every endpoint that returns observations, FHIR
Bundle validation on every `/fhir/*` response, the ETag/304 contract, the
RFC 7807 / OperationOutcome envelopes, cursor pagination invariants, and the
audit-on-read invariant.

---

## Phase 3 — Clinical decision endpoints

Two new POST endpoints layered on the Phase 2 substrate:

- **`POST /api/v1/patients/{id}/asam-loc`** — ASAM 4th-edition Level-of-Care
  recommendation with per-dimension cited rationale. Marcus → **Level 3.7**
  (Medically Monitored Intensive Inpatient), non-COE, non-BIO.
- **`POST /api/v1/patients/{id}/tjc-audit`** — Joint Commission compliance
  audit over a 13-EP behavioral-health catalog. Marcus → **7 satisfied / 5
  gap / 1 n/a**, all 5 planted gaps (G1–G5) surfaced.

The architectural commitment: **the recommendation is deterministic Python;
the rationale is LLM-narrated.** ASAM 4th-edition Chapter 10's Level-of-Care
Determination Rules (pp. 279–281) are encoded as a pure-Python decision tree
in `app/clinical/asam/level_decision.py`. The TJC audit is 13 Python
predicates in `app/clinical/tjc/audit_functions.py`. Claude Sonnet 4.6
writes the human-readable rationale around the engine's already-computed
decision; it cannot alter the level or the EP-status verdicts because they
aren't in its tool-use schema.

### Why not LLM-only?

ASAM publicly states: *"Inputting ASAM Criteria and other ASAM intellectual
property into artificial intelligence is strictly prohibited."* This design
respects that — Chapter 10 rules live in code; the LLM only sees the
project's derived clinical findings + the engine's outputs, never any
proprietary ASAM rubric text. Same with the CAMBHC manual on the TJC side:
the EP catalog (`app/clinical/tjc/ep_catalog.py`) is paraphrased from public
R3 reports, not copied. The LLM is constrained to surveyor-RFI register and
explicit absence-language for negative findings.

Beyond IP: deterministic levels are *reproducible* — reviewers who re-run
the demo get the same recommendation. The cache layer (keyed by
`(patient_id, evidence_hash, model_version)`) makes the response **byte-
identical** on repeat POSTs even though Claude itself is not bit-stable.

### Endpoint contract

```
POST /api/v1/patients/{patient_id}/asam-loc        →  AsamAssessmentRead
  Body:           { "force_recompute": false }
  201 Created     fresh compute
  200 OK          cache hit; body and ETag identical to the original 201
  304 Not Modified  on If-None-Match for the GET-by-id
  422             patient has no AsamEvidence (RFC 7807)
  503             Claude rate-limited or breaker open (RFC 7807 + Retry-After)
  201 + degraded  Claude unreachable; engine's recommendation preserved
                  (headers: x-rationale-status: unavailable, x-rule-engine-only: true)
  502             malformed structured output (RFC 7807)
  Headers:        ETag: W/"{evidence_hash}-{model_version}"
                  Location: /api/v1/asam-assessments/{id}

GET /api/v1/asam-assessments/{id}                  →  AsamAssessmentRead | 304
POST /api/v1/patients/{patient_id}/tjc-audit       →  TjcAuditRead   (same semantics)
GET /api/v1/tjc-audits/{id}                        →  TjcAuditRead | 304
```

FHIR mirrors:
- `GET /fhir/ClinicalImpression?subject=Patient/{id}` — one per ASAM assessment.
- `GET /fhir/DetectedIssue?subject=Patient/{id}` — one per TJC gap finding.
- Both round-trip through `fhir.resources.R4B`.

### Cost, latency, cache

| Path                      | Cost            | Latency  |
|---------------------------|-----------------|----------|
| Fresh `/asam-loc` POST    | ~$0.06          | 8–12 s   |
| Fresh `/tjc-audit` POST   | ~$0.08          | 15–25 s  |
| Cached re-POST (same evidence_hash + model) | $0 | < 20 ms |
| GET by id + If-None-Match | $0              | < 10 ms (304) |
| `force_recompute: true`   | full cost again | full latency |

Every Claude call writes one `LlmInvocation` row capturing tokens / latency /
citation-validation outcome — the cost/reliability ground truth for the demo
recording.

### Quick start

```bash
# 1. Add ANTHROPIC_API_KEY to .env (obtain at https://console.anthropic.com).
# 2. (Optional) Smoke-test the SDK + Citations API:
python scripts/smoke_anthropic.py

# 3. Hit the endpoints (X-API-Key header is required):
PID=$(curl -s -H "X-API-Key: phealth_dev_ingest_key" \
        localhost:8000/api/v1/patients | jq -r '.[0].id')

curl -X POST localhost:8000/api/v1/patients/$PID/asam-loc \
  -H "X-API-Key: phealth_dev_ingest_key" \
  -H "Content-Type: application/json" -d '{}' | jq .recommendation

curl -X POST localhost:8000/api/v1/patients/$PID/tjc-audit \
  -H "X-API-Key: phealth_dev_ingest_key" \
  -H "Content-Type: application/json" -d '{}' | jq .summary
```

Sample responses (regeneratable via `python scripts/generate_sample_json.py`):
- [`examples/marcus_reyes_asam_admission.json`](examples/marcus_reyes_asam_admission.json) — Level 3.7
- [`examples/marcus_reyes_tjc.json`](examples/marcus_reyes_tjc.json) — 5 planted gaps + 7 satisfied EPs

Design rationale: [`docs/adr/003-llm-narration.md`](docs/adr/003-llm-narration.md).
Full design: [`documents/phase_3_PRD.md`](documents/phase_3_PRD.md).

---

## What makes this not a wrapper

- **Char-offset provenance on every observation in every response.** Asserted
  on the wire by `tests/test_provenance.py` against `/chart`,
  `/observations`, `/fhir/Observation`, `$everything`, and `/fhir/Provenance`.
- **Trinary completeness** — `{found, looked_but_missing, not_assessed}` on
  every BPS section, plus a `completeness_score` ratio in the chart meta.
  Distinguishes "the chart didn't fill a section that was asked" from "the
  chart didn't have that section in its template" — the difference between a
  quality gap and irrelevant absence.
- **Two-stage storage: `raw_text` + sectioned JSONB.** Section parsing
  happens once, at ingest. Rule-based audits run on the precise sections; LLM
  reasoning can run on the raw text — neither re-parses at query time.
- **One unified `ClinicalDocument` table.** BPS intake and all three
  progress-note formats live in one table; the format-specific structure is
  preserved in the `sections` JSONB.
- **FHIR-R4-shaped, not FHIR-served.** Every relational row carries a sibling
  `fhir_json` column populated at ingest. The `/fhir/*` endpoints are thin
  projections, not a parallel pipeline.
- **Pre-computed evidence tables.** `AsamEvidence` (all six ASAM 4th-edition
  dimensions) and `TjcCoverage` (the EP matrix) are built at ingest, so Phase
  3's endpoints become `SELECT`s rather than full-chart inference.
- **Event-sourced `AuditEvent` log** with audit-on-read enabled — every
  successful read writes a row, off the response critical path via
  `BackgroundTasks`.
- **Idempotent ingest** (`sha256(raw_text)`) and a generated `tsvector` FTS
  column on every document — the hooks Phase 3 retrieval needs.

---

## Repository layout

```
app/
  ingest/      pipeline: pdf_parser, vcard_parser, section_detector,
               scale_extractor, entity_tagger, asam_evidence_index,
               tjc_coverage_matrix, provenance, simplepractice_zip
  fhir/        mappers.py — internal rows → FHIR R4 resources + Bundles
               (Patient, Encounter, DocumentReference, ClinicalImpression,
                Observation, Provenance, CapabilityStatement)
  api/         routers + cross-cutting: ingest, patients, notes, fhir,
               errors (RFC 7807 + OperationOutcome dispatcher),
               etag (xxh64 middleware), pagination (cursor + Bundle.link),
               completeness (trinary classifier),
               schemas / chart_schemas / fhir_schemas
  db/          SQLModel schema + session
  core/        config (pydantic-settings) + API-key security
  synthetic/   persona.yaml (source of truth) + the chart-text generator
data/
  synthetic_export/   the committed SimplePractice export (fully synthetic)
  seed/               ASAM dimension + TJC EP catalogs (reviewable YAML)
documents/   the assessment brief, research playbook, PRDs, implementation plans
examples/    marcus_reyes_chart.json       (live /chart response)
             marcus_reyes_everything.json  (live $everything Bundle)
             marcus_reyes.json             (Phase 1 sample — superseded)
tests/       127 tests + the face-validity checklist
```

---

## Production prerequisites (out of MVP scope, flagged honestly)

- **BAA with SimplePractice.** Any production data flow through the Data
  Export would require a Business Associate Agreement.
- **Deletion path for real exports.** The committed export is *fully
  synthetic*, so it lives in git. A real export would be gitignored with a
  documented deletion procedure.
- **Secrets.** `.env` is gitignored; `.env.example` carries dev-only
  defaults; `pydantic-settings` fails fast on a missing required value.
- **LLM cost caps.** Claude is the single LLM provider. Section detection is
  regex-first; the LLM fallback is bounded by `MAX_LLM_CALLS_PER_INGEST` and
  never fires for the well-formed synthetic chart. Phase 3 endpoints cache
  every assessment by `(patient_id, evidence_hash, model_version)`, so
  reviewer re-runs cost zero after the first.
- **HIPAA posture.** The dev environment is treated as if it held PHI even
  though the data is synthetic — Postgres on a private Docker network, no
  secrets in git, audit-on-read enabled.
- **Auth.** `X-API-Key` is intentional demo-grade auth. Production needs
  OAuth 2 / SMART-on-FHIR; the dependency lives in `app/core/security.py`
  and is a single swap.

## Plan B

If SimplePractice access became unavailable, the documented fallback is
**OpenEMR in Docker** with equivalent custom note templates — the same
ingestion architecture over a different EMR's export.

## Phase boundaries

- **Phase 1** delivered the synthetic chart, the FHIR-shaped ingestion
  substrate, and the read API (`documents/phase_1_PRD.md`).
- **Phase 2** delivered the dual-surface read API, the consolidated
  `/chart` endpoint, the strict `/fhir/*` namespace, the custom
  Provenance extension, audit-on-read, ETag, and unified errors
  (`documents/phase_2_PRD.md`).
- **Phase 3** delivered `POST /api/v1/patients/{id}/asam-loc` +
  `POST /api/v1/patients/{id}/tjc-audit` (cached by `evidence_hash`),
  FHIR `ClinicalImpression` + `DetectedIssue` mirrors, and the
  deterministic-core-plus-LLM-narration architecture
  (`documents/phase_3_PRD.md` + `docs/adr/003-llm-narration.md`).

---

## Submission

Per the brief: the code repository plus the inline + attached sample
JSON files goes to both `kyle@perspectiveshealth.ai` and
`eshan@perspectiveshealth.ai`.

Sample artifacts:
- [`examples/marcus_reyes_chart.json`](examples/marcus_reyes_chart.json) — the canonical Task 2 `/chart` response.
- [`examples/marcus_reyes_everything.json`](examples/marcus_reyes_everything.json) — the FHIR `$everything` Bundle.
- [`examples/marcus_reyes_asam_admission.json`](examples/marcus_reyes_asam_admission.json) — Task 3 ASAM Level 3.7.
- [`examples/marcus_reyes_tjc.json`](examples/marcus_reyes_tjc.json) — Task 3 TJC audit (5 gaps + 7 satisfied).
