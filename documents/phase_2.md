# Task 2 / Phase 2 Implementation Playbook — Perspectives Health Intern Assessment

## TL;DR
- **Ship a dual-surface API**: a clinical-friendly `/api/v1/*` namespace (snake_case JSON envelopes, ergonomic for graders to read) and a strict `/fhir/*` namespace (FHIR R4 resources, `OperationOutcome` errors, `Bundle` pagination). Both are thin SQL views over Phase 1's `ClinicalDocument.fhir_json`, `ExtractedObservation`, and `Patient.fhir_json` — Task 2 is response-shaping, not parsing.
- **The canonical brief-required endpoint is `GET /api/v1/patients/{id}/chart`**, returning `{patient, intake, timeline, observations, scales, provenance, completeness}`. Every observation carries `provenance: {document_id, char_start, char_end, snippet}` so a test can `assert raw_text[char_start:char_end] == snippet`. This is the single piece that most differentiates this submission from an EHR wrapper.
- **Lean into FHIR conventions but don't be religious**: use FHIR `Bundle.searchset` for collections with `link.relation=next`, `OperationOutcome` on `/fhir/*` errors, RFC 7807 `application/problem+json` on `/api/v1/*` errors. Use `_elements` partial response on FHIR endpoints, ETag/`If-None-Match` on all reads, an audit-log dependency that writes to `AuditEvent` for every request, and an NDJSON `Patient/$export` to demonstrate familiarity with the Bulk Data IG without actually implementing the full asynchronous Kick-Off flow.

## Key Findings

### 1. Response shape: FHIR-aligned vs. custom

The FHIR R4 spec is clear that progress notes and intake narratives belong in `DocumentReference` (metadata pointer) plus either inline `Binary` or `Composition` for structured sections (hl7.org/fhir/R4/documentreference.html). The Argonaut/US Core profile's LOINC type bindings for clinical notes are `11506-3 Progress Note`, `11488-4 Consultation Note`, `34117-2 History & Physical Note`, `18842-5 Discharge Summary`, `28570-0 Procedure Note` — these should be your `document_type` mapping. US Core treats `DocumentReference` as the canonical "find all clinical notes for a patient" entry point and specifies the search shape: `GET [base]/DocumentReference?patient={Type/}[id]&category=http://hl7.org/fhir/us/core/CodeSystem/us-core-documentreference-category|clinical-note`.

For the brief's required output, however, FHIR shapes are *verbose* for a human grader: a Bundle with 30 entries is harder to scan than `{"patient": {...}, "intake": {"text": "..."}, "timeline": [...]}`. The pragmatic decision: expose **both** under different path prefixes, with the same underlying queries. Custom shapes win on `/api/v1` because the brief is evaluated on "captures all relevant clinical fields" — graders want to read the JSON, not validate it.

**Envelope decision**: bare resources on FHIR paths (FHIR is its own envelope); `{data, meta, links}` on `/api/v1` collection endpoints; bare object on `/api/v1` singletons. `meta` carries `extraction_version`, `generated_at`, `total`, `completeness_score`. This matches JSON:API spirit without paying the full JSON:API tax.

**Pagination**: the FHIR spec requires Bundle `link[relation=next]` and explicitly warns against clients constructing their own page URLs (Darren Devitt: "in order to successfully paginate results in FHIR you MUST use the provided Bundle links as they are: 'self', 'prev' and 'next'. You cannot and should not construct your own URLs based on parameters you find for individual FHIR servers"). Use opaque cursor tokens (base64-encoded `last_id`); don't expose `offset`/`limit` as the contract. Azure Health Data Services and many production servers only support `next`, not `prev/first/last`, so don't over-build. For default page size, 1upHealth documents `_count` defaulting to 10 with a max of 100 ("By default the number of resources per page is set to 10... Note that the _count parameter value can't exceed 1000" on Azure); Epic's public docs do not publish a canonical default. I recommend 50 for timeline, 20 for documents — and document it in your `CapabilityStatement`.

