# Phase 2 — Implementation Plan

**Companion to:** `documents/phase_2_PRD.md`
**Source playbook:** `documents/phase_2.md`
**Date:** 2026-05-15
**Total estimated effort:** ~28–32 focused hours

This plan sequences Phase 2 into 13 milestones with file-level scope and acceptance gates. Each milestone is independently runnable — at the end of each, `pytest`, `ruff`, and `mypy` should be green. Where Phase 2 changes a Phase 1 contract (URL prefix moves, error shapes), the test files are updated in the same milestone so the suite never goes red.

---

## 1. Architecture at a glance

```
                ┌──────────────────────────────────────────────────────────┐
                │                  Phase 1 substrate (unchanged)            │
                │                                                          │
                │  Patient · Encounter · ClinicalDocument                   │
                │  (raw_text + sections JSONB + fhir_json + embedding + fts) │
                │  ExtractedObservation (char-offset provenance)            │
                │  AsamEvidence · TjcCoverage · AuditEvent                  │
                └────────────────────────────┬─────────────────────────────┘
                                             │
        ┌────────────────────────────────────┴────────────────────────────────┐
        │                                                                    │
        ▼                                                                    ▼
┌──────────────────────────────────┐                  ┌─────────────────────────────────────┐
│  /api/v1/*  (snake_case, custom)  │                  │  /fhir/*  (strict FHIR R4)           │
│                                  │                  │                                     │
│  /patients/{id}/chart   ← brief  │                  │  /Patient/{id}                       │
│  /patients/{id}/intake           │                  │  /Patient/{id}/$everything (Bundle)  │
│  /patients/{id}/timeline         │                  │  /DocumentReference  (Bundle search) │
│  /patients/{id}/observations     │                  │  /Observation        (Bundle search) │
│  /patients/{id}/documents        │                  │  /Provenance         (Bundle search) │
│  /patients/{id}/asam-evidence    │                  │  /metadata           (CapabilityStmt)│
│  /patients/{id}/tjc-coverage     │                  │  /Patient/{id}/$export  (NDJSON, opt)│
│  /patients                       │                  │                                     │
│  /capabilities                   │                  │  errors: OperationOutcome            │
│  errors: RFC 7807                │                  │  pagination: Bundle.link[next]       │
│  pagination: {data, meta, links} │                  │                                     │
└──────────────────────────────────┘                  └─────────────────────────────────────┘
                │                                                                    │
                └──────────────────────────┬─────────────────────────────────────────┘
                                           │
                                           ▼
                       cross-cutting middleware / dependencies
                       • X-API-Key auth (every request)
                       • audit-on-read (BackgroundTasks → AuditEvent)
                       • ETag + If-None-Match → 304
                       • gzip
                       • unified exception handler (branches on path prefix)
```

Both surfaces are thin views over the same row store. No new tables, no new alembic migrations.

---

## 2. Milestones

### M0 — Phase 1 carry-over fixes (1.5 h)

Fold the bug-hunt findings from the end of Phase 1 into Phase 2's foundation so the new code lands on a clean baseline.

**Deliverables:**
- `app/fhir/mappers.py::_fhir_datetime` — attach `America/Chicago`, not UTC. Naive chart timestamps are CT.
- `app/api/patients.py::list_patients` — new `GET /patients` (will move to `/api/v1/patients` in M1) returning `[{id, given_name, family_name, birth_date}]`.
- `tests/test_security.py` — verify `X-API-Key` rejects missing/wrong on `POST /ingest/*` (now; on every endpoint after M1).

**Acceptance:** ruff + mypy clean; pytest green; the `Encounter.period.start` in `examples/marcus_reyes.json` (regenerated) carries `-05:00`, not `Z`.

---

### M1 — `/api/v1/` namespace + auth on reads (2 h)

Move every Phase-1 patient-scoped endpoint under `/api/v1/`. Apply `X-API-Key` to every route.

**Deliverables:**
- `app/api/patients.py`, `app/api/notes.py` — change `APIRouter(prefix="/patients", ...)` → `APIRouter(prefix="/api/v1/patients", ...)`. Add `dependencies=[Depends(require_api_key)]` on each router.
- `app/api/fhir.py` — the `/fhir-bundle` route currently mounts under `/patients/{id}/fhir-bundle`. Leave it for M9 to replace; in this milestone just add the auth dependency.
- `app/main.py` — include a tiny `/api/v1/capabilities` placeholder (filled out properly in M12).
- `tests/test_read_api.py` — bulk URL-prefix update.
- `tests/test_fhir.py` — same, for the existing bundle test.

