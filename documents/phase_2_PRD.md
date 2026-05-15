# Phase 2 — PRD: Dual-Surface Clinical Read API + Provenance

**Project:** Perspectives Health Intern Technical Assessment
**Phase:** 2 of 3 (corresponds to Task 2 in the brief, layered on the Phase 1 substrate)
**Status:** Approved
**Owner:** Adrian
**Date:** 2026-05-15
**Last reviewed:** 2026-05-15
**Reference:** `documents/phase_2.md` (research playbook), `documents/phase_1_PRD.md` (the substrate this builds on), `documents/Technical task 5-8.pdf` (assessment brief)

---

## 1. Context

Phase 1 shipped the FHIR-shaped substrate: a SimplePractice export ingested into a unified `ClinicalDocument` table, with `ExtractedObservation` rows carrying char-offset provenance, pre-computed `AsamEvidence` / `TjcCoverage`, and `fhir_json` mirrors on every relational row. The brief's Task 2 asks for *"a Python-based API that programmatically extracts information from your SimplePractice sandbox … a structured JSON object containing patient demographic data, the full text of the initial assessment, timeline-ordered progress notes with their respective metadata (date, author, type)."*

Phase 1 partly fulfilled this — `GET /patients/{id}`, `/intake`, `/timeline`, `/observations`, `/asam-evidence`, `/tjc-coverage`, `/fhir-bundle` already exist. **Phase 2 reorganizes them under a `/api/v1/` namespace, adds the canonical consolidated `GET /api/v1/patients/{id}/chart` endpoint the brief asks for, splits the single `/fhir-bundle` into the proper `/fhir/*` namespace (Patient, DocumentReference, Observation, Provenance, `Patient/$everything`, `metadata`), and bakes in the differentiators**: char-offset provenance on every observation, trinary completeness scoring, audit-on-read, ETag conditional reads, RFC 7807 / OperationOutcome unified errors, FHIR `Bundle.searchset` pagination, and a custom Provenance extension for character-range citations.

This is a **response-shaping phase**, not a parsing phase. No LLM at request time; no re-extraction. Endpoints are thin SQL views over Phase 1's already-ingested rows.

## 2. Goals

1. Ship the canonical Task 2 deliverable: **`GET /api/v1/patients/{id}/chart`** — one endpoint returning the brief's shape (patient + intake + timeline) plus the extraction surplus (observations, scales with provenance, ASAM summary, TJC coverage, completeness score).
2. Expose a strict **`/fhir/*` namespace** for FHIR R4 conformance: `Patient`, `Patient/$everything` (Synthea-shaped Bundle), `DocumentReference` / `Observation` / `Provenance` search with `Bundle.searchset` + cursor pagination, `CapabilityStatement` at `/fhir/metadata`.
3. **Bake char-offset provenance into every observation** in every response, and prove the round-trip (`raw_text[char_start:char_end] == snippet`) with a dedicated test that runs across all observation-bearing endpoints.
4. Add **audit-on-read**, **ETag / If-None-Match → 304**, and a **unified error handler** (RFC 7807 on `/api/v1/*`, OperationOutcome on `/fhir/*`).
5. Ship the Task 2 sample JSON (`examples/marcus_reyes_chart.json`) and a refreshed README that leads with the "not a wrapper" thesis.

## 3. Non-Goals (Phase 2)

- No Task 3 reasoning endpoints (`POST /patients/{id}/asam-loc`, `POST /patients/{id}/tjc-audit`) — those are Phase 3.
- No real async FHIR Bulk Data Kick-Off (the 202 → polling → manifest dance); a synchronous NDJSON stream is the chosen scope-control (see Open Question Q1).
- No SMART on FHIR / OAuth flow — `X-API-Key` continues; a scope-check stub is the documented migration path.
- No LLM at request time. All inference is in Phase 1 ingestion; Phase 2 reads pre-computed rows.
- No re-ingestion or re-extraction endpoints. Reads only.
- No multi-patient discovery beyond a simple `GET /api/v1/patients` list; no multi-tenant.
- No UI (`/docs` Swagger continues to suffice).

