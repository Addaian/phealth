# Phase 1 — Implementation Plan

**Companion to:** `documents/phase_1_PRD.md`
**Source playbook:** `documents/phase_1.md`
**Date:** 2026-05-11
**Total estimated effort:** ~20–22 focused hours

This plan sequences Phase 1 into 10 milestones with concrete deliverables, file-level scope, acceptance gates, and decision points where the plan branches if a risk fires. Each milestone is independently runnable — at the end of each, `docker compose up && pytest` should pass.

---

## 1. Architecture at a glance

```
┌─────────────────────────────────────────────────────────────────────────┐
│  SimplePractice (Essential trial)                                       │
│  ──────────────────────────────                                         │
│  • 1 patient: Marcus J. Reyes                                           │
│  • 1 BPS intake (custom form)                                           │
│  • 3 progress notes: [SOAP] [DAP] [DSAP]                                │
│                                                                         │
│       │  Settings → Practice → Data Export → Complete                   │
│       ▼                                                                 │
│  data/synthetic_export/<date>.zip                                       │
│     ├── Contacts/*.csv          (demographics)                          │
│     └── Medical_Records/<Client>/*.pdf   (notes)                        │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼   POST /ingest/simplepractice-zip
┌─────────────────────────────────────────────────────────────────────────┐
│  FastAPI app                                                            │
│  ───────────                                                            │
│  ingest/  ZIP → pdfplumber → classifier → sectioner → scale extractor   │
│           → entity tagger → ASAM evidence indexer → TJC coverage        │
│           builder → embeddings → FHIR mappers                           │
│                                                                         │
│  api/     /patients/{id}/{intake|timeline|observations|                 │
│           asam-evidence|tjc-coverage|fhir-bundle}                       │
└─────────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  PostgreSQL 16 + pgvector                                               │
│  Patient · Encounter · ClinicalDocument(raw_text + sections + fhir_json)│
│  · ExtractedObservation(char_start/end) · AsamEvidence · TjcCoverage    │
│  · AuditEvent                                                           │
└─────────────────────────────────────────────────────────────────────────┘
```

The substrate is FHIR-R4-shaped, not FHIR-served. Every relational row has a sibling `fhir_json` column populated at write time so a future `/fhir/<Resource>/<id>` endpoint is a no-op join.

---

## 2. Milestones

### M0 — Persona document (45 min)
**Deliverable:** `app/synthetic/persona.yaml` — single source of truth.

Captures: demographics, presenting problem, substance-use timeline, scale results (PHQ-9=18, GAD-7=15, AUDIT-C=11, DAST-10, CIWA-Ar=12 on arrival, COWS=N/A, C-SSRS Q2-positive), MSE, dimension-by-dimension ASAM risk evidence, recommended LoC (3.7), and the five intentional compliance gaps (G1–G5) with exactly where each is embedded.

**Acceptance:** every ASAM dimension and every G1–G5 gap is traceable to a `persona.yaml` field.

---

### M1 — SimplePractice sandbox (1 h)
**Steps:**
1. Sign up for the 30-day free trial; choose **Essential** (unlocks note + progress-note template customization).
2. Create custom note templates titled `[SOAP] Progress Note`, `[DAP] Progress Note`, `[DSAP] Progress Note`.
3. Customize the default Biopsychosocial intake form to include every section in PRD §6.1.
4. Create one client: Marcus J. Reyes.

**Acceptance:** templates appear in the note-type picker; intake form contains all 10 sections.

**Decision point — Starter blocks customization:** upgrade to Essential mid-trial (free during 30 days).

---

### M2 — Author the synthetic chart inside SimplePractice (2–3 h)
**Deliverables (inside SimplePractice):**
- BPS intake completed for Marcus, with verbatim scales (per PRD §6.2).
- **Note 1 (Day 2, SOAP, MD):** alcohol withdrawal improving (CIWA-Ar=8↓12), gabapentin added, MOUD eval deferred. *Embed G1: no repeat PHQ-9.*
- **Note 2 (Day 5, DAP, peer specialist):** peer-support group attended. *Embed G2: peer-support not on treatment plan.*
- **Note 3 (Day 8, DSAP, LCSW):** LoC reassessment 3.7 → 2.5; plan calls for C-SSRS re-administration. *Embed G3: no C-SSRS result documented.*
- *Embed G4: no discharge plan note within 72 h.*
- *Embed G5: co-signature timestamp inconsistent on one note.*