**Errors**: the FHIR HTTP spec states "If the failure occurs at a FHIR-aware level of processing, the HTTP response SHOULD be accompanied by an OperationOutcome." RFC 7807 Problem Details (`application/problem+json`) is the equivalent for non-FHIR endpoints. Implement a single FastAPI exception handler that branches on the path prefix.

**IDs**: use stable opaque UUIDs as the canonical `id`. Carry the SimplePractice external ID as `identifier[].system=http://simplepractice.com/...` per FHIR `Identifier` convention. Never expose internal sequential PKs.

### 2. Extraction coverage — what Phase 1 may have missed

Phase 1 already does PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS, C-SSRS, substance/med/dx entity tagging, and ASAM/TJC evidence indexing. Task 2 needs to surface — not re-derive — the following BPS taxonomy. ICANotes documents the canonical structure: "A complete assessment covering all five domains is the foundation of defensible, billable behavioral health care" with 25 elements across biological, psychological, social, risk, and formulation domains.

| Domain | Fields the API must expose |
|---|---|
| Identifying | name, DOB, sex/gender, race/ethnicity, language, address, insurance |
| Presenting | chief complaint, HPI, onset, duration, triggers, severity |
| Biological | medical conditions, current meds + adherence cues, allergies, substance use history, family medical history |
| Psychological | psychiatric history, prior hospitalizations, current symptoms, mental status exam, trauma history, coping |
| Social | housing, employment, education, relationships, legal, spirituality, cultural factors, **SDOH per US Core SDOH IG (food, housing, transportation, finance security)** |
| Risk | SI/HI, C-SSRS, violence, self-harm, protective factors |
| Diagnostic | DSM-5/ICD-10 dx (provisional, rule-out, confirmed), differential |
| Formulation | clinician summary, treatment recommendations, level of care (ASAM dimensions) |

The "trinary completeness" problem matters here: every BPS field should resolve to `{status: "found"|"not_assessed"|"looked_but_missing", value, provenance}`. This is what makes the API auditable for Task 3 and is the single most defensible answer to "Does the API capture all relevant clinical fields?"

Free-text findings that don't tie to a scale (e.g., "patient reports drinking 'too much' but declined AUDIT-C") should surface as US Core `Observation` with `category=social-history` and `valueString`. SDOH-specific findings should bind to LOINC grouping code `LG41762-2` ("The LOINC 'grouping' code: LG41762-2 'Social Determinants Of Health' is used to categorize SDOH for Assessments, Problem, and Service Requests" — US Core SDOH guidance, hl7.org/fhir/us/core/2022Jan/sdoh.html).

### 3. REST patterns for clinical data

