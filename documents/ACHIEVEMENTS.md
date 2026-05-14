# Achievements — Phase 1 (in flight)

**Project:** Perspectives Health Intern Technical Assessment
**Phase:** 1 of 3
**Author:** Adrian
**Last updated:** 2026-05-13

This document summarizes what has been built, decided, and produced so far on
Phase 1. Use it as raw material for a submission writeup, a README narrative,
or a Loom script — it is meant to be reviewer-facing.

---

## Phase 1 milestone status

| ID | Milestone | Status | Notes |
|---|---|---|---|
| —  | PRD review + finalization | ✅ done | `documents/phase_1_PRD.md` flipped Draft → Approved |
| M0 | Persona document | ✅ done | `app/synthetic/persona.yaml`, all acceptance checks pass |
| M1 | SimplePractice sandbox + templates | ✅ done | Essential trial; `[SOAP]`/`[DAP]`/`[DSAP]` note templates + `[BPS] Intake Assessment` (21 sections) |
| M1.5 | Chart-text generator | ✅ done | `app/synthetic/generate_chart_text.py` — added mid-stream to compress M2 |
| M2 | Author chart in SimplePractice | 🟡 in progress | Paste-and-click pass through SP using generated text |
| M3 | Data Export ZIP commit | ⏳ pending | After M2 completes |
| M4 | Repo scaffold | ⏳ pending | Can start in parallel with M2/M3 |
| M5 | DB schema + migration | ⏳ pending | |
| M6 | Ingestion pipeline | ⏳ pending | |
| M7 | FHIR mapping + Bundle endpoint | ⏳ pending | |
| M8 | Read API surface | ⏳ pending | |
| M9 | Tests + face-validity checklist | ⏳ pending | |
| M10 | README + demo | ⏳ pending | |

---

## What has been delivered

### 1. PRD finalized — `documents/phase_1_PRD.md`

Status flipped from `Draft` → `Approved` on 2026-05-13 after a structured
review against the brief, the research playbook (`documents/phase_1.md`), and
the implementation plan. Substantive edits:

- **Promoted Q1–Q3 from "Open Questions" to functional requirements.**
  Specifically:
  - FHIR Bundle now includes an `Observation` resource for **every** scale
    extraction (not only headline totals).
  - `ExtractedObservation.extraction_method` is a **per-field** flag that
    defaults to `regex` and flips to `llm` only on regex miss — making the
    regex-first / LLM-fallback policy auditable at the row level.
  - Embedding model pinned to `text-embedding-3-small`, configured via
    `EMBEDDING_MODEL` so an open-source embedder can be swapped in without
    code change (relevant for HIPAA-sensitive deployments).
- **Pinned a DSAP definition.** The format is a minority convention with
  genuine disagreement among clinicians over what "Data" means. Defined for
  this project as `Data` (measurable/observable) / `Subjective` (client
  narrative) / `Assessment` / `Plan`, recorded in the template so the
  parser, the reviewer, and the chart author share one contract.
- **Aligned gap lists.** §5.1 and §6.3 now both reference all five gaps
  (G1–G5), including G5 (RC.01.02.01 co-signature anomaly).
- **Added §12 Deliverables & Submission.** Codifies the brief's submission
  contract (Git repo URL + sample JSON, sent to both
  `kyle@perspectiveshealth.ai` and `eshan@perspectiveshealth.ai`) so it
  doesn't get forgotten at the end.
- Minor: fixed reviewer names (Kyle Hyun Woo Jung, CTO); added
  `Last reviewed: 2026-05-13` field.

> Architectural commitments locked at this stage: char-offset provenance on
> every extracted field; pre-computed ASAM-evidence index and TJC-coverage
> matrix at ingest; unified `ClinicalDocument` table with format-preserving
> JSONB sections; FHIR R4 JSON mirror on every relational row; event-sourced
> `AuditEvent` log; pgvector embeddings at ingest. These collectively form
> the "FHIR-shaped substrate" answer to the brief's "you can't just wrap"
> challenge.

### 2. Changelog created — `CHANGELOG.md`

Per project convention in `CLAUDE.md`, every session that produces a change
appends a one-line entry. First entry recorded for the PRD review session.

### 3. M0: Persona — `app/synthetic/persona.yaml`

The single source of truth for the synthetic patient. Drives every
downstream artifact (SP chart authoring, golden tests, ASAM evidence index,
TJC coverage matrix, face-validity checklist).

Persona: **Marcus J. Reyes, 34M**, presenting 2026-05-04 with concurrent
alcohol + benzodiazepine use disorder, MDD recurrent moderate, GAD, and
unstable recovery environment. Referred from ED after a fall while
intoxicated. Recommended ASAM level of care 3.7 on admission, with a planned
step-down to 2.5 at day 8.

Acceptance checks (all passing — validated programmatically):

| Check | Target | Result |
|---|---|---|
| YAML parses cleanly | yes | ✅ |
| All 6 ASAM dimensions covered with evidence | 6/6 | ✅ |
| All 5 intentional gaps (G1–G5) traced to chart locations | 5/5 | ✅ |
| All 7 scales present with item-level scores | 7/7 | ✅ |
| Scale items sum to documented totals | 4/4 verifiable scales | ✅ (PHQ-9=18, GAD-7=15, AUDIT-C=11, CIWA-Ar=12) |
| 3 progress notes (SOAP/DAP/DSAP) blueprinted | 3/3 | ✅ |