## 4. Users / Personas

- **Primary:** the assessment reviewers (Kyle Hyun Woo Jung, CTO; Eshan Dosani, co-founder). They will (a) read `examples/marcus_reyes_chart.json`, (b) hit `/docs`, (c) run `curl /api/v1/patients/{id}/chart` and `curl /fhir/Patient/{id}/$everything`, (d) run `pytest` to see the provenance round-trip pass.
- **Secondary (informs design):** a clinical informaticist who would extend this stack to additional patients / EMR sources / SMART OAuth. The dual-surface architecture must read as a foundation for that.

## 5. Functional Requirements

### 5.1 Dual surface

- **`/api/v1/*`** — ergonomic snake_case envelopes. Collections return `{data, meta, links}`; singletons return a bare object. `meta` carries `extraction_version`, `generated_at`, `completeness_score`, `etag`, and pre-allocated nullable `audit_id` / `compliance_check_id` slots (for Phase 3 backward compatibility).
- **`/fhir/*`** — strict FHIR R4. Bare resource for singletons; `Bundle.searchset` for collections (no envelope wrapper).

### 5.2 Canonical endpoint — `GET /api/v1/patients/{id}/chart`

Returns one JSON object covering everything the brief asks for plus the extraction surplus:

```
{patient, intake, timeline, observations, scales, asam_summary, tjc_coverage, meta}
```

The exact shape is in §6.1. Every `observations[]` and `scales[]` entry carries a `provenance: {document_id, char_start, char_end, snippet}`. `intake.sections` uses the trinary status (§5.5). `meta.completeness_score` is the computed-field ratio.

### 5.3 Endpoint inventory

The full surface this phase ships. "Audit" = a row is written to `AuditEvent` (action=`read`) via BackgroundTasks after a successful response.

| Method | Path | Response | Pagination | Audit |
|--------|------|----------|------------|-------|
| GET | `/health` | `{status, db, version}` | — | no |
| GET | `/api/v1/capabilities` | custom self-describe | — | no |
| GET | `/fhir/metadata` | `CapabilityStatement` | — | no |
| GET | `/api/v1/patients` | `{data, meta, links}` | cursor | yes |
| GET | `/api/v1/patients/{id}` | bare object | — | yes |
| **GET** | **`/api/v1/patients/{id}/chart`** | **canonical (§6.1)** | — | **yes** |
| GET | `/api/v1/patients/{id}/intake` | bare object | — | yes |
| GET | `/api/v1/patients/{id}/timeline` | `{data, meta, links}` | cursor | yes |
| GET | `/api/v1/patients/{id}/documents` | `{data, meta, links}` | cursor | yes |
| GET | `/api/v1/patients/{id}/documents/{doc_id}` | bare object | — | yes |
| GET | `/api/v1/patients/{id}/observations` | `{data, meta, links}` | cursor | yes |
| GET | `/api/v1/patients/{id}/asam-evidence` | bare object | — | yes |
| GET | `/api/v1/patients/{id}/tjc-coverage` | bare object | — | yes |
| GET | `/fhir/Patient/{id}` | FHIR `Patient` | — | yes |
| GET | `/fhir/Patient/{id}/$everything` | `Bundle.searchset` | next | yes |
| GET | `/fhir/DocumentReference` (+`/{id}`) | `Bundle` / resource | next / — | yes |
| GET | `/fhir/Observation` | `Bundle` | next | yes |
| GET | `/fhir/Provenance?target={ref}` | `Bundle` | next | yes |

### 5.4 Provenance contract

Every observation-like value in every response that cites extracted data carries:

```
provenance: { document_id, char_start, char_end, snippet }
```

with the invariant `raw_text(document_id)[char_start:char_end] == snippet`. Phase 1 already enforces this at the row level (`tests/test_provenance.py`); Phase 2 extends the guarantee to the response surface — a dedicated test asserts the round-trip on every observation served by every endpoint.

FHIR `Provenance` resources use a **custom extension** for the char-range citation, since R4 has no standard text-anchor selector:

```
url:  http://perspectiveshealth.ai/fhir/StructureDefinition/text-position-selector
parts: {url:"start" valueInteger, url:"end" valueInteger, url:"exact" valueString}
```

attached under `entity[0].extension[]`. Documented in the README as a deliberate IG-level choice.

### 5.5 Trinary completeness

Each BPS section and scale carries `status ∈ {"found", "looked_but_missing", "not_assessed"}`. Definitions:

- **`found`** — the section/scale text is present and was extracted with a non-null provenance span.
- **`looked_but_missing`** — the section exists in the BPS template but its body is empty (the clinician didn't fill it in). Or the scale's verbatim string was searched-for but not matched.
- **`not_assessed`** — the section/scale is not part of the document type or template at all (e.g. COWS for a non-opioid case).

`meta.completeness_score = |{found}| / (|{found}| + |{looked_but_missing}|)` ∈ `[0, 1]`. `not_assessed` is excluded from both numerator and denominator (you can't penalize a clinician for not collecting data their workflow doesn't ask for).

### 5.6 Audit-on-read

Every successful read (`/api/v1/*` and `/fhir/*`) writes one `AuditEvent` row with `action="read"`, `actor=<api-key principal>`, `resource_type=<path>`, `payload={method, status, query_string}`, via a FastAPI `BackgroundTasks` dependency that runs after the response is sent. Failures (4xx/5xx) are NOT audited (only successful disclosures).

### 5.7 Conditional reads (ETag / If-None-Match)

Every `200 OK` response includes a weak ETag `W/"<8-hex>"` derived from `xxhash.xxh64(canonical_json(payload))`. Clients sending `If-None-Match: <etag>` receive `304 Not Modified` with no body. Per FHIR `http.html`, clients SHALL accept either 304 or full content.

### 5.8 Error format

- **`/api/v1/*` errors:** RFC 7807 `application/problem+json` — `{type, title, status, detail, instance}`.
- **`/fhir/*` errors:** FHIR `OperationOutcome` — `{resourceType:"OperationOutcome", issue:[{severity, code, diagnostics, details}]}`.

A single FastAPI exception handler branches on path prefix.

**HTTP code conventions:**
- An empty collection (e.g. a patient with no progress notes) returns **`200`** with `data:[]` — NOT 404. 404 is reserved for "the entity in the path does not exist."
- A partial extraction failure surfaces as `status:"extraction_failed"` on the affected observation — NEVER a 500.

### 5.9 Pagination

Cursor-based on collections. Query params: `?cursor=<opaque-base64>&limit=<n>`. Default limits documented in `CapabilityStatement.rest.resource.searchParam`: **50** for timeline, **20** for documents. Max 100.

On `/api/v1/*`, the response carries `meta.next_cursor` (or `null`) and `links.next` (or absent). On `/fhir/*`, the `Bundle.link[]` array carries `relation="self"` and (when more pages exist) `relation="next"` with the full opaque next-page URL. Only `next` is implemented; `prev/first/last` are deliberately not (Azure-style precedent).

### 5.10 Timestamps

ISO 8601 with **proper offset**. Phase 1 shipped a bug where naive CT chart times were serialized with a `Z` suffix (claiming UTC); Phase 2 fixes this at the boundary (`_fhir_datetime` → attach `America/Chicago`) — see M0 below.

### 5.11 Auth & compression

- **Auth:** `X-API-Key` header required on **every** `/api/v1/*` and `/fhir/*` endpoint (Phase 1 only required it on `/ingest/*`). A SMART-on-FHIR scope-check dependency is stubbed so the OAuth migration is a swap, not a rewrite.
- **Compression:** gzip middleware enabled on the FastAPI app.

## 6. Detailed Requirements

### 6.1 The canonical `/chart` response shape

```json
{
  "meta": {
    "generated_at": "2026-05-15T14:22:08-05:00",
    "extraction_version": "phase1-v1.2.0",
    "completeness_score": 0.86,
    "etag": "W/\"3f9a2c\"",
    "audit_id": null,
    "compliance_check_id": null
  },
  "patient": {
    "id": "<uuid>",
    "identifiers": [{"system": "simplepractice", "value": "106915126"}],
    "name": {"given": ["Marcus"], "family": "Reyes"},
    "birth_date": "1991-04-11",
    "gender": "unknown",
    "address": {...},
    "phone": null,
    "preferred_language": "en"
  },
  "intake": {
    "document_id": "<uuid>",
    "document_type": "biopsychosocial",
    "encounter_date": "2026-05-04T13:30:00-05:00",
    "author": {"name": "Adrian Dai", "npi": null},
    "loinc_type": {"system": "http://loinc.org", "code": "11488-4", "display": "Consultation Note"},
    "full_text": "...",
    "sections": {
      "<section_key>": {"status": "found",              "text": "...", "provenance": {...}},
      "<section_key>": {"status": "looked_but_missing", "text": null,  "provenance": null},
      "<section_key>": {"status": "not_assessed",       "text": null,  "provenance": null}
    },
    "scales": [
      {"instrument":"PHQ-9","score":18,"severity":"moderately severe","status":"found","provenance":{...}}
    ]
  },
  "timeline": [
    {"document_id":"<uuid>","date":"2026-05-06T11:00:00-05:00","type":"progress_note","format":"SOAP",
     "loinc_type":{"system":"http://loinc.org","code":"11506-3","display":"Progress Note"},
     "author":{"name":"Dr. Aisha Patel, MD","npi":null},
     "sections":{"subjective":"...","objective":"...","assessment":"...","plan":"..."},
     "full_text":"..."}
  ],
  "observations": [
    {"id":"<uuid>","code":{"system":"http://loinc.org","code":"44261-6","display":"PHQ-9 total"},
     "value_quantity":18.0,"effective_date":"2026-05-04",
     "provenance":{"document_id":"<uuid>","char_start":12397,"char_end":12587,"snippet":"PHQ-9 administered ..."}}
  ],
  "asam_summary": {"dim_1":"moderate-to-severe","dim_2":"moderate","dim_3":"moderate","dim_4":"moderate-to-high","dim_5":"high","dim_6":"moderate"},
  "tjc_coverage": {"covered_eps": 3, "total_eps": 8, "gap_eps": ["CTS.03.01.09","CTS.03.01.03","NPSG.15.01.01","R3-25","RC.01.02.01"]}
}
```

### 6.2 LOINC document-type bindings

| `ClinicalDocument.document_type` | LOINC `code` | `display` |
|-----------------------------------|--------------|-----------|
| `bps_intake` | `11488-4` | Consultation Note |
| `soap` / `dap` / `dsap` | `11506-3` | Progress Note |

These bind on the `DocumentReference.type` and `Composition.type` resources, and on the `loinc_type` field in the `/api/v1/*` response shapes.

### 6.3 BPS domain coverage (trinary keys)

Each BPS section in `intake.sections` maps to one of the eight domains the playbook §2 enumerates: Identifying, Presenting, Biological, Psychological, Social (incl. SDOH per US Core SDOH IG, LOINC grouping `LG41762-2`), Risk, Diagnostic, Formulation. The Phase 1 BPS template has 21 sections that already cover these domains; Phase 2 surfaces them with the trinary `status` field per §5.5.

### 6.4 Carry-over fixes from Phase 1

These are not new requirements but Phase-1 issues this phase folds in:

- **Timezone bug:** `_fhir_datetime` currently labels naive (CT) datetimes as UTC. Fix: attach `America/Chicago`. The boundary is the only place this needs to change — Postgres columns stay naive (single-clinic MVP).
- **`GET /api/v1/patients` list endpoint:** Phase 1 has no list endpoint; the patient id is only discoverable via the ingest log. Phase 2 adds the list.
- **X-API-Key on read endpoints:** Phase 1's reads were unauthenticated; Phase 2 makes the key required on all paths (still demo-grade, but consistent).
- **Auth tests:** Phase 2 adds automated tests for missing/wrong X-API-Key.

## 7. Non-Functional Requirements

- **Stack:** same as Phase 1 (FastAPI, SQLModel, Pydantic v2, fhir.resources.R4B, pgvector). New deps: `xxhash` (for ETag). Optional: `syrupy` (golden snapshots), `schemathesis` (CI fuzzing) — see Open Questions.
- **Determinism:** zero LLM at request time. Task 2 responses are deterministic functions of the row store.
- **Performance:** p95 < 500ms for `/chart` on the seeded chart; p95 < 250ms for the simpler single-resource endpoints.
- **Pydantic discipline:** continue separating `db/` (SQLModel `table=True`) from `schemas/` (Pydantic response models). FHIR-shaped schemas use `model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)`. JSONB columns flowing through `/fhir/*` are validated via `fhir.resources.R4B` (not passed through raw).
- **Observability:** every read writes an `AuditEvent`. `INFO` logs include the path + status; errors include the exception type and the unified-error response body.
- **Compression:** gzip middleware enabled.
- **OpenAPI:** every operation has an `operation_id`, a description, and at least one example. `/docs` is the primary discoverability surface for reviewers.
- **Coverage target:** 90% line, **100%** on path operations, **100%** on the unified error handler.

## 8. Success Metrics

| # | Metric | Target |
|---|---|---|
| M1 | Every observation in every `/api/v1/*` or `/fhir/*` response carries non-null `(char_start, char_end)` provenance | 100% |
| M2 | Provenance round-trip — `raw_text[char_start:char_end] == snippet` — across every response endpoint | 100% |
| M3 | Every `/fhir/*` response validates against `fhir.resources.R4B` | 100% |
| M4 | Every `/api/v1/*` error response is valid RFC 7807 (Content-Type `application/problem+json`, fields `{type, title, status, detail, instance}`) | 100% |
| M5 | Every `/fhir/*` error response is a valid `OperationOutcome` | 100% |
| M6 | Every successful GET returns an `ETag`; a subsequent request with `If-None-Match: <etag>` yields `304` | 100% |
| M7 | Every successful read writes one `AuditEvent` row with `action="read"` | 100% |
| M8 | `GET /api/v1/patients/{id}/chart` output matches the canonical shape in §6.1 | exact |
| M9 | `examples/marcus_reyes_chart.json` (refreshed) is the live `/chart` response and re-validates against `Bundle` (for the embedded FHIR) and against the round-trip invariant | refreshed |
| M10 | README leads with the "not a wrapper" thesis + the provenance round-trip line, and a reviewer can read it end-to-end in <10 minutes | qualitative |
| M11 | `/fhir/metadata` `CapabilityStatement` validates and accurately describes the served surface | passes |

## 9. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| FHIR Bundle / OperationOutcome edge cases drift between code and `fhir.resources` | Med | Med | Validate every `/fhir/*` response in tests via `fhir.resources.R4B.<Resource>.model_validate(body)`. CI fails fast. |
| Schemathesis surfaces many issues across the dual surface and balloons scope | Med | Med | Gate as Open Question Q3. Run it manually if not in CI; fix only crashes/contract violations, not stylistic issues. |
| Audit-on-read writes pile up and slow tests | Low | Low | BackgroundTasks runs after response; tests use the rolled-back `db_session` so audit rows never persist. |
| Pagination cursor opacity has bugs (mis-decoded base64, wrong `last_id`) | Med | Med | Cursor encoder/decoder is one helper module with its own unit tests. |
| The custom Provenance extension URL gets challenged ("this isn't FHIR") | Low | Low | Documented explicitly in README + tests; R4 has no standard char-anchor selector, so a custom extension is the only path. |
| Refactoring Phase 1's URLs to `/api/v1` breaks the existing tests | High | Low | Update `test_read_api.py` URLs in M1; mechanical change. |
| LLM-fallback path in `section_detector` never tested → latent bug surfaces in Task 3 | Low | Med | Phase 2 doesn't change it. Note as a Phase 3 prerequisite; not in scope here. |

## 10. Open Questions

*All Phase 2 draft-stage questions resolved 2026-05-15 during PRD review:*

- **Q1 (resolved):** Ship the synchronous NDJSON `$export` stub as M13 (optional milestone) — ~30 lines, demonstrates Bulk Data IG familiarity without the async polling dance.
- **Q2 (resolved):** Descope `_elements` partial response on `/fhir/*` — not a brief requirement; conformance polish that doesn't move the submission needle.
- **Q3 (resolved):** Run schemathesis manually once during M11, fix anything it surfaces, and paste the report into the README. CI integration is Phase 3+ polish.
- **Q4 (resolved):** Require `X-API-Key` on every `/api/v1/*` and `/fhir/*` read. Codified in §5.11 and applied in M1.

No questions remain open for Phase 2. New questions surfaced during implementation should be tracked here or in `documents/phase_2.md`.

## 11. Out of Scope (deferred to Phase 3+)

- **Phase 3 — clinical-intelligence endpoints:**
  - `POST /api/v1/patients/{id}/asam-loc` — ASAM Level-of-Care prediction with LLM rationale over `AsamEvidence`.
  - `POST /api/v1/patients/{id}/tjc-audit` — TJC compliance audit with LLM rationale over `TjcCoverage`.
- **Full async FHIR Bulk Data Kick-Off** (202 → polling → manifest per `hl7.org/fhir/uv/bulkdata`).
- **SMART on FHIR OAuth flow** (scope grammar parser stubbed for migration path; OAuth itself out).
- **Multi-patient discovery beyond `GET /api/v1/patients`** (search by name, demographics, etc.).
- **Real-time webhook notifications** (Metriport-style async delivery).
- **Multi-tenant** (the schema and code assume a single practice).
- **A consumer UI.** `/docs` remains the discovery surface.

## 12. Deliverables & Submission

This phase produces the artifacts the brief asks for under "Submission Instructions":

- The Git repository URL — same repo, with M0–M12 commits visible.
- **`examples/marcus_reyes_chart.json`** — the canonical `/chart` response for Marcus, regenerated from the live endpoint. (Optionally also `examples/marcus_reyes_bundle.json` for the FHIR `Patient/$everything` payload, paired with the `/chart` as a dual-format demonstration.)
- Updated README (M12) leading with the "not a wrapper" thesis, an architecture diagram (Phase 1 ingestion → Phase 2 read surface), the canonical `/chart` shape inline, and the provenance round-trip line.

Submission contract — per the brief, page 3:

- Email both `kyle@perspectiveshealth.ai` (Kyle Hyun Woo Jung, CTO) and `eshan@perspectiveshealth.ai` (Eshan Dosani, co-founder).
- Paste the `/chart` shape (truncated to ~80 lines) inline in the email; attach the FHIR Bundle as a separate file. Inline beats attached for a tired reader.

---

## Appendix A — Glossary

- **Trinary completeness** — the `{found, looked_but_missing, not_assessed}` enum on every section/scale, plus the `completeness_score` ratio.
- **Provenance round-trip** — the invariant `raw_text[char_start:char_end] == snippet` for every cited observation, asserted by `tests/test_provenance.py`.
- **Dual-surface** — `/api/v1/*` (ergonomic, custom envelopes) + `/fhir/*` (strict FHIR R4) over the same row store.
- **RFC 7807** — Problem Details for HTTP APIs (`application/problem+json`). Used on `/api/v1/*` errors.
- **OperationOutcome** — the FHIR error resource. Used on `/fhir/*` errors.
- **Bundle.searchset** — a FHIR Bundle returned from a search; carries `total`, `entry[]`, and `link[]` with `relation: self|next`.
- **`$everything`** — the FHIR `Patient` operation that returns a Bundle of a patient's compartment.
- **US Core SDOH IG** — `hl7.org/fhir/us/core/sdoh.html`; LOINC grouping code `LG41762-2`.
- **PH-offset extension** — our custom FHIR extension at `http://perspectiveshealth.ai/fhir/StructureDefinition/text-position-selector`, carrying `valueInteger start`, `valueInteger end`, `valueString exact` for character-range citations.