**Acceptance:** face-validity checklist (M9) passes against the authored chart.

---

### M3 — Export and commit (15 min)
- Settings → Practice → Data Export → Complete → download ZIP.
- Commit to `data/synthetic_export/2026-05-11.zip`.
- Verify ZIP contains `Contacts/*.csv` and `Medical_Records/Marcus Reyes/*.pdf`.

**Acceptance:** `unzip -l data/synthetic_export/2026-05-11.zip` shows expected layout; PDFs open as text-layer (not image-only) — verifiable by `pdfplumber` extracting non-empty text.

**Decision point — PDFs are image-only:** skip OCR; document the SP setting that produces text-layer PDFs, or fall back to copy-paste-to-`.txt` per the playbook.

---

### M4 — Repo scaffold (1 h)
**Deliverables:**
```
perspectives-task1/
├── pyproject.toml              # uv-managed, Python 3.11+
├── docker-compose.yml          # postgres:16 + pgvector ext, app service
├── .env.example
├── .gitignore                  # excludes .env, *.zip is allow-listed under data/
├── alembic/
├── app/
│   ├── main.py
│   ├── core/{config.py, security.py}
│   ├── db/{session.py, models.py}
│   ├── fhir/mappers.py
│   ├── ingest/{...}             # stubs per playbook §Step 2
│   ├── api/{ingest.py, patients.py, notes.py, fhir.py}
│   └── synthetic/persona.yaml
├── data/{synthetic_export/, seed/}
└── tests/
```

`docker compose up -d postgres && alembic upgrade head && pytest -q` succeeds on the empty schema.

**Acceptance:** CI-clean scaffold; `GET /health` returns `{"status":"ok"}`.

---

### M5 — Schema + first migration (1.5 h)
**Tables (per PRD §5):** `Patient`, `Encounter`, `ClinicalDocument`, `ExtractedObservation`, `AsamEvidence`, `TjcCoverage`, `AuditEvent`.

Key design points:
- `ClinicalDocument` is the **unified table** for BPS + all progress notes; `document_type` discriminator + JSONB `sections` preserve SOAP/DAP/DSAP shape.
- `fhir_json` JSONB column on Patient/Encounter/ClinicalDocument — populated at write time, mirrors canonical FHIR R4.
- `ExtractedObservation` carries `(char_start, char_end, extraction_method, confidence)` — the provenance backbone.
- `AsamEvidence` composite PK `(document_id, dimension, char_start)`.
- `TjcCoverage` composite PK `(patient_id, ep_code)`; `status ∈ {satisfied, gap, ambiguous}`.
- pgvector extension enabled; `ClinicalDocument.embedding vector(1536)`.
- TSVECTOR `fts` column on `ClinicalDocument` for BM25-style search.

**Acceptance:** `alembic upgrade head` succeeds; a manual insert of a minimal Patient + Encounter + ClinicalDocument validates against `fhir.resources` pydantic models.

---

### M6 — Ingestion pipeline (3–4 h)
File-by-file scope:

| File | Responsibility | Inputs → Outputs |
|---|---|---|
| `app/ingest/simplepractice_zip.py` | Top-level orchestrator | ZIP path → ingest job |
| `app/ingest/pdf_parser.py` | `pdfplumber`-based text extraction with positional regions for date/author header | PDF → `raw_text`, `metadata` |
| `app/ingest/section_detector.py` | Regex-first format classifier + section splitter; LLM fallback gated by `MAX_LLM_CALLS_PER_INGEST` | `raw_text`, `doc_type` → `{section_key: {text, char_span}}` |
| `app/ingest/scale_extractor.py` | Pattern-matched extractors for PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS, C-SSRS | sections → `ExtractedObservation[]` with LOINC codes |
| `app/ingest/entity_tagger.py` | Substance / medication / diagnosis spotting (regex + dictionary; optional scispaCy) | sections → `ExtractedObservation[]` |
| `app/ingest/asam_evidence_index.py` | Per-dimension keyword + scale-result hooks; emits spans | document + observations → `AsamEvidence[]` |
| `app/ingest/tjc_coverage_matrix.py` | EP-by-EP rules; sets `satisfied`/`gap`/`ambiguous` with evidence pointer | patient's documents + observations → `TjcCoverage[]` |
| `app/ingest/embeddings.py` | `text-embedding-3-small` per section, written to pgvector | section text → vector |
| `app/ingest/provenance.py` | Helper for char-offset bookkeeping across cleaning steps | normalized text + original → offset map |