Intentional compliance gaps embedded in the persona (these are what the
Phase 3 TJC audit endpoint will surface):

| ID | EP | Mechanism |
|---|---|---|
| G1 | CTS.03.01.09 | PHQ-9 at intake (=18) but never repeated in any of the three progress notes — violates measurement-based-care requirement |
| G2 | CTS.03.01.03 | Note 2 documents peer-support group; treatment plan does not list peer-support as an intervention (golden-thread break) |
| G3 | NPSG.15.01.01 | C-SSRS at intake (Q2-positive); Note 3 plan calls for re-administration prior to step-down but no result is documented |
| G4 | R3-25 | No discharge planning note exists in the 72-hour window after admission |
| G5 | RC.01.02.01 | Note 2 (non-clinical peer specialist author) requires a clinical co-signature; co-sign timestamp anomalous |

G2 was designed to mirror Perspectives Health's own landing-page audit
example verbatim ("Progress Note dated [date] documents a Peer Support
session. However, the current Treatment Plan does not list Peer Support as
an intervention").

### 4. M1: SimplePractice sandbox + templates

- **30-day Essential trial** activated.
- **Three custom progress-note templates** created with bracketed prefixes
  that serve as the format dispatcher for `section_detector.py`:
  - `[SOAP] Progress Note`
  - `[DAP] Progress Note`
  - `[DSAP] Progress Note`
- **`[BPS] Intake Assessment` template** built out to **21 sections** by
  customizing the SP built-in "Biopsychosocial Assessment & SOAP" template.
  Critical sections added that were missing from the built-in:
  - §17 Past Psychiatric History (5 fields: prior diagnoses,
    hospitalizations, prior medications, self-harm, suicide attempts)
  - §18 Mental Status Exam (13 fields, including standalone PHQ-9 and
    GAD-7 verbatim placeholders)
  - §19 Risk Assessment (7 fields: C-SSRS, homicidal ideation, CIWA-Ar,
    COWS, violence, overall risk level, safety plan)
  - §20 Diagnosis (4 fields: primary/secondary/tertiary/differential)
  - §21 Treatment Plan (6 fields: 3 goals + interventions + outcomes +
    discharge criteria) — golden-thread anchor for gap G2
  - Expanded §14 Physical Health (vitals, current medications, allergies,
    recent labs)
  - Expanded §15 Chemical Use History (per-substance breakdown, AUDIT-C
    verbatim, DAST-10 verbatim)

Every field in `persona.yaml` was cross-checked against the template — every
value has a home before paste-time.

### 5. M1.5: Deterministic chart-text generator — `app/synthetic/generate_chart_text.py`

Added mid-stream after questioning whether M2's 2–3 hour estimate was wrong.
It was: that estimate assumed composing clinical prose live inside SP's
editor. By rendering all the chart prose deterministically from
`persona.yaml` first, M2 collapses to a paste-and-click pass.

The generator emits four plain-text files to `app/synthetic/chart_text/`:

| File | Bytes | Field count |
|---|---|---|
| `bps_intake.txt` | 26 KB | 83 fields across 21 sections |
| `note_1_soap.txt` | 2.1 KB | 4 fields (S/O/A/P) |
| `note_2_dap.txt` | 2.0 KB | 3 fields (D/A/P) |
| `note_3_dsap.txt` | 2.3 KB | 4 fields (D/S/A/P) |

Each file is structured as `▶ Field Label` blocks the clinician matches to
SP fields by name, with section banners and decorative rules clearly marked
as paste-skip. Each progress note ends with an `AUTHOR NOTE` footer naming
the intentional gap it embeds (also paste-skip).

**Design choice: deterministic templates, not LLM generation.** Golden tests
in `tests/test_scale_extractor.py` (Phase 1 M9) will assert exact substring
matches against the verbatim scale strings. LLM rephrasing on every run
would silently break those tests. This is the playbook's documented
fallback when LLM nondeterminism appears in clinical content.

---

## Narrative arc for a writeup

Three story beats already have their proof-points in place:

1. **"You can't just wrap SimplePractice."** SP has no public chart API; the
   only sanctioned programmatic path is the Data Export ZIP. The whole
   architecture is designed around that constraint rather than fighting it.

2. **The substrate matters more than the wrapper.** Char-offset provenance,
   per-field extraction-method auditing, pre-computed ASAM-evidence and
   TJC-coverage tables, FHIR R4 JSON mirrors on every row — these design
   choices turn Phase 3's clinical reasoning into a *retrieval* problem
   ("find the supporting span") rather than a *generation* problem
   ("re-read the chart and reason"), which is faster, cheaper, and far more
   explainable to a TJC surveyor.

3. **Clinical realism is not optional.** The persona's intentional gaps are
   the same shape as Perspectives' own landing-page audit examples. The
   chart isn't just "synthetic data" — it's an instrument designed to make
   the audit endpoint demonstrate something interesting.

---

## Pointers (for the writeup, in case useful)

- Brief: `documents/Technical task 5-8.pdf` (3 pages)
- Research playbook: `documents/phase_1.md` (citations, design rationale)
- PRD (approved): `documents/phase_1_PRD.md`
- Implementation plan: `documents/phase_1_implementation_plan.md`
- Persona (SoT): `app/synthetic/persona.yaml`
- Chart-text generator: `app/synthetic/generate_chart_text.py`
- Generator output: `app/synthetic/chart_text/`
- Changelog: `CHANGELOG.md`