- **Auth**: API key in `X-API-Key` header is sufficient for the assessment; document the OAuth/SMART migration path. The SMART App Launch v2.2.0 IG (`hl7.fhir.uv.smart-app-launch#2.2.0` based on FHIR 4.0.1) defines scope grammar as `( 'patient' | 'user' | 'system' ) '/' ( fhir-resource | '*' ) '.' ( 'c'|'r'|'u'|'d'|'s'|'*' )` (build.fhir.org/ig/HL7/smart-app-launch/scopes-and-launch-context.html). Stub the scope-check dependency so swapping in real OAuth later is trivial.
- **Audit on read**: HIPAA's accounting-of-disclosures rule effectively requires it. Write one `AuditEvent` row per request via a FastAPI dependency that runs after the response (BackgroundTasks).
- **ETag / If-None-Match**: FHIR uses weak ETags `W/"<versionId>"` per hl7.org/fhir/R4/http.html: "Clients may use the If-Modified-Since, or If-None-Match HTTP header on a read request. If so, they SHALL accept either a 304 Not Modified as a valid status code... or full content." Generate from `xxhash(resource_json)` or `meta.versionId`. Return `304 Not Modified` when matched. Cheap, high signal.
- **Compression**: gzip middleware is one line of FastAPI; for clinical JSON, gzip typically achieves a ~3.7× compression ratio (NCBI PMC12338849 reports 3.66× on 300-patient medical-record JSON; Baeldung's "Reducing JSON Data Size" reports gzip to 25.3% of original size — ≈3.95×). Enable it.
- **Idempotency**: not needed for read-only Task 2. Mention in README for re-ingest/re-extract endpoints if they exist.

### 4. Reference architectures

- **Epic on FHIR** (fhir.epic.com): R4, OperationOutcome on errors, opinionated profiles, Binary endpoint for note bodies. DocumentReference.Read returns metadata only; the note body lives at `Binary/{id}` referenced from `content.attachment.url`. open.epic states: "DocumentReference provides a list of available documents for a patient. CDA documents and clinical notes are examples of documents that this resource may return." Epic uses SMART on FHIR launch flows and post-filter search parameters as of the May 2024 build.
- **Oracle Health / Cerner Millennium** (fhir.cerner.com/millennium/r4): publishes `CapabilityStatement` at `/metadata`, supports Bulk Data `$export` for Patient and Group, with the spec noting "The Patient Export operation does not support an export of all patients. A list of patients IDs must be provided in the request" — i.e., callers supply the cohort.
- **Metriport** (open-source, FHIR R4-native): webhook-based async delivery model, OAuth 2.0, audit log per query. Per their docs: "Metriport logs every access and query, including who queried which patient data, what was returned, and when. These logs are available for audits and to meet requirements of regulations like the 21st Century Cures Act's information blocking provisions." Architecturally the closest peer to Phase 1.
- **Health Gorilla / 1upHealth / Particle Health**: FHIR-strict aggregators; surface is purely `/fhir/{Resource}` with searchset Bundles, cursor pagination (Health Gorilla) or `_count`/`_offset` (1upHealth), OperationOutcome errors.
- **Eleos / Lyssn**: behavioral-health-specific but with no public REST API — Eleos explicitly markets itself as "Integrates with any web-based EHR via browser extension—no APIs, no custom builds, no compatibility risk." Notable for what they DON'T do — they avoid integration tax by not exposing APIs. Your submission can credibly position itself as "the API surface Eleos chose not to build."
- **Synthea**: outputs one FHIR R4 transaction Bundle per patient, Patient first, then resources roughly grouped by Encounter chronologically (mitre.github.io/fhir-for-research: "This Bundle contains a single Patient resource as the first entry, followed by other patient-specific resources such as Conditions, Observations, Procedures, etc, roughly grouped by Encounter in chronological order"). This is exactly the shape your `Patient/$everything` should return.

### 5. Pydantic v2 + FastAPI

Critical pattern: separate `db/` SQLModel `table=True` ORM models from `schemas/` Pydantic `BaseModel` response models. Use `model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)` on FHIR-shaped response models so internal `snake_case` serializes to FHIR's `camelCase`. Pydantic's docs note: "In v2.11, `serialize_by_alias` was introduced to address the popular request for consistency with alias behavior for validation and serialization settings... We anticipate changing this default in V3." Set it explicitly to avoid relying on a default that may change. Use `computed_field` for derived values like `completeness_score`. Don't pass JSONB columns through opaquely on `/fhir/*` — validate them with `fhir.resources` Pydantic models to catch drift.

### 6. Provenance — the differentiator

There is **no standard FHIR R4 extension for character-offset citations**. The base spec's `Provenance` resource (hl7.org/fhir/R4/provenance.html) uses `target → Observation`, `activity = DERIVE` (from `http://terminology.hl7.org/CodeSystem/v3-DataOperation`), `entity.role = source`, `entity.what → DocumentReference`. The IHE mXDE profile confirms the canonical pattern: "target will point at ALL of the data found within the document … activity will be Derivation to indicate the target resources were derived from documents." The closest base-spec extensions are `cqf-citation` (a bibliographic `valueString`) and `observation-time-offset` (waveform offsets, not text). No standard text-anchor selector exists in R4 — implementers must define a custom extension. Define your own extension URL `http://perspectiveshealth.ai/fhir/StructureDefinition/text-position-selector` with `valueInteger` start/end and a `valueString` exact snippet — document it in your README as a deliberate IG-level decision.

## Details

### Endpoint inventory

| Method | Path | Response | Pagination | Audit | Notes |
|---|---|---|---|---|---|
| GET | `/health` | `{status, db, version}` | — | no | k8s liveness |
| GET | `/api/v1/capabilities` | custom capability doc | — | no | Self-describe |
| GET | `/fhir/metadata` | `CapabilityStatement` | — | no | Per FHIR spec |
| GET | `/api/v1/patients` | `{data:[...], meta, links}` | cursor | yes | list |
| GET | `/api/v1/patients/{id}` | `Patient`-shaped custom | — | yes | demographics + counts |
| **GET** | **`/api/v1/patients/{id}/chart`** | **canonical shape (below)** | — | yes | **brief deliverable** |
| GET | `/api/v1/patients/{id}/intake` | `{document_id, text, sections, scales, provenance}` | — | yes | full BPS |
| GET | `/api/v1/patients/{id}/timeline` | `{data:[notes], meta, links}` | cursor | yes | filterable |
| GET | `/api/v1/patients/{id}/documents` | list | cursor | yes | all docs |
| GET | `/api/v1/patients/{id}/documents/{doc_id}` | single | — | yes | with sections |
| GET | `/api/v1/patients/{id}/observations` | list | cursor | yes | filter by code |
| GET | `/api/v1/patients/{id}/asam-evidence` | `{dimensions: {...}}` | — | yes | Phase 1 |
| GET | `/api/v1/patients/{id}/tjc-coverage` | `{eps: [...]}` | — | yes | Phase 1 |
| GET | `/fhir/Patient/{id}` | FHIR `Patient` | — | yes | ETag |
| GET | `/fhir/Patient/{id}/$everything` | `Bundle` | next | yes | Synthea-shaped |
| GET | `/fhir/Patient/{id}/$export` | NDJSON manifest | — | yes | Bulk Data stub |
| GET | `/fhir/DocumentReference` | `Bundle` | next | yes | `?patient&type&category&date` |
| GET | `/fhir/DocumentReference/{id}` | FHIR | — | yes | — |
| GET | `/fhir/Observation` | `Bundle` | next | yes | `?patient&code&category` |
| GET | `/fhir/Composition/{id}` | FHIR | — | yes | for intake |
| GET | `/fhir/Provenance?target={ref}` | `Bundle` | next | yes | citation index |

Filtering: `/api/v1/patients/{id}/timeline?from=2025-01-01&to=2026-05-15&author=...&type=progress_note&section=substance_use`. Date filters MUST support FHIR comparators `gt|lt|ge|le` on `/fhir/*` paths per US Core DocumentReference SHALL.

### Canonical response shape (`GET /api/v1/patients/{id}/chart`)

```json
{
  "meta": {
    "generated_at": "2026-05-15T14:22:08Z",
    "extraction_version": "phase1-v1.2.0",
    "completeness_score": 0.86,
    "etag": "W/\"3f9a2c\""
  },
  "patient": {
    "id": "pt_01HW...",
    "identifiers": [{"system": "simplepractice", "value": "SP-44219"}],
    "name": {"given": ["Jordan"], "family": "Reeves"},
    "birth_date": "1989-03-14",
    "gender": "female",
    "address": {"city": "Denver", "state": "CO", "postal_code": "80205"},
    "phone": "+1-303-555-0144",
    "preferred_language": "en"
  },
  "intake": {
    "document_id": "doc_01HW...",
    "document_type": "biopsychosocial",
    "encounter_date": "2026-04-02",
    "author": {"name": "Dr. Maya Chen, LCSW", "npi": "1234567890"},
    "loinc_type": {"system": "http://loinc.org", "code": "11488-4", "display": "Consultation Note"},
    "full_text": "...complete raw text...",
    "sections": {
      "presenting_problem": {"status": "found", "text": "...", "provenance": {"char_start": 412, "char_end": 1188}},
      "substance_use": {"status": "found", "text": "...", "provenance": {"char_start": 1189, "char_end": 2240}},
      "trauma_history": {"status": "looked_but_missing", "text": null, "provenance": null},
      "family_history": {"status": "not_assessed", "text": null, "provenance": null}
    },
    "scales": [
      {"instrument": "PHQ-9", "score": 17, "severity": "moderately severe",
       "items": [{"q": 1, "score": 3}],
       "provenance": {"document_id": "doc_01HW...", "char_start": 3201, "char_end": 3654, "snippet": "PHQ-9 total: 17"}},
      {"instrument": "AUDIT-C", "score": 8, "severity": "high risk",
       "provenance": {"document_id": "doc_01HW...", "char_start": 2890, "char_end": 3010, "snippet": "AUDIT-C = 8"}}
    ]
  },
  "timeline": [
    {
      "document_id": "doc_01HW...", "date": "2026-04-09", "type": "progress_note",
      "loinc_type": {"system": "http://loinc.org", "code": "11506-3", "display": "Progress Note"},
      "format": "SOAP",
      "author": {"name": "Dr. Maya Chen, LCSW", "npi": "1234567890"},
      "sections": {"subjective": "...", "objective": "...", "assessment": "...", "plan": "..."},
      "full_text": "..."
    }
  ],
  "observations": [
    {"id": "obs_01HW...", "code": {"system": "http://loinc.org", "code": "44261-6", "display": "PHQ-9 total"},
     "value": 17, "effective_date": "2026-04-02",
     "provenance": {"document_id": "doc_01HW...", "char_start": 3201, "char_end": 3654, "snippet": "PHQ-9 total: 17"}}
  ],
  "asam_summary": {"dim1": "moderate", "dim2": "low"},
  "tjc_coverage": {"covered_eps": 14, "total_eps": 18, "missing": ["EP.3", "EP.7"]}
}
```

### Critical code snippets

**Audit dependency** (FastAPI):
```python
async def audit_read(request: Request, response: Response, bg: BackgroundTasks,
                     session: AsyncSession = Depends(get_session),
                     principal: Principal = Depends(api_key_auth)):
    async def _write():
        async with session_factory() as s:
            s.add(AuditEvent(actor=principal.id, action="read",
                             resource=str(request.url.path),
                             status=response.status_code,
                             occurred_at=datetime.utcnow()))
            await s.commit()
    bg.add_task(_write)
```

**ETag conditional read**:
```python
def make_etag(payload: dict) -> str:
    return f'W/"{xxhash.xxh64(canonical_json(payload).encode()).hexdigest()[:8]}"'

@router.get("/patients/{pid}/chart")
async def chart(pid: str, request: Request, response: Response):
    body = build_chart(pid)
    etag = make_etag(body)
    if request.headers.get("If-None-Match") == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag
    return body
```

**FHIR Bundle assembly** (Synthea-shaped `$everything`):
```python
def build_everything_bundle(patient_id: str, cursor: str | None, limit: int = 50) -> dict:
    entries = []
    p = load_patient_fhir(patient_id)
    entries.append({"fullUrl": f"urn:uuid:{p['id']}", "resource": p,
                    "search": {"mode": "match"}})
    for doc in load_docs(patient_id, cursor, limit):
        entries.append({"resource": doc["fhir_json"], "search": {"mode": "include"}})
    bundle = {"resourceType": "Bundle", "type": "searchset",
              "total": count(patient_id), "entry": entries, "link": []}
    bundle["link"].append({"relation": "self", "url": current_url()})
    if has_more:
        bundle["link"].append({"relation": "next", "url": next_url(cursor)})
    return bundle
```

**NDJSON streaming for `$export`** (returns manifest synchronously; real Bulk Data is async):
```python
@router.get("/fhir/Patient/{pid}/$export")
async def export(pid: str):
    async def gen():
        for resource in iter_patient_compartment(pid):
            yield json.dumps(resource) + "\n"
    return StreamingResponse(gen(),
        media_type="application/fhir+ndjson",
        headers={"Content-Disposition": f"attachment; filename={pid}.ndjson"})
```

**FHIR `_elements` partial response**:
```python
def project(resource: dict, elements: list[str] | None) -> dict:
    if not elements:
        return resource
    keep = {"resourceType", "id", "meta"} | set(elements)
    out = {k: v for k, v in resource.items() if k in keep}
    out.setdefault("meta", {}).setdefault("tag", []).append(
        {"system": "http://terminology.hl7.org/CodeSystem/v3-ObservationValue",
         "code": "SUBSETTED"})
    return out
```

**Provenance with character-offset extension**:
```python
PH_OFFSET_EXT = "http://perspectiveshealth.ai/fhir/StructureDefinition/text-position-selector"

def build_provenance(obs_id: str, doc_id: str, start: int, end: int, snippet: str) -> dict:
    return {
        "resourceType": "Provenance",
        "target": [{"reference": f"Observation/{obs_id}"}],
        "recorded": datetime.utcnow().isoformat() + "Z",
        "activity": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/v3-DataOperation",
                                 "code": "DERIVE"}]},
        "agent": [{"type": {"coding": [{"code": "assembler"}]},
                   "who": {"display": "phase1-extractor"}}],
        "entity": [{"role": "source",
                    "what": {"reference": f"DocumentReference/{doc_id}"},
                    "extension": [{"url": PH_OFFSET_EXT, "extension": [
                        {"url": "start", "valueInteger": start},
                        {"url": "end", "valueInteger": end},
                        {"url": "exact", "valueString": snippet}]}]}]
    }
```

### Testing strategy

1. **Provenance round-trip** (the killer test): for every observation in every chart response, `assert source_doc.raw_text[ev.char_start:ev.char_end] == ev.snippet`. This is the test that proves no hallucination.
2. **Golden JSON** for `/api/v1/patients/{persona_id}/chart` — one fixture per persona. Use `syrupy` snapshot lib so updates are explicit.
3. **fhir.resources Pydantic validation** on every `/fhir/*` response — catches schema drift.
4. **schemathesis** against `/openapi.json` with `--checks all --max-examples 200 --continue-on-failure` in CI; treat undocumented status codes as failures. Per the docs: "Schemathesis tests OpenAPI and GraphQL APIs by generating inputs from your schema, adapting to server responses... 💥 500 errors that crash your API on edge case inputs."
5. **pytest-asyncio + httpx.AsyncClient(transport=ASGITransport(app=app))** for end-to-end against the live FastAPI app, fixture-seeded from `persona.yaml`.
6. **Completeness scoring test**: assert `completeness_score` is in `[0, 1]` and equals the ratio of `found` to `(found + looked_but_missing)` fields.

Coverage target: 90% line, 100% on path operations, 100% on error handlers.

### Implementation roadmap with time estimates

| Day | Work | Hrs |
|---|---|---|
| 1 AM | Refactor Phase 1 router into `/api/v1/` namespace; add `/api/v1/capabilities` and FastAPI tags | 3 |
| 1 PM | Audit-log dependency + API-key auth + ETag middleware | 3 |
| 2 AM | Canonical `/chart` endpoint + Pydantic response models with provenance shape | 4 |
| 2 PM | Trinary `status` logic and `completeness_score` computed field | 3 |
| 3 AM | `/fhir/Patient`, `/fhir/DocumentReference`, `/fhir/Observation`, `/fhir/Provenance` search + `_elements` | 5 |
| 3 PM | `/fhir/Patient/{id}/$everything` bundle + `/fhir/metadata` CapabilityStatement | 3 |
| 4 AM | NDJSON `$export` + RFC 7807 / OperationOutcome unified error handler | 3 |
| 4 PM | Provenance round-trip tests, golden snapshots, schemathesis CI | 4 |
| 5 AM | OpenAPI polish (examples, operation IDs, descriptions) + README + sample JSON file | 4 |
| 5 PM | Loom demo recording (12 min) + dry-run submission | 2 |

Total: ~34 hours.

## Recommendations

1. **Name the canonical endpoint `/api/v1/patients/{id}/chart`** — not `/extract` (sounds like an action, not a resource) and not `/export` (overloaded with FHIR Bulk Data). "Chart" matches clinician mental model and is the noun that scales to "give me this patient's complete chart."
2. **Always include observations and scales in `/chart`** even though the brief only asks for demographics + intake text + timeline. The "Extraction Accuracy" criterion is explicitly evaluated on field coverage; the surplus is free points.
3. **Make provenance non-optional** in every response that cites extracted data. The char_start/char_end/snippet triple is the single most distinctive feature of this submission against an EHR wrapper.
4. **Submit BOTH sample JSONs** in the README: the `/chart` shape AND a `Patient/$everything` Bundle. Two readers, two languages. Kyle (CTO, technical depth) reads the FHIR Bundle and the schemathesis CI; Eshan (founder, policy/clinical) reads the `/chart` shape and the completeness scoring.
5. **Don't implement async Bulk Data Kick-Off** (202 → polling → manifest). Stub the synchronous NDJSON stream and call it out in README: "Demonstrates the IG; production would split into kick-off + status + manifest per hl7.org/fhir/uv/bulkdata." This is the single biggest scope-control decision.
6. **Skip SMART on FHIR for the submission**. Document the scope-string parser and a stub `Depends(scope_check)` so the migration path is visible; don't build the OAuth flow.

Benchmarks that would change these recommendations: if grader feedback emphasizes async patterns → upgrade `$export` to real polling. If feedback emphasizes auth → add SMART backend services with JWT client assertion. If feedback emphasizes UI integration → add SMART EHR launch context.

## Pitfalls

- **Empty timeline**: return `{"data": [], "meta": {"total": 0}, "links": {}}` with HTTP 200, NOT 404. 404 is reserved for "patient does not exist."
- **Patient not found**: return RFC 7807 `{type, title: "Patient not found", status: 404, instance: "/api/v1/patients/xyz"}` on `/api/v1`; `OperationOutcome` with `issue.severity=error, issue.code=not-found` on `/fhir`.
- **Partial extraction failures**: surface them. A scale that failed to parse should appear as `{"instrument": "PHQ-9", "status": "extraction_failed", "error": "...", "provenance": null}`. Silent omission is the most dangerous failure mode for a clinical API.
- **Default pagination size**: 50 for timeline, 20 for documents. Document in `CapabilityStatement.rest.resource.searchParam` and in `/api/v1/capabilities`.
- **FHIR Bundle size**: keep `$everything` under ~5MB by default. Page large patients.
- **LLM unavailability**: Task 2 endpoints MUST be deterministic — no LLM at request time. All LLM work happens in Phase 1 ingestion. If a degraded extraction is detected, surface it as a `status: "extraction_degraded"` field, never a 500.
- **Backward compat for Task 3**: pre-allocate `meta.audit_id`, `meta.compliance_check_id` fields (null for now). When Task 3 ships, populate without a breaking change.
- **Time zones**: serialize all timestamps as ISO 8601 with `Z` suffix (FHIR `instant`). Partial dates (e.g., birth_date with no month) use FHIR `date` semantics.
- **Internal IDs**: never expose autoincrement PKs. Use ULIDs or UUID v7.

## Submission Polish

**README framing** — lead with three sentences:

> "This is not a wrapper. SimplePractice exposes raw notes; this API exposes a structured, FHIR-aligned, provenance-stamped clinical chart. Every extracted observation cites the exact character range of its source — `assert raw_text[char_start:char_end] == snippet` is part of the test suite."

Follow with a labeled diagram showing Phase 1 (ingestion + structured storage) → Phase 2 (this API). Make explicit that Task 2 is the read surface over Phase 1 storage, not a re-extraction layer.

**Sample JSON in the submission email**: paste the `/chart` response (truncated to ~80 lines) inline; attach the full `Patient/$everything` Bundle as `sample_bundle.json`. Inline beats attached for a tired reader.

**Demo recording (Loom, 10-12 min)**:
1. (0:30) Spin up `docker-compose up`; show health endpoint.
2. (1:00) Open Swagger at `/docs`, scroll the endpoint inventory — visual proof of surface area.
3. (2:00) `curl /api/v1/patients/{id}/chart | jq` — narrate the provenance fields.
4. (1:30) `curl /fhir/Patient/{id}/$everything | jq` — show same data in FHIR shape.
5. (1:00) Run pytest, highlight the provenance round-trip test passing.
6. (1:00) Run schemathesis in CI mode — show 0 failures across hundreds of generated requests.
7. (1:30) `curl /fhir/metadata` — CapabilityStatement self-description.
8. (1:00) Open `AuditEvent` table; show one row per request.
9. (0:30) Close: "Task 3 hooks (`meta.audit_id`) are already in the response."

**For Kyle (CTO)**: emphasize OpenAPI completeness, schemathesis CI, ETag/conditional reads, FHIR `_elements`, NDJSON, dual-namespace architecture.
**For Eshan (founder, policy/clinical)**: emphasize completeness scoring, trinary status, provenance round-trip, BPS field coverage taxonomy, ASAM and TJC pre-computed views.

## Caveats

- "Extraction accuracy" is the eval lens but the brief leaves it vague; the trinary `status` field is my interpretation of what graders mean by "captures all relevant fields." Confirm by reading the rubric one more time before shipping.
- The character-offset Provenance extension is custom (no FHIR R4 standard exists). Document it in the README to forestall "this isn't FHIR" objections.
- Synchronous `$export` is technically non-conformant with the Bulk Data IG (which mandates async 202 + polling). Acknowledge in README; this is a deliberate scope choice.
- Epic/Cerner production behavior is gathered from public docs; behavior varies per tenant. Patterns here are informed-by, not equivalent-to, those servers. Epic's `/Specifications` page renders content dynamically, so element-level table behavior should be confirmed against a live sandbox before claiming full conformance.
- `serialize_by_alias=True` default in Pydantic v3 is anticipated but not committed by Pydantic maintainers — set it explicitly today regardless.
- The 34-hour estimate assumes Phase 1 is solid. If `ExtractedObservation` rows lack stable char offsets, budget +6 hours to backfill.

### Completion checklist

| Item | Covered |
|---|---|
| JSON response shape (FHIR vs custom, envelope, pagination, errors, IDs) | ✓ |
| Extraction taxonomy + trinary completeness | ✓ |
| REST patterns (auth, audit, ETag, compression, idempotency) | ✓ |
| Testing strategy (golden, schema, schemathesis, provenance round-trip) | ✓ |
| Reference architectures (Epic/Cerner/Metriport/Eleos/Synthea/Health Gorilla/1upHealth/Particle) | ✓ |
| Pydantic + FastAPI best practices | ✓ |
| Concrete endpoint inventory table | ✓ |
| Canonical response shape with example JSON | ✓ |
| Roadmap with per-endpoint time estimates | ✓ |
| Code snippets (audit, ETag, Bundle, NDJSON, _elements, Provenance) | ✓ |
| Pitfalls section | ✓ |
| Submission polish (README, sample JSON, demo) | ✓ |
| Differentiator callouts (provenance, trinary status, dual-surface) | ✓ |