**Idempotency:** ingester computes `sha256(raw_text)` per document; re-ingest is a no-op when hash matches.

**Acceptance:**
- All 4 documents (1 BPS + 3 notes) ingest with non-empty `raw_text`.
- All 7 scales extract from the chart with non-null `(char_start, char_end)`.
- All 6 ASAM dimensions have ≥1 `AsamEvidence` row.
- `TjcCoverage` contains a row per EP in the seed catalog with G1–G5 all flagged `gap`.

---

### M7 — FHIR mapping + Bundle endpoint (1.5 h)
- `app/fhir/mappers.py`: internal model → `fhir.resources` model conversions for `Patient`, `Encounter`, `ClinicalImpression` (per progress note), `DocumentReference` (per chart document), `Observation` (per `ExtractedObservation` with a LOINC code).
- `GET /patients/{id}/fhir-bundle` assembles a transaction Bundle.

**Acceptance:** the Bundle JSON validates round-trip through `fhir.resources` parsers; `Bundle.entry[*].fullUrl` is unique; the Bundle contains 1 Patient, 1+ Encounters, 4 DocumentReferences, 3+ ClinicalImpressions, and N Observations equal to the count of scale extractions.

---

### M8 — Read API surface (2 h)
Implement and document via FastAPI's OpenAPI:
- `POST /ingest/simplepractice-zip`
- `GET /patients/{id}`
- `GET /patients/{id}/intake`
- `GET /patients/{id}/timeline`
- `GET /patients/{id}/observations`
- `GET /patients/{id}/asam-evidence`
- `GET /patients/{id}/tjc-coverage`
- `GET /patients/{id}/fhir-bundle`
- `GET /health`

API-key dependency on `POST /ingest/*` via header `X-API-Key`; GETs unauthenticated for local demo (documented).

**Acceptance:** `/docs` renders clean; sample JSON saved as `examples/marcus_reyes.json` (this is also the artifact emailed at final submission).

---

### M9 — Tests + face-validity checklist (2 h)
**Test files:**
- `tests/test_section_detector.py` — golden inputs/outputs for SOAP/DAP/DSAP/BPS.
- `tests/test_scale_extractor.py` — golden strings → expected `ExtractedObservation` rows with offsets.
- `tests/test_provenance.py` — for every extracted observation, assert `raw_text[char_start:char_end]` matches the recorded snippet.
- `tests/test_fhir_roundtrip.py` — internal → FHIR → internal stability.
- `tests/test_idempotency.py` — re-ingest same ZIP, assert row counts unchanged.
- `tests/face_validity_checklist.md` — manual checklist run against the chart (coherence, cross-doc consistency, scale plausibility, 6-dimension coverage, gap presence, de-id check).

**Acceptance:** `pytest -q` green; checklist checked into repo with date-stamped pass.

---

### M10 — README + demo (1.5 h)
**README structure (lead with novelty, not setup):**
1. Persona TL;DR (one paragraph).
2. "Why not just wrap SimplePractice's API" — the absent-API reality, the sanctioned Data Export, the FHIR-shaped substrate as the answer.
3. Architecture diagram (ASCII, the one in §1 above).
4. Schema novelty: provenance, two-stage storage, event-sourced audit, pre-computed evidence index.
5. How to run (docker-compose + ingest curl).
6. Intentional TJC gaps catalogue (G1–G5) — mirrors Perspectives' landing-page audit examples.
7. Production prerequisites (BAA, deletion path, secrets, LLM cost caps).
8. Plan B (OpenEMR fallback) — briefly.