**Acceptance:** `pytest -q` green; `curl /api/v1/patients/{id}` returns 200 (with key) and 401 (without). The old `/patients/{id}` returns 404.

---

### M2 — Audit-on-read dependency (1.5 h)

Every successful GET writes one `AuditEvent` row with `action="read"` after the response is sent.

**Deliverables:**
- `app/api/deps.py` — new `audit_read` dependency: takes `Request`, `Response`, `BackgroundTasks`, and the API-key principal; on success (`response.status_code < 400`), schedules an `AuditEvent` insert.
- `app/db/models.py::AuditEvent` — no schema change; just confirm `action` accepts `"read"` in the documented set (it's a plain `str`, so this is a comment-only update).
- Wire `audit_read` onto every `/api/v1/*` and `/fhir/*` router as a router-level dependency.

**Acceptance:** after a GET, exactly one `AuditEvent` with `action="read"` exists for that path. A 4xx response does NOT write an AuditEvent. Tested in `tests/test_audit.py`.

---

### M3 — Unified exception handler: RFC 7807 + OperationOutcome (2 h)

A single FastAPI exception handler that branches on path prefix.

**Deliverables:**
- `app/api/errors.py` — new module:
  - `problem_response(request, exc) -> JSONResponse` (`application/problem+json`).
  - `operation_outcome_response(request, exc) -> JSONResponse` (FHIR `OperationOutcome`).
  - `register_exception_handlers(app)` that attaches a single `HTTPException` and `RequestValidationError` handler that dispatches on `request.url.path.startswith("/fhir/")`.
- `app/main.py` — call `register_exception_handlers(app)`.
- Adjust existing `HTTPException(status_code=..., detail=...)` raises so the unified handler picks them up (no code-site changes needed if FastAPI's default `HTTPException` is used).

**Acceptance:** `GET /api/v1/patients/<unknown-uuid>` returns `application/problem+json` with `{type, title, status:404, detail, instance}`. `GET /fhir/Patient/<unknown-uuid>` returns `application/fhir+json` with a valid `OperationOutcome`. Tested in `tests/test_errors.py`.

---

### M4 — ETag + If-None-Match (1.5 h)

Conditional reads.

**Deliverables:**
- `app/api/etag.py` — `make_etag(payload: Any) -> str` using `xxhash.xxh64(canonical_json(payload))[:8]` and the weak-ETag prefix `W/"…"`. `canonical_json` does `json.dumps(payload, sort_keys=True, default=str)`.
- A response-mutating dependency `etag_response(request, response, body)` — or, more simply, a small wrapper in each handler: compute ETag from the response body, compare to `request.headers.get("if-none-match")`, return `Response(status_code=304)` on match else set `response.headers["ETag"]`.
- New dep: `xxhash` (add to `pyproject.toml`).

**Acceptance:** every `200 OK` carries an `ETag` header. A repeat request with `If-None-Match: <etag>` yields `304 Not Modified` with no body. Tested in `tests/test_etag.py`.

---

### M5 — Pydantic schemas refactor + provenance shape (2.5 h)

Bake the provenance and trinary-status shapes into the response models.

**Deliverables:**
- `app/api/schemas.py` extended:
  - `ProvenanceRead` — `{document_id: UUID, char_start: int, char_end: int, snippet: str}`.
  - `SectionRead` — add `status: Literal["found","looked_but_missing","not_assessed"]`, make `text` and `provenance` `Optional`.
  - `ScaleRead` — new model: `{instrument, score, severity, status, items?, provenance}`.
  - `ObservationRead` — add `provenance: ProvenanceRead` (mandatory for LOINC-coded rows).
  - `AsamEvidenceRead`, `TjcCoverageRead` — add `provenance` where evidence pointers exist; conform to the dual model (snake_case here; a separate FHIR-shaped variant in M8).
- A second module `app/api/fhir_schemas.py` for the FHIR-aligned schemas with `model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)`.

**Acceptance:** unit tests verify a `SectionRead(status="not_assessed", text=None, provenance=None)` validates; mypy clean; existing endpoints continue to work (no behavior change yet).

---

### M6 — Trinary completeness scoring (2 h)

The `status` enum populated correctly, and `completeness_score` derived.

**Deliverables:**
- `app/api/completeness.py` — new module:
  - `classify_section(section_key, sections_dict, template_for_doc_type) -> Literal[…]` — given the BPS template's expected section set, return `found` if non-empty, `looked_but_missing` if present in the template but empty, `not_assessed` if absent from the template entirely.
  - `score(sections: dict[str, SectionRead]) -> float` — `|found| / (|found| + |looked_but_missing|)`.
- A BPS template constant — the 21 expected section keys from the M1 SimplePractice template — lives in `app/api/completeness.py` (not in the seed YAMLs, since this is presentation-layer policy).
- `app/api/patients.py::get_patient_intake` — populate `status` per section, compute the score.

**Acceptance:** for Marcus's chart (all 21 BPS sections present), every section is `found`, score is `1.0`. A test that artificially blanks one section asserts `looked_but_missing` and a lower score. A test for `not_assessed` uses a hypothetical missing section. `tests/test_completeness.py`.

---

### M7 — Canonical `GET /api/v1/patients/{id}/chart` (3 h)

The brief's required endpoint.

**Deliverables:**
- `app/api/patients.py::get_patient_chart` — new endpoint:
  - Load Patient + Encounters + ClinicalDocuments + ExtractedObservations + AsamEvidence + TjcCoverage in 6 queries.
  - Build the response per PRD §6.1: `{meta, patient, intake, timeline, observations, scales, asam_summary, tjc_coverage}`.
  - Attach ETag (M4). Auth + audit via the router-level deps.
  - `meta.audit_id` and `meta.compliance_check_id` are pre-allocated nullable fields (for Phase 3 backward compat).
- New schema: `ChartRead` aggregating `PatientRead`, `IntakeRead`, `list[TimelineEntry]`, `list[ObservationRead]`, etc.

**Acceptance:** `GET /api/v1/patients/{id}/chart` returns the canonical shape; every observation carries provenance; `meta.completeness_score` ∈ `[0, 1]`. `tests/test_chart.py` validates the shape and asserts the round-trip on every observation in the response.

---

### M8 — `/fhir/*` search endpoints with `Bundle.searchset` (4 h)

The strict FHIR namespace, replacing the Phase-1 `/patients/{id}/fhir-bundle` over a series of milestones (M8–M9).

**Deliverables:**
- `app/api/fhir.py` reorganized:
  - `GET /fhir/Patient/{id}` — bare FHIR Patient resource.
  - `GET /fhir/DocumentReference` — Bundle search; params `patient`, `type`, `category`, `date` (with FHIR comparators `gt|lt|ge|le`).
  - `GET /fhir/DocumentReference/{id}` — bare resource.
  - `GET /fhir/Observation` — Bundle search; params `patient`, `code`, `category`.
  - `GET /fhir/Provenance?target={ref}` — Bundle search.
- `app/api/pagination.py` — new module: opaque cursor encode/decode (`base64({last_id, limit})`), `Bundle.link` builder with `self` + `next`.
- Default page sizes: timeline 50, documents 20, observations 50 (documented in M12's CapabilityStatement).

**Acceptance:** each Bundle round-trips through `fhir.resources.R4B.Bundle.model_validate(response.json())`. Paginating across the dataset reaches every row exactly once. `tests/test_fhir_search.py`.

---

### M9 — `/fhir/Patient/{id}/$everything` + `/fhir/metadata` (2 h)

Replace the Phase-1 `/patients/{id}/fhir-bundle` with the canonical FHIR operations.

**Deliverables:**
- `GET /fhir/Patient/{id}/$everything` — Synthea-shaped Bundle: Patient first, then Encounters, then per-Encounter Documents (DocumentReference + ClinicalImpression for progress notes), then Observations, then Provenance. Cursor pagination.
- `GET /fhir/metadata` — a hand-built `CapabilityStatement` describing the served resources, their search params, and the supported interactions (`read`, `search-type`, `operation`).
- `app/fhir/mappers.py::build_capability_statement()` — new helper.
- Deprecate (or remove) the Phase-1 `/patients/{id}/fhir-bundle` route in favor of `$everything`. If kept, add a 301 redirect.

**Acceptance:** `$everything` validates as a `Bundle`; `metadata` validates as a `CapabilityStatement`. The `entry[]` count in `$everything` for Marcus equals 1 (Patient) + 4 (Encounter) + 4 (DocumentReference) + 3 (ClinicalImpression) + 9 (Observation) + 9 (Provenance, one per Observation) = 30.

---

### M10 — Custom Provenance extension + Provenance resources (1.5 h)

The character-offset extension and the FHIR `Provenance` resources that carry it.

**Deliverables:**
- `app/fhir/mappers.py::PH_OFFSET_EXT` — extension URL constant.
- `to_fhir_provenance(observation, document) -> Provenance` — builds a `Provenance` resource targeting the Observation with `activity = DERIVE`, `entity.what → DocumentReference/{doc_id}`, and an `entity.extension` carrying the custom char-range extension.
- `build_patient_bundle` (existing) — extended to also emit Provenance entries (one per LOINC-coded Observation).
- README addendum (in M12) — document the custom extension URL.

**Acceptance:** `$everything` contains one `Provenance` per `Observation`; each `Provenance.entity[0].extension[].url == PH_OFFSET_EXT`; `start` and `end` `valueInteger` parts re-locate the snippet in `raw_text`.

---

### M11 — Tests (2.5 h)

The provenance round-trip, golden snapshots, and the existing-test cleanup.

**Deliverables:**
- `tests/test_provenance.py` extended — round-trip across **every endpoint that returns observations** (`/chart`, `/observations`, `/fhir/Observation`, `$everything`). Replaces the table-level round-trip from Phase 1 with a response-level round-trip.
- `tests/test_chart.py` — golden snapshot of `/chart` for Marcus (using `syrupy` if Q3 says yes, otherwise plain assert on a stable subset of fields).
- `tests/test_audit.py` — every successful GET writes one AuditEvent; 4xx writes none.
- `tests/test_etag.py` — second request with `If-None-Match` → 304.
- `tests/test_errors.py` — `/api/v1/*` 404 is RFC 7807; `/fhir/*` 404 is OperationOutcome.
- `tests/test_fhir_search.py` — pagination, search params, Bundle.link[next].
- `tests/test_capability.py` — `/fhir/metadata` validates and lists every supported resource.
- (Optional Q3) `tests/test_schemathesis.py` — boots the app via ASGITransport and runs schemathesis against `/openapi.json` with `--checks all --max-examples 50`.

**Acceptance:** `pytest -q` green; coverage ≥ 90% on `app/api/*`.

---

### M12 — README polish + Phase 2 sample JSON (2 h)

The reviewer-facing artifacts.

**Deliverables:**
- `examples/marcus_reyes_chart.json` — the live `/chart` response, regenerated (the Phase 1 `examples/marcus_reyes.json` is now superseded; either replace it or keep both with cross-references in README).
- `examples/marcus_reyes_bundle.json` (optional) — the live `$everything` Bundle.
- `README.md` polish:
  - Lead with the "this is not a wrapper" three-sentence framing from the playbook.
  - The labeled diagram showing Phase 1 → Phase 2.
  - Inline the `/chart` shape (~80 lines truncated) so a reviewer sees it without leaving the README.
  - The custom Provenance extension documented (URL + rationale).
  - The dual-surface argument: ergonomic on `/api/v1/*` for graders, strict on `/fhir/*` for tooling.
  - The submission contract (Kyle + Eshan, inline `/chart` + attached Bundle).
- `CHANGELOG.md` — append a one-liner per the project convention.

**Acceptance:** a reviewer reads the README in <10 minutes; `examples/marcus_reyes_chart.json` is the current `/chart` output.

---

### M13 — (Optional, Q1) NDJSON `$export` stub (1.5 h)

Synchronous demonstration of the FHIR Bulk Data IG.

**Deliverables:**
- `GET /fhir/Patient/{id}/$export` — `StreamingResponse(generator, media_type="application/fhir+ndjson", headers={"Content-Disposition": "attachment"})`. The generator iterates the patient compartment and yields one `json.dumps(resource) + "\n"` per resource.
- README note: "Demonstrates the IG; production would split into kick-off + status + manifest per `hl7.org/fhir/uv/bulkdata`."

**Acceptance:** `curl /fhir/Patient/{id}/$export -H "Accept: application/fhir+ndjson" -o out.ndjson` produces a file with one JSON-per-line, every line validates as a FHIR Resource.

---

## 3. Sequencing & dependencies

```
M0 (carryover fixes)
   │
   ▼
M1 (/api/v1 namespace + auth) ──────────────┐
   │                                        │
   ▼                                        │
M2 (audit-on-read) ── M3 (error handler) ── M4 (ETag)
   │
   ▼
M5 (schemas + provenance shape)
   │
   ▼
M6 (trinary completeness) ── M7 (canonical /chart) ── (acceptance milestone for the brief)
                                  │
                                  ▼
                              M8 (/fhir/* search) ── M9 ($everything + metadata) ── M10 (Provenance ext)
                                                                                       │
                                                                                       ▼
                                                                                   M11 (tests)
                                                                                       │
                                                                                       ▼
                                                                                   M12 (README + sample JSON)
                                                                                       │
                                                                              (optional) M13 ($export)
```

M0 must precede everything (it's the prerequisite-fix milestone). M3 and M4 are independent of each other and can run in parallel. M7 (the canonical `/chart`) is the **Task 2 acceptance milestone** — at the end of M7 the brief's required deliverable is shippable; M8–M13 deepen the FHIR conformance and the differentiator surface.

## 4. Tech stack additions

```toml
# additions to pyproject.toml dependencies
xxhash = "^3"                # ETag generation (M4)

# dev / optional
syrupy = "^4"                # golden snapshots (M11, optional)
schemathesis = "^3"          # CI fuzzing (M11, optional, gated by Q3)
```

No new database migrations. No new infrastructure. Postgres and the existing pgvector + alembic setup are unchanged.

## 5. Risks & branch points

| Trigger | Branch |
|---|---|
| Schemathesis surfaces 50+ low-severity issues | Fix only crashes / contract violations (500s, undocumented status codes). Document the rest in README. |
| `fhir.resources.R4B.Bundle.model_validate` fails on `$everything` due to ref-resolution edge cases | Move to `model_validate_json` and rely on the round-trip test instead of strict cross-resource validation. |
| ETag computation slows `/chart` past 500ms (xxhash is O(payload-size)) | Cache the canonical-JSON once per request via `response.body` (FastAPI exposes it after rendering). Acceptable cost in practice — Marcus's `/chart` is ~100KB. |
| Auth-on-read breaks `examples/marcus_reyes_chart.json` regeneration (the generator now needs an API key) | The generator script reads `INGEST_API_KEY` from `.env` and sends `X-API-Key`. Update `scripts/dump_sample_json.py` (or the existing heredoc) accordingly. |
| Phase 1's `examples/marcus_reyes.json` referenced in README → broken link after M12 supersedes it | Either delete and update README, or keep both with cross-references. Decide in M12. |

## 6. What ships at end of Phase 2

Repo with:

- ✅ `/api/v1/*` namespace (with auth, audit-on-read, ETag, RFC 7807 errors, cursor pagination) and the canonical `GET /api/v1/patients/{id}/chart` endpoint.
- ✅ `/fhir/*` namespace (strict R4: Patient, DocumentReference, Observation, Provenance search; `$everything` Bundle; CapabilityStatement; OperationOutcome errors).
- ✅ Char-offset provenance on every observation in every response, asserted across endpoints.
- ✅ Trinary completeness on every BPS section + a `completeness_score` in `meta`.
- ✅ Custom Provenance extension URL documented.
- ✅ 60+ tests (Phase 1's 42 + ~20 new from M11), 90% coverage on `app/api/*`.
- ✅ `examples/marcus_reyes_chart.json` and optionally `examples/marcus_reyes_bundle.json`.
- ✅ README leading with "this is not a wrapper" and a dual-surface architecture diagram.

**Phase 3 inherits:** a deterministic read substrate with provenance baked into every observation. The Phase 3 endpoints (`POST /patients/{id}/asam-loc`, `POST /patients/{id}/tjc-audit`) become *narrate over retrieved evidence* — they consume `AsamEvidence`, `TjcCoverage`, and the `ExtractedObservation` rows surfaced through this phase's reads.