**Demo:** a 5-minute Loom showing chart → ZIP → ingest → `/timeline` → `/asam-evidence` → `/fhir-bundle`. Recorded only if reviewer asks; otherwise the README + sample JSON suffice.

**Acceptance:** reviewer can read in <10 minutes and understand the novelty argument.

---

## 3. Sequencing & dependencies

```
M0 (persona) ── M1 (SP setup) ── M2 (chart) ── M3 (export)
                                                   │
M4 (scaffold) ── M5 (schema) ── M6 (ingest) ──────┤
                                  │                │
                                  ├── M7 (FHIR) ───┤
                                  │                │
                                  └── M8 (API) ────┤
                                                   │
                              M9 (tests) ──────────┤
                                                   │
                                              M10 (README)
```

M4 and M5 can start in parallel with M1–M2 by a second contributor; for solo execution, do M0–M3 first to ensure the export artifact exists before writing the parser.

## 4. Tech stack — pinned

```toml
# pyproject.toml essentials
python = ">=3.11,<3.13"
fastapi = "^0.115"
uvicorn = {extras = ["standard"], version = "^0.30"}
sqlmodel = "^0.0.22"
sqlalchemy = "^2.0"
alembic = "^1.13"
psycopg = {extras = ["binary"], version = "^3.2"}
pgvector = "^0.3"
pydantic = "^2.8"
pydantic-settings = "^2.4"
fhir.resources = "^7.1"
pdfplumber = "^0.11"
pypdf = "^4.3"          # metadata fallback
python-multipart = "^0.0.9"
httpx = "^0.27"
openai = "^1.40"        # behind a thin interface
# dev:
pytest = "^8.3"
pytest-asyncio = "^0.24"
ruff = "^0.6"
mypy = "^1.11"
```

## 5. Environment & secrets

`.env.example` (checked in):
```
DATABASE_URL=postgresql+psycopg://phealth:CHANGE_ME@postgres:5432/phealth
OPENAI_API_KEY=
INGEST_API_KEY=CHANGE_ME
EMBEDDING_MODEL=text-embedding-3-small
MAX_LLM_CALLS_PER_INGEST=5
```

`app/core/config.py` uses `pydantic-settings.BaseSettings` — startup fails fast if `DATABASE_URL` or `INGEST_API_KEY` is missing. `OPENAI_API_KEY` is only required if regex section detection misses (LLM fallback path).

## 6. Risks & branch points

| Risk | Trigger | Branch |
|---|---|---|
| SP template editor unavailable | Free trial restrictions | Upgrade to Essential within trial (free) |
| PDF text-layer brittle | `pdfplumber` returns empty text | Skip OCR; copy rendered chart → `.txt`; document |
| LLM nondeterminism in tests | Section detector falls through to LLM in golden tests | Force `MAX_LLM_CALLS_PER_INGEST=0` in test env; goldens must pass regex-only |
| Time-pressure on FHIR Bundle endpoint | M7 slips past day-3 | Ship `fhir_json` columns; defer the `/fhir-bundle` endpoint — schema decision is worth more points than the endpoint |
| SP trial inaccessible at submission | Account flagged / locked | Pivot to OpenEMR in Docker + equivalent custom templates; document substitution |

## 7. What ships at end of Phase 1

Repo with:
- ✅ One synthetic patient chart authored in SimplePractice and exported as ZIP.
- ✅ FastAPI + Postgres service with the schema, ingestion pipeline, and read endpoints above.
- ✅ A FHIR R4 Bundle endpoint backed by `fhir.resources`.
- ✅ Tests (section detection, scale extraction, provenance round-trip, FHIR round-trip, idempotency).
- ✅ Face-validity checklist signed off.
- ✅ README with architecture, novelty argument, gap catalogue, and run instructions.
- ✅ `examples/marcus_reyes.json` — sample JSON response payload.

What Phase 2 inherits: a populated `ExtractedObservation` table + per-document `sections` JSONB + a Phase-2-shaped `/timeline` envelope.
What Phase 3 inherits: a populated `AsamEvidence` table + `TjcCoverage` matrix — Phase 3 LLM work becomes narration over retrieved rows, not raw chart reasoning.
