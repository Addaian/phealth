# Task 1 Implementation Playbook — Perspectives Health Clinical Informatics Assessment

## TL;DR

- **SimplePractice has no public chart-data API** (their own support documentation states this explicitly); the only sanctioned programmatic path is the **Data Export ZIP** (CSV demographics + per-client folders of PDF chart notes). Design Task 1 around parsing this ZIP — not around scraping or the "SimplePractice Enterprise API," which is a scheduling API only.
- **Build the ingestion layer FHIR-R4-native from day one** (Patient + Encounter + ClinicalImpression + DocumentReference + Observation), store each note as both raw text and a sectioned-JSON view, embed clinically validated scales (PHQ-9, GAD-7, AUDIT-C, CIWA-Ar, COWS, C-SSRS) verbatim in the synthetic notes, and pre-compute extraction scaffolds (section spans, char-offset provenance, ASAM-evidence index, TJC-coverage matrix, embeddings) at ingest. This is the single biggest leverage point against the assessment's "you can't just build a wrapper" warning.
- **Engineer a single patient persona before writing any notes**: a "Marcus Reyes, 34M, alcohol + benzodiazepine use disorder, CIWA-Ar 12 on intake, PHQ-9 18, C-SSRS Q2-positive, unstable housing" persona produces (a) clear evidence across all six ASAM 4th-edition dimensions and (b) intentional TJC compliance gaps (missing 72-hour discharge plan, no measurement-based-care reassessment per CTS.03.01.09, peer-support session not on treatment plan per CTS.03.01.03, no C-SSRS reassessment per NPSG.15.01.01) — giving your Task 3 endpoints something interesting to find.

---

## Key Findings

### A. SimplePractice — the ground truth
- **30-day free trial, no credit card.** Three plans during trial: Starter $49, Essential $79, Plus $99. Intake-form templates are customizable on **all plans**; progress-note and treatment-plan templates are customizable on **Essential and Plus only**. Sign up on Essential.
- **No public API for chart data.** SimplePractice support is explicit: "SimplePractice doesn't currently offer a public-facing API for integrations with other software or platforms." The 2022 "SimplePractice Enterprise API" announcement is a B2B *scheduling* API for EAPs/MCOs to request appointments — it does not expose notes, intakes, or assessments. Do not pitch a solution that depends on it.
- **Data Export is the only legitimate programmatic path.** Settings → Practice → Data export → "Complete" produces a ZIP with (a) a `Contacts/` folder containing CSV demographics and (b) a `Medical_Records/` folder containing one subfolder per client with PDFs of every signed and draft chart note. Exports are logged in the Account Activity → History tab — useful for your audit-trail narrative.
- **Third-party middleware reality.** SimplePractice's published integrations are primarily Stripe (payments), Google/Outlook/iCal (calendar), and AI scribes (Clinically AI, Medsender). There is no general-purpose Zapier or Make.com integration for chart content; their available Zapier triggers are limited to billing/calendar events. So routing chart data through Zapier is not a credible architecture.
- **Tagging features.** SimplePractice supports custom tags on clients and limited categorization on documents — useful as an extraction-friendly marker (e.g., tag a client `study:asam-prototype`), but tags do not export reliably in machine-readable form via the Data Export, so do not architect around them.
- **Underlying stack: Ruby on Rails + PostgreSQL** (per the Medsender integration write-up). Trivia, but reinforces that your own Postgres choice aligns with the upstream.
- **Browser automation note.** Perspectives Health's own product extracts from "any web-based EMR" — and they explicitly say they *don't* use browser agents, but rather form-fill in seconds. Replicating that against SimplePractice's UI in a take-home assessment would violate TOS and is not the right play. Use the sanctioned Data Export and mention in your README that you understand Perspectives' production approach addresses the same constraint via a different (vendor-acceptable to them, by virtue of how their tool runs in the clinician's own browser session) mechanism.

### B. ASAM 4th Edition (published 2023) — the spec your Task 3 must satisfy
Six Dimensions (per ASAM and the Colorado HCPF dissemination summary):
1. **Intoxication, Withdrawal, and Addiction Medications** (now explicitly includes MOUD)
2. **Biomedical Conditions**
3. **Psychiatric and Cognitive Conditions/Complications**
4. **Substance Use-Related Risks**
5. **Recovery Environment**
6. **Person-Centered Considerations** — replaces 3rd-edition "Readiness to Change"; assessed *after* the other five, with readiness now woven across all dimensions

Levels of Care 0.5 → 4.0, with **Level 1.0 Long-Term Remission Monitoring** new in the 4th edition. Risk ratings are now embedded in **Dimensional Admission Criteria**, so synthetic notes need to give the engine enough evidence to assign a risk rating per sub-dimension. ASAM and UCLA's free **Level of Care Assessment Guide** is the operational tool clinicians actually use — model your BPS structure on its sub-dimensions.

### C. Joint Commission CTS standards — what your audit endpoint must check
- **CTS.03.01.09** — "The organization assesses the outcomes of care, treatment, or services provided to the individual served." Effective Jan 1, 2018, requires a *standardized tool* (measurement-based care). It was the #4 most-cited noncompliance for behavioral health surveys at 54.14% (per 2020 Joint Commission Perspectives data).
- **CTS.03.01.03** — "The organization has a plan for care, treatment, or services that reflects the assessed needs, strengths, preferences, and goals of the individual served." Top noncompliance at 63.79%.
- **CTS.02.02.01** — Applies to organizations certified as Behavioral Health Homes (history & physical, comprehensive-assessment requirements). The exact EP 2 wording sits behind the Joint Commission E-dition paywall; treat any verbatim quotation as a paraphrase referenced to the current *Comprehensive Accreditation Manual for Behavioral Health Care*.
- **NPSG.15.01.01** — National Patient Safety Goal for suicide screening. Industry standard validated tool is the **Columbia-Suicide Severity Rating Scale (C-SSRS)** — confirmed by Columbia Lighthouse Project documentation that "Screening all patients for suicidal ideation who are being evaluated or treated for behavioral health conditions as their primary reason for care supports the Joint Commission's National Patient Safety Goal 15.01.01."
- **R3 Report Issue 25** (effective **July 1, 2020**) — eight standards / 14 new or revised Elements of Performance for SUD treatment in the CTS and LD chapters, focused on (a) treating individuals at the *appropriate level of care*, (b) transitions of care and follow-up, and (c) proper use of urine drug testing. **This is the most assessment-relevant change** because it explicitly ties ASAM level-of-care decisions (your Task 3 reasoning endpoint) to TJC compliance (your Task 3 audit endpoint).
- **RC chapter** (RC.01.01.01 completeness/accuracy, RC.01.02.01 authentication, RC.01.03.01 documentation content) — straightforward auditable gaps for your synthetic chart.

### D. Perspectives Health — what you are pitching to
- YC **Summer 2025**, founded 2024 in Chicago. Co-founders **Eshan Dosani** (UChicago, formerly White House Drug Policy team) and **Kyle Hyun Woo Jung** (CTO).
- Per YC's company page: "We're building a suite of AI agents to automate away the tasks that occupy half of the time and resources of behavioral health clinics — documentation, compliance, audits, and more." 25% weekly growth at time of YC profile; 9 pilot clinics, 180 clinicians on track.
- **Their public landing page already shows ASAM levels and TJC-style audit findings in their UI.** Verbatim examples from perspectiveshealth.ai: patient cards tagged "3.1 Clinically Managed Low" and "3.7 Medically Monitored"; audit findings such as *"Progress Note dated 01/11/2026 documents a Peer Support session. However, the current Treatment Plan does not list Peer Support as an intervention"* and *"Patient admitted 01/02/2026. No discharge planning note found within 72 hours of admission as required."* **Your synthetic patient and audit endpoint should produce findings of exactly this shape.** This is the most important single signal in the assessment.
- Their thesis: "Most clinical record software works like Microsoft 95, has no modern APIs, and can't integrate with new tools." Your Task 1 should demonstrate that you've internalized this and have an explicit story for the "no API" reality — namely a FHIR-shaped substrate that bridges the gap.

### E. Competitive landscape — adjacent products to position against
- **ASAM CONTINUUM** — ASAM's own decision-support software implementing the ASAM Criteria. It is a placement tool (computer-guided assessment → level-of-care recommendation), not an EMR substrate. Positioning: your work *feeds* something like CONTINUUM; it is not a replacement for it.
- **Eleos Health** — AI scribe + analytics for behavioral health (passive listening, structured note generation, quality metrics). Adjacent to but not overlapping with what Task 1 builds.
- **Bells AI** — behavioral-health-specific note-writing AI; tightly coupled to compliance language for accreditors and payers. Closest competitor surface to what Perspectives is building; your design rationale should explicitly cite "golden-thread" continuity (treatment-plan ↔ progress-note alignment), which is Bells' marketed strength.
- **Lyssn** — session quality analytics from audio (fidelity to MI, CBT, etc.). Orthogonal to your work but worth a one-line README mention as an example of "the layer above" that benefits from clean structured chart data.
- **Talkspace internal tooling** — proprietary; no useful public documentation, but worth noting as proof that telebehavioral-health unicorns build this stack in-house.

Position your submission as the **FHIR-shaped extraction and audit substrate** underneath all of the above — what Perspectives appears to also be building toward, but for SimplePractice specifically.

### F. Note formats — what actually differs
Confirmed across Headway, SOAPNoteAI, Note Designer, Upheal, Zanda:
- **SOAP** (Subjective / Objective / Assessment / Plan) — most structured; common in medical-handoff and multi-disciplinary contexts. Subjective = client report; Objective = clinician-observable (MSE, vitals, scale scores); Assessment = clinical interpretation; Plan = next steps.
- **DAP** (Data / Assessment / Plan) — outpatient mental-health default. Subjective + Objective collapse into one **Data** section. Faster, narrative-friendly.
- **DSAP** (Data / Subjective / Assessment / Plan) — minority format; common in some addiction-treatment programs that want observable Data (CIWA, UDS, vitals, group attendance) separated from the client's narrative report. Treat it as: Data = measurable/observable, Subjective = client report, Assessment = interpretation, Plan = next steps. State your definition in your template — clinicians genuinely disagree on the exact meaning of "Data" in DSAP.

### G. Synthea and synthetic data ecosystem
- **Synthea** (MITRE, Apache-2.0, Java 17+) outputs FHIR R4/STU3/DSTU2, C-CDA, and CSV. 120+ modules including ONC-funded **opioid use disorder modules** developed under the PCOR initiative (per HHS ASPE final report).
- **LLM-augmented Synthea**: arXiv 2507.21123 ("Leveraging Generative AI to Enhance Synthea Module Development") introduces *progressive refinement* — iteratively evaluate an LLM-generated module for syntactic correctness and clinical accuracy, then refine. Cite this in your README as the justification for your generation approach.
- **Limitation**: Synthea produces structured FHIR (Conditions, Encounters, Medications) but **does not generate narrative clinical notes**. You'll still need an LLM- or template-based generator for the BPS intake and progress notes themselves.
- **Style-template corpora to reference** (do **not** ingest in your prototype — licensing): **MIMIC-III/IV** (intensive-care de-identified notes), **n2c2 challenge data** (i2b2 successor; medication, adverse-event, substance-use challenge tasks), **i2b2 NLP shared tasks**. These are the canonical corpora used in clinical NLP research to learn realistic register, abbreviation patterns, and section-header conventions. Cite them in your README as the *source of stylistic priors* for your generator's few-shot prompts (you transcribe patterns, not data).

### H. Clinically validated scales to embed verbatim
- **PHQ-9** — 9-item depression screen, total 0–27
- **GAD-7** — 7-item anxiety screen, total 0–21
- **AUDIT-C** — 3-item alcohol use screen, total 0–12 (positive ≥4 men, ≥3 women)
- **DAST-10** — 10-item drug use screen, total 0–10
- **CIWA-Ar** — Clinical Institute Withdrawal Assessment for Alcohol, revised; total 0–67 (mild <10, moderate 10–18, severe >18)
- **COWS** — Clinical Opiate Withdrawal Scale; total 0–48 (mild 5–12, moderate 13–24, mod-severe 25–36, severe >36)
- **C-SSRS** — Columbia-Suicide Severity Rating Scale; 6 yes/no ideation items + behavior items, with documented validity for predicting suicide attempt and supporting NPSG.15.01.01 compliance

Embedding these *verbatim with totals* in synthetic notes is what makes the chart genuinely useful for downstream extraction — it converts free-text reasoning into a regex/LLM-extractable structured signal.

### I. Open-source EMR fallbacks
- **OpenEMR** (PHP/MySQL, ONC-certified, ~70k+ deployments globally, Docker images available) — best fallback. Custom forms, custom SOAP/DAP-style templates, FHIR R4 connector available.
- **OpenMRS** (Java, originally Regenstrief / Partners In Health) — strong for low-resource settings, modular; weaker for U.S. ambulatory mental-health workflows.
- **LibreHealth EHR** (2016 fork of OpenMRS/OpenEMR) — modern, modular, research-oriented; smaller community.
- **Bahmni** — hospital-oriented distribution built on OpenMRS + OpenERP + OpenELIS; overkill for a behavioral-health outpatient workflow.
- **Metriport** — **not** an EMR; it's an open-source universal API for healthcare data (FHIR + HIE retrieval). Useful to mention as the kind of layer Perspectives sits adjacent to, but does not solve the "simulate a chart" problem.

If SimplePractice access becomes restrictive, pivot to **OpenEMR in Docker** with custom note templates and document the substitution. It's a defensible Plan B that demonstrates breadth.

### J. HIPAA / compliance posture (even with synthetic data)
- Treat your dev environment as if it held PHI: env vars for any API keys, never commit `.env`, run Postgres in a private Docker network, encrypt at rest if your laptop's disk isn't already FileVault/BitLocker.
- Even with synthetic data, **do not** enroll any real person in SimplePractice (would violate their TOS and HIPAA Business-Associate framing).
- Document an explicit deletion path for the synthetic export ZIP in your README — Perspectives sells to TJC-accredited facilities and will notice if you don't think in these terms.
- The system you build is **not** a HIPAA-covered entity in the prototype state, but if Perspectives integrates this work into their stack, a **BAA with SimplePractice** would be required for any production data flow. Note this explicitly as a "production prerequisite, not Task 1 scope."

---

## Details — The Implementation Playbook

### Step 0 — Persona document (write this FIRST, ~45 minutes)
Before touching SimplePractice or writing any code, draft a one-page persona YAML. Example skeleton:

> **Marcus J. Reyes, 34yo cisgender male**, presenting for assessment 2026-05-04. Referred by ED after a fall while intoxicated. ETOH ~14 standard drinks/day x 4 years; daily alprazolam 1 mg x 18 months (originally prescribed, now obtained illicitly). Last drink 36 h ago. Reports tremor, sweats, sleep <2 h. **CIWA-Ar on arrival = 12** (moderate). UDS positive for benzodiazepines, ethanol, cannabis. **Dim 1**: moderate-to-severe risk (concurrent ETOH + benzo withdrawal). **PHQ-9 = 18** (moderately severe), **GAD-7 = 15**, **AUDIT-C = 11**. Hx of TBI 2019. **Dim 2**: hypertension uncontrolled (158/96), elevated LFTs. **Dim 3**: passive SI without plan — **C-SSRS** positive on Q2 only ("wished I were dead"), no current ideation, no prior attempts. **Dim 4**: lost job 6 weeks ago for intoxication at work, DUI pending. **Dim 5**: living with brother (active user), no sober supports identified. **Dim 6**: ambivalent — "I'm here because my brother said so." Recommended level of care: **3.7 Medically Monitored Intensive Inpatient** based on Dim 1 risk and unstable recovery environment.
>
> **Intentional TJC compliance gaps to embed:** (a) No discharge planning note within 72 h of admission (R3-25 transitions-of-care EP); (b) PHQ-9 collected at intake but **never repeated** in any progress note (CTS.03.01.09 measurement-based-care violation); (c) Treatment plan lists CBT and MI but Progress Note 2 documents a peer-support session that is **not on the treatment plan** (golden-thread / CTS.03.01.03 violation — deliberately mirrors Perspectives' own landing-page example); (d) Suicide screen done at intake but **not re-screened prior to level-of-care change** (NPSG.15.01.01 weak point); (e) Notes signed but co-signature timestamps inconsistent (RC.01.02.01 authentication gap).

This persona becomes your single source of truth — every BPS field, every progress note line, and every audit finding traces back to it. **Version-control it as `app/synthetic/persona.yaml` and reference it in every generator.**

### Step 1 — SimplePractice sandbox setup (~1 hour)
1. Sign up for the 30-day free trial; **choose Essential** to unlock note-template customization.
2. Create three custom **note templates**: `[SOAP] Progress Note`, `[DAP] Progress Note`, `[DSAP] Progress Note`. The bracketed prefix becomes your downstream format dispatcher.
3. Customize the default **Biopsychosocial intake form** to include every section enumerated in §"BPS sections" below (Aetna's sample BPS form is the cleanest public template to copy from).
4. Create a single client (Marcus Reyes); send the intake form; complete it as the client.
5. Open Marcus's chart and complete the BPS as the clinician.
6. Author three progress notes on different dates using the three different templates, advancing the persona's clinical trajectory (see "Three progress notes" below).
7. Settings → Practice → Data export → "Complete" → download ZIP → commit to `data/synthetic_export/`. **This ZIP is your Task-2 input contract.**

**Pitfall:** SimplePractice's PDF export embeds note metadata (date, author, type) in the PDF header/footer rather than as machine-readable fields. Plan on `pdfplumber` extracting text by positional region rather than relying on PDF form fields.

### Step 2 — Repository structure
```
perspectives-task1/
├── README.md                       # Lead with persona, "why no API," then schema, then novelty
├── pyproject.toml                  # uv or poetry; Python 3.11+
├── docker-compose.yml              # postgres:16 with pgvector ext, app
├── .env.example                    # all secrets named, never the values
├── alembic/                        # migrations
├── app/
│   ├── main.py                     # FastAPI app, /health, OpenAPI tags
│   ├── core/
│   │   ├── config.py               # pydantic-settings, reads .env
│   │   └── security.py             # API key dependency for ingest/extract
│   ├── db/
│   │   ├── session.py              # async SQLAlchemy engine
│   │   └── models.py               # SQLModel ORM
│   ├── fhir/
│   │   └── mappers.py              # internal ↔ fhir.resources mappers
│   ├── ingest/
│   │   ├── simplepractice_zip.py   # ZIP parser
│   │   ├── pdf_parser.py           # pdfplumber-based note text extraction
│   │   ├── section_detector.py     # SOAP/DAP/DSAP/BPS section splitter
│   │   ├── scale_extractor.py      # PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS, C-SSRS
│   │   ├── entity_tagger.py        # substance/med/dx extractor
│   │   ├── asam_evidence_index.py  # per-dimension evidence span builder
│   │   ├── tjc_coverage_matrix.py  # per-EP evidence span builder
│   │   ├── embeddings.py           # pgvector embedding at ingest
│   │   └── provenance.py           # char-offset bookkeeping helper
│   ├── api/
│   │   ├── ingest.py
│   │   ├── patients.py
│   │   ├── notes.py
│   │   └── fhir.py
│   └── synthetic/
│       ├── persona.yaml
│       ├── generate_bps.py
│       └── generate_notes.py
├── data/
│   ├── synthetic_export/           # the SimplePractice ZIP
│   └── seed/                       # ASAM 4th-ed dimensions, TJC EP catalog fixtures
└── tests/
    ├── test_section_detector.py    # golden tests on the synthetic notes
    ├── test_scale_extractor.py     # golden tests on known scale strings
    ├── test_provenance.py          # every extracted field re-locates in source
    └── test_fhir_roundtrip.py      # internal → FHIR → internal stability
```

**Why this structure scores points:** explicit `synthetic/` (proves you didn't hand-write the chart in an undocumented way), explicit `fhir/` (proves FHIR-readiness), explicit `provenance.py` (proves you anticipated the audit-rationale explainability problem), explicit `asam_evidence_index.py` and `tjc_coverage_matrix.py` (proves you designed Task 1 around Task 3's needs), golden tests in `tests/` (proves clinical literacy and engineering rigor).

### Step 3 — Database schema (FHIR-shaped, PostgreSQL)
A production-defensible schema in SQLModel:

```python
from sqlmodel import SQLModel, Field, Column
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from datetime import datetime, date
from typing import Optional
import uuid

class Patient(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    external_id: str = Field(index=True)               # SimplePractice client ID
    given_name: str
    family_name: str
    birth_date: date
    gender: str
    fhir_json: dict = Field(sa_column=Column(JSONB))   # canonical FHIR Patient

class Encounter(SQLModel, table=True):
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID = Field(foreign_key="patient.id", index=True)
    period_start: datetime
    period_end: Optional[datetime]
    class_code: str        # "AMB" | "IMP" | "EMER" per FHIR v3-ActCode
    type_code: str         # "intake" | "progress" | "discharge" | "loc-reassessment"
    fhir_json: dict = Field(sa_column=Column(JSONB))

class ClinicalDocument(SQLModel, table=True):
    """Unified store for BPS intake AND any progress note.
       Preserves SOAP/DAP/DSAP format-specific structure in `sections`."""
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    patient_id: uuid.UUID = Field(foreign_key="patient.id", index=True)
    encounter_id: uuid.UUID = Field(foreign_key="encounter.id")
    document_type: str                  # "bps_intake" | "soap" | "dap" | "dsap"
    authored_on: datetime
    author_name: str
    author_role: str
    raw_text: str                       # exact text extracted from PDF
    sections: dict = Field(sa_column=Column(JSONB))
    # sections example (SOAP):
    # {"subjective": {"text": "...", "char_span": [0, 412]},
    #  "objective":  {"text": "...", "char_span": [413, 1108]},
    #  "assessment": {"text": "...", "char_span": [1109, 1740]},
    #  "plan":       {"text": "...", "char_span": [1741, 2200]}}
    fhir_document_reference: dict = Field(sa_column=Column(JSONB))
    fhir_clinical_impression: Optional[dict] = Field(default=None, sa_column=Column(JSONB))
    fts: Optional[str] = Field(sa_column=Column(TSVECTOR))
    # embedding via pgvector: list[float] of dim 1536

class ExtractedObservation(SQLModel, table=True):
    """Every clinically meaningful value extracted from any note.
       Backs both Task 2 structured output and Task 3 explainable reasoning."""
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    document_id: uuid.UUID = Field(foreign_key="clinicaldocument.id", index=True)
    code_system: str                # "LOINC" | "SNOMED" | "RxNorm" | "internal"
    code: str                       # e.g. "44261-6" (LOINC PHQ-9 total)
    display: str                    # "PHQ-9 total score"
    value_quantity: Optional[float]
    value_string: Optional[str]
    char_start: int                 # provenance: exact offset in raw_text
    char_end: int
    extraction_method: str          # "regex" | "llm" | "manual"
    confidence: float

class AsamEvidence(SQLModel, table=True):
    document_id: uuid.UUID = Field(foreign_key="clinicaldocument.id", primary_key=True)
    dimension: int = Field(primary_key=True)        # 1..6
    subdimension: Optional[str]
    char_start: int = Field(primary_key=True)
    char_end: int
    snippet: str

class TjcCoverage(SQLModel, table=True):
    patient_id: uuid.UUID = Field(foreign_key="patient.id", primary_key=True)
    ep_code: str = Field(primary_key=True)          # e.g. "CTS.03.01.09"
    status: str                                     # "satisfied" | "gap" | "ambiguous"
    evidence_document_id: Optional[uuid.UUID]
    evidence_char_start: Optional[int]
    evidence_char_end: Optional[int]
    rationale: str

class AuditEvent(SQLModel, table=True):
    """Event-sourced log of every change to any chart record.
       Enables longitudinal ASAM reassessment workflows in Task 3."""
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    occurred_at: datetime = Field(default_factory=datetime.utcnow)
    actor: str
    action: str                     # "ingest" | "extract" | "amend"
    resource_type: str
    resource_id: uuid.UUID
    payload: dict = Field(sa_column=Column(JSONB))
```

**Why this schema design is defensible (put these in your README):**
- **Both raw_text AND sectioned JSON** are stored. Task 3 LLM reasoning can operate on raw_text for nuance; rule-based audits operate on sections for precision. Section parsing happens **at ingest, once**, not at query time, so Task 3 latency is bounded.
- **`fhir_json` columns mirror canonical FHIR R4 resources** (Patient, Encounter, ClinicalImpression, DocumentReference). You aren't *running* a FHIR server, but adding `GET /fhir/Patient/{id}` later is trivial. Directly addresses Perspectives' "legacy EMRs have no modern APIs" thesis.
- **The unified `ClinicalDocument` table handles SOAP/DAP/DSAP diversity** via (a) a `document_type` discriminator and (b) format-specific `sections` JSONB keys. SOAP rows have `subjective`/`objective`/`assessment`/`plan` keys; DAP rows have `data`/`assessment`/`plan`; DSAP rows have `data`/`subjective`/`assessment`/`plan`. The schema is uniform; the JSON contents preserve format-specific structure without losing fidelity. Task 2's `/timeline` endpoint returns the same envelope shape with format-specific section bodies inside — clients dispatch on `document_type`.
- **`ExtractedObservation` with char-offset provenance** is the highest-leverage single design decision. Both ASAM rationale ("Dim 1 rated high because of CIWA-Ar=12 documented in intake at chars 1244–1252") and TJC audit findings ("No PHQ-9 reassessment found in any progress note after intake") become trivially explainable. This is exactly what production clinical-decision-support systems do.
- **`AsamEvidence` and `TjcCoverage` pre-computed tables** make Task 3 a SELECT query rather than a full-document inference, which is faster, cheaper, and more explainable. Task 3 LLM calls become *narration over retrieved evidence*, not raw chart reasoning.
- **`AuditEvent` event-sourced log** gives you longitudinal queryability for free — exactly what ASAM 4th-edition reassessment requires.
- **TSVECTOR + pgvector embeddings** give Task 3 both BM25-style keyword search and semantic retrieval over notes; recommend `text-embedding-3-small` (cheap) embedded at ingest.

### Step 4 — Synthetic BPS content (the load-bearing clinical document)

**BPS intake required sections** (synthesized from Aetna's sample BPS form, ICANotes guidance, and ASAM 4th-edition assessment guidance):

| Section | Must include |
|---|---|
| Demographics | Name, DOB, gender, contact, language, race/ethnicity, marital status, emergency contact |
| Presenting problem / HPI | Onset, duration, severity, precipitants, prior treatment attempts |
| Past psychiatric history | Prior diagnoses, hospitalizations, medications (date, dose, frequency, prescriber, efficacy), self-harm hx |
| **Substance use history** | For every substance: dates of first/last use, route, frequency, duration, amounts, withdrawal hx, prior treatment episodes (formal + informal). Embed **AUDIT-C and DAST-10** scores. Load-bearing for ASAM Dim 1 and Dim 4. |
| Medical history | Conditions, surgeries, medications, allergies, recent vitals, labs (LFTs/UDS) — feeds ASAM Dim 2 |
| Family / social | Family psych and SUD hx, relationships, employment, housing, legal — feeds ASAM Dim 5 |
| Mental Status Exam | Appearance, behavior, speech, mood/affect, thought process/content, perception, cognition, insight, judgment |
| **Risk assessment** | **C-SSRS** (suicide), homicidal ideation, withdrawal risk (**CIWA-Ar** for alcohol/benzo, **COWS** for opioids), violence risk. NPSG.15.01.01 compliance lives here. |
| Diagnosis | DSM-5-TR / ICD-10 codes |
| Treatment plan | Measurable goals with timeframes, interventions, frequency, expected outcomes (CTS.03.01.03 requires this) |

**Embed scales verbatim** — don't just say "PHQ-9 was moderate." Write: *"PHQ-9 administered 2026-05-04: little interest/pleasure 3, feeling down 3, sleep 3, fatigue 2, appetite 2, feeling bad about self 2, concentration 2, slowed/restless 1, thoughts dead 0 — total 18 (moderately severe)."* This converts free text into a regex-extractable structured signal.

### Step 5 — Three progress notes (use the persona's clinical arc)

- **Note 1 (Day 2, SOAP, MD, individual)** — *Subjective:* tremor improving, sleep still poor, "want to leave AMA." *Objective:* CIWA-Ar = 8 (down from 12), BP 142/88, MSE detail. *Assessment:* alcohol withdrawal, moderate, improving on librium taper; benzo withdrawal anticipated. *Plan:* continue librium taper, start gabapentin, MOUD evaluation deferred (no opioids). **Deliberately omit** any update to the treatment plan or a repeat PHQ-9 → CTS.03.01.09 gap.
- **Note 2 (Day 5, DAP, peer specialist, peer-support group)** — *Data:* 90-min group, client attended, contributed once about lost job. *Assessment:* engagement improving, still ambivalent. *Plan:* continue daily groups, schedule family-coordination call. **Deliberately:** peer-support is *not* in the treatment plan generated at intake → golden-thread gap that mirrors Perspectives' own landing-page example verbatim.
- **Note 3 (Day 8, DSAP, LCSW, individual + LOC reassessment)** — *Data:* UDS negative, CIWA-Ar = 2, BP 128/82, no withdrawal x48h. *Subjective:* "ready to step down to PHP, my brother is still using though." *Assessment:* ready clinically per Dim 1 and Dim 2; Dim 5 (recovery environment) remains high risk; recommending 3.7 → 2.5 (IOP) transition rather than full discharge. *Plan:* transfer to PHP, re-administer C-SSRS prior to transfer, identify alternate housing. **Deliberately:** the plan states a C-SSRS reassessment but no corresponding C-SSRS result is documented → NPSG.15.01.01 gap.

### Step 6 — Ingestion pipeline (~3–4 hours)
```python
# app/ingest/simplepractice_zip.py
import zipfile, re
from pathlib import Path
import pdfplumber

NOTE_TYPE_MARKERS = {
    "bps_intake": re.compile(r"Biopsychosocial|Intake Assessment", re.I),
    "soap": re.compile(r"\[SOAP\]|Subjective:.+?Objective:.+?Assessment:.+?Plan:", re.S | re.I),
    "dap":  re.compile(r"\[DAP\]|Data:.+?Assessment:.+?Plan:", re.S | re.I),
    "dsap": re.compile(r"\[DSAP\]|Data:.+?Subjective:.+?Assessment:.+?Plan:", re.S | re.I),
}

def ingest_export_zip(zip_path: Path):
    with zipfile.ZipFile(zip_path) as zf:
        # 1. Parse Contacts/*.csv → Patient + FHIR Patient resource
        # 2. Walk Medical_Records/<Client>/*.pdf
        for member in zf.namelist():
            if member.endswith(".pdf"):
                with zf.open(member) as fh, pdfplumber.open(fh) as pdf:
                    raw = "\n".join((p.extract_text() or "") for p in pdf.pages)
                    doc_type = classify_doc(raw)
                    sections = split_sections(raw, doc_type)
                    doc = persist_document(member, raw, sections, doc_type)
                    extract_scales(doc)                    # → ExtractedObservation
                    tag_entities(doc)                      # → ExtractedObservation
                    build_asam_evidence_index(doc)         # → AsamEvidence
                    update_tjc_coverage(doc.patient_id)    # → TjcCoverage
                    embed_sections(doc)                    # → pgvector
```

Section detection is **regex-first** (cheap, deterministic, testable) with **LLM fallback** for malformed notes. Pattern: try regex; if it fails or returns ambiguous spans, call an LLM with the raw text and a JSON-schema-constrained output asking for section spans; persist the result either way and tag the `extraction_method`.

### Step 7 — FastAPI surface area for Task 2
```python
# app/api/ingest.py
@router.post("/ingest/simplepractice-zip", status_code=202)
async def ingest_zip(file: UploadFile, bg: BackgroundTasks):
    path = save_temp(file)
    bg.add_task(ingest_export_zip, path)
    return {"status": "accepted", "filename": file.filename}

# app/api/patients.py
@router.get("/patients/{patient_id}")
@router.get("/patients/{patient_id}/intake")
@router.get("/patients/{patient_id}/timeline")
@router.get("/patients/{patient_id}/observations")
@router.get("/patients/{patient_id}/asam-evidence")
@router.get("/patients/{patient_id}/tjc-coverage")
@router.get("/patients/{patient_id}/fhir-bundle")
```

The `fhir-bundle` endpoint uses the maintained **`fhir.resources`** pydantic library to assemble a transaction Bundle. Any FHIR-aware consumer (a future Task 4, ASAM CONTINUUM's planned FHIR connectors, a HIE) can consume it. The `asam-evidence` and `tjc-coverage` endpoints already do 80% of Task 3's work — Task 3 LLM calls become narration over these retrieved evidence rows.

### Step 8 — Synthetic-data quality control (face-validity check)
Before you submit, run a written QC checklist against your generated chart:

1. **Coherence**: Does every date sequence make sense? (No CIWA-Ar going from 2 back to 12 without explanation.)
2. **Cross-document consistency**: Does the diagnosis in the BPS appear in every progress-note Assessment section? Does the treatment plan list interventions that the progress notes then reference (the "golden thread")?
3. **Scale plausibility**: Are PHQ-9 item-level scores summing correctly to the total? Are CIWA/COWS sub-scores in valid ranges?
4. **Six-dimension coverage**: Walk each ASAM dimension and confirm at least one BPS sentence and at least one progress-note sentence provides evidence.
5. **TJC gap audit**: Walk each EP you plan to test and confirm the chart has *both* clearly satisfied EPs and clearly missing EPs — a chart that is too clean fails to demonstrate the audit endpoint.
6. **Clinician sanity check**: If you know any licensed counselor/social worker/psychiatrist, ask them to read the chart for "does this read like a real chart?" (5 minutes; this is the cheapest, highest-value QC step.)
7. **De-identification check**: Confirm no real PHI accidentally leaked into the persona (names, addresses, dates that map to a real person you know).

Persist this checklist as `tests/face_validity_checklist.md` and check it into the repo.

### Step 9 — Python library inventory (concrete, version-pinned)
- **FastAPI** + **Uvicorn** (API layer)
- **SQLModel** (recommended over raw SQLAlchemy for this size) or **SQLAlchemy 2.x** if you prefer; **Alembic** for migrations
- **psycopg[binary]** v3 (Postgres driver)
- **pgvector** (optional, for embeddings) — `pip install pgvector` + Postgres extension
- **Pydantic v2** + **pydantic-settings** (validation + env-var loading)
- **fhir.resources** (FHIR R4 pydantic models)
- **pdfplumber** (note-text extraction; preferred over PyPDF2 for layout-aware text)
- **pypdf** (lightweight metadata fallback)
- **python-multipart** (FastAPI file upload)
- **httpx** (any outbound HTTP)
- **openai** or **anthropic** SDK (for LLM generation and LLM-fallback section detection) — wrap behind an interface
- **scispaCy** + **`en_core_sci_sm`** (optional clinical NER) or stick to LLM-based entity extraction
- **LangChain** or **LlamaIndex** — *optional*; only add if you're doing RAG over notes in Task 3. For Task 1 alone, plain pgvector + a thin retrieval helper is enough and avoids dependency bloat.
- **pytest** + **pytest-asyncio** + **httpx.AsyncClient** (testing)
- **ruff** + **mypy** (lint/type-check)
- **uv** or **poetry** (package management)

### Step 10 — Environment & secrets management
- `.env.example` checked in with **named** but **unvalued** secrets: `DATABASE_URL=`, `OPENAI_API_KEY=`, `INGEST_API_KEY=`, `EMBEDDING_MODEL=text-embedding-3-small`.
- `.env` in `.gitignore`. Real values local only.
- `app/core/config.py` uses `pydantic-settings.BaseSettings` to read `.env` with strict typing — fail-fast on startup if a required secret is missing.
- For the reviewer demo, ship a `.env.demo` with the OpenAI key removed and document how to substitute their own.
- Database: docker-compose runs Postgres on a private bridge network with a strong randomized password (generated by a `make seed` target, written to a local `.env.local` that is also gitignored).
- LLM costs: cap with a `MAX_LLM_CALLS_PER_INGEST` env var and short-circuit. Documented in the README as production prudence.

---

## Recommendations — Step-by-Step with Time Estimates

| # | Step | Time | Decision threshold |
|---|---|---|---|
| 0 | One-page persona YAML | 45 min | Every ASAM dimension and TJC gap must trace to a persona fact |
| 1 | SimplePractice trial + 3 custom note templates + customized BPS form | 1 h | If template editor is blocked on Starter, upgrade to Essential within trial |
| 2 | Write BPS intake + 3 progress notes inside SimplePractice as clinician | 2–3 h | Must contain ≥5 verbatim scale results across the chart |
| 3 | Data export → commit ZIP to `data/synthetic_export/` | 15 min | Verify ZIP contains per-client PDF subfolders |
| 4 | Repo scaffold, docker-compose, Alembic init | 1 h | `docker compose up && pytest` passes on empty schema |
| 5 | SQLModel schema + first migration | 1.5 h | Schema serializes to valid FHIR R4 (validate with `fhir.resources`) |
| 6 | ZIP parser + PDF text extraction | 2 h | All notes ingest with non-empty `raw_text` |
| 7 | Section detector (regex-first, LLM fallback) + golden tests | 2 h | 100% of synthetic notes pass; document failure modes |
| 8 | Scale extractor + golden tests | 2 h | PHQ-9, GAD-7, AUDIT-C, CIWA-Ar, COWS, C-SSRS all extracted with offsets |
| 9 | ASAM-evidence index + TJC-coverage builder | 2 h | Hand-verify all 6 dimensions have ≥1 evidence span and ≥3 EPs have status set |
| 10 | FastAPI endpoints + OpenAPI docs + API-key auth | 2 h | All Task-2 contract endpoints return; `/docs` is clean |
| 11 | FHIR Bundle endpoint with `fhir.resources` | 1.5 h | Bundle validates against the FHIR R4 schema |
| 12 | README + ASCII architecture diagram + design-decisions doc + face-validity checklist | 1.5 h | Reviewer can read in <10 min and understand novelty |
| 13 | Loom demo (5 min) showing chart → ingest → query → FHIR bundle | 30 min | Show, don't tell |

**Total: ~20–22 focused hours.** Budget more if Synthea or FHIR is new to you.

### Staged decision points (what would change the plan)
- **SimplePractice template editor blocked on Starter** → upgrade to Essential mid-trial (free during 30 days).
- **PDF parsing brittle** → don't go down an OCR rabbit hole; SP exports text-layer PDFs. Fall back to copy-paste of the rendered chart view into a `.txt` and ingest that. Document the choice.
- **Time pressure before FHIR Bundle endpoint** → ship the internal schema with FHIR JSON columns populated and skip the endpoint. The schema decision is worth more points than the endpoint.
- **LLM generation producing inconsistent content** → revert to template-driven generation seeded from the persona YAML. Clinical realism > generative novelty in this assessment.
- **SimplePractice trial inaccessible at submission time** → fall back to **OpenEMR in Docker** with custom note templates and document the substitution explicitly. Defensible Plan B that shows breadth.
- **Reviewer asks "why not just scrape?"** → answer: TOS, fragility against UI changes, and the Data Export is purpose-built for exactly this. (Mention that Perspectives' own production approach is in-browser and complementary.)

### What separates a great submission from a wrapper — your README "Novel Improvements" section

1. **FHIR-R4-native bridge from a non-FHIR EMR.** Every chart record persists in dual form (internal relational + canonical FHIR JSON). Directly addresses the Perspectives thesis that "legacy EMRs have no modern APIs."
2. **Char-offset provenance on every extracted field.** Both ASAM rationale and TJC audit findings cite back to exact substrings. Table-stakes for clinical AI explainability; differentiates from hallucination-prone wrappers.
3. **Two-stage storage: raw_text + sectioned JSON.** Enables both LLM-on-raw (nuance, Task 3 reasoning) and rule-based-on-structured (precision, Task 3 audit). Section parsing happens once at ingest, not on every query.
4. **Event-sourced AuditEvent log.** Makes longitudinal ASAM reassessment workflows queryable — exactly what ASAM 4th-edition explicitly requires.
5. **Pre-computed ASAM-evidence index and TJC-coverage matrix at ingest.** Task 3 endpoints become retrieval problems, not generation problems — faster, cheaper, more explainable.
6. **Verbatim embedded scales (PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS, C-SSRS).** Demonstrates clinical literacy and gives downstream reasoning genuinely parseable evidence, not paraphrases.
7. **Intentional TJC gaps mirroring Perspectives' marketing.** Your audit endpoint produces findings of literally the same shape as their landing-page examples — referenceable in your README as design inspiration.
8. **Persona-driven LLM generation with progressive refinement** (cite arXiv 2507.21123) seeded by stylistic priors from MIMIC/n2c2/i2b2 *patterns* (not data). Justifies clinical coherence rather than just verbosity.
9. **pgvector embeddings at ingest** for retrieval-augmented Task 3 reasoning. Cheaper and faster than re-embedding at query time.
10. **Unified `ClinicalDocument` schema with format-preserving JSONB sections** for SOAP/DAP/DSAP diversity. One table, one envelope, format-specific bodies — Task 2 returns the same shape regardless of source format.
11. **Explicit honesty about the SimplePractice TOS reality.** State plainly: "SimplePractice does not offer a public chart-data API; we use the sanctioned Data Export ZIP as our ingestion source. Perspectives' production browser-side approach addresses the same constraint by a different mechanism."
12. **Production prerequisites called out**: BAA with SimplePractice, deletion path for the export ZIP, secrets management, LLM cost caps. Demonstrates that you think in HIPAA-grade operations terms.

---

## Caveats

- **CTS.02.02.01 EP 2 exact wording** could not be retrieved within research budget — the Joint Commission E-dition is paywalled. The standard applies to organizations *certified as Behavioral Health Homes* and concerns history-and-physical / comprehensive-assessment requirements. Treat any verbatim quotation as a paraphrase referenced to the current Joint Commission *Comprehensive Accreditation Manual for Behavioral Health Care* unless you obtain institutional access.
- **ASAM 4th edition** published late 2023; payer adoption is uneven and some states still operate against 3rd edition. State explicitly in your README which edition your synthetic narrative is calibrated against (recommend 4th, since the assessment names it).
- **DSAP** is a minority format. Some clinicians treat "DSAP" as essentially SOAP with "Data" replacing "Objective"; others treat it as DAP with Subjective added back. Pick one definition (recommend: Data = measurable/observable facts, Subjective = client narrative, Assessment = interpretation, Plan = next steps) and **state it in your template**.
- **SimplePractice TOS**: synthetic data in your own trial is fine; do not scrape their UI, do not enroll a real patient, do not test against production. The Data Export feature is the only fully sanctioned programmatic path. Mention this in the README — it's a credibility signal, not a weakness.
- **HIPAA**: even with fully synthetic data, treat your dev environment as if it were live PHI. Env vars for secrets, never commit `.env`, Postgres in a private Docker network, encrypted-at-rest disk. Document deletion path for the export ZIP — Perspectives sells to TJC-accredited facilities and will notice if you don't think in these terms. Note that production integration would require a Business Associate Agreement with SimplePractice (not Task 1 scope, but flag it).
- **MIMIC-III/IV, n2c2, i2b2 corpora**: do **not** ingest these into your prototype — licensing constraints apply (PhysioNet credentialing for MIMIC; data-use agreements for n2c2/i2b2). Reference them as *stylistic priors* only — your LLM prompts can describe the conventions (abbreviations, MSE section conventions, register) without containing copyrighted text.
- **Commercial competitive landscape**: ASAM CONTINUUM (placement-criteria DSS), Eleos Health (AI scribe + analytics), Bells AI (compliance-tuned note generation), Lyssn (session-quality from audio), Talkspace internal tooling — within my research budget I confirmed positioning at a high level but did not deeply source architecture details. Position your work as the **FHIR-shaped extraction and audit substrate** underneath all of them — not a competitor to any specific product, but the layer Perspectives is building toward.
- **The assessment says "you'll quickly learn you can't" just wrap.** Take that literally. If your final README's "Architecture" section can be summarized as "I called the SimplePractice API and stuffed results in Postgres," you've failed the spirit of the test. If it reads "I built a FHIR-shaped extraction substrate over the only sanctioned SimplePractice ingestion channel, with char-offset provenance, event sourcing, pre-computed ASAM-dimension evidence indices, and a TJC-EP coverage matrix — because Tasks 2 and 3 need exactly this — and I designed the synthetic chart to have intentional auditable gaps that mirror Perspectives' own marketing examples" — you've understood the assignment.

---

## Coverage table (against the original brief)

| Requested area | Where addressed |
|---|---|
| SimplePractice sandbox setup, trial length, features | Key Findings A; Step 1 |
| SimplePractice API capabilities and absence | Key Findings A (NO public chart API confirmed) |
| Programmatic access alternatives (CSV/PDF/scraping) | Key Findings A; Step 6 |
| Third-party integrations (Zapier, Make, OAuth) | Key Findings A ("middleware reality") |
| HIPAA considerations with synthetic data | Key Findings J; Step 10; Caveats |
| Open-source EMR alternatives (OpenEMR, Bahmni, Metriport) | Key Findings I; staged decision points |
| Synthetic data realism best practices + Synthea | Key Findings G; Step 4 |
| BPS intake required sections | Step 4 BPS table |
| Substance-use docs supporting ASAM dimensions | Step 4; Steps 0 and 5 |
| Risk screening (C-SSRS, withdrawal, violence) | Key Findings B and H; Step 4 |
| SOAP vs DAP vs DSAP usage | Key Findings F; Step 5 |
| ASAM + TJC signal/gap design | Step 0 persona; Step 5 notes |
| Synthetic data tools (Synthea, MITRE BH modules, LLM, hybrid) | Key Findings G |
| Quality control / face validity | Step 8 |
| Architecture for ingestion (FastAPI + PostgreSQL) | Steps 2–7 |
| PDF parsing strategies and OCR | Step 6; staged decision points |
| Database schema for patients/intake/notes/metadata | Step 3 |
| Storing unstructured note text for Task 2 + Task 3 | Step 3 (raw + sections); Step 7 |
| FHIR R4 compatibility | Step 3; Step 7 fhir-bundle endpoint |
| Format diversity in unified schema | Step 3 ClinicalDocument design |
| LLM-generated narratives with consistency checks | Key Findings G; Step 4; Novel Improvements #8 |
| MIMIC / n2c2 / i2b2 as style templates | Key Findings G; Caveats |
| Persona-driven generation | Step 0; Novel Improvements #8 |
| Embedding validated scales | Key Findings H; Step 4 |
| Realistic clinician writing patterns | Key Findings G (stylistic priors) |
| Workflow innovations on SimplePractice side | Step 1; Key Findings A (tagging) |
| Two-stage parsing | Step 3; Novel Improvements #3 |
| Event-sourced chart-change storage | Step 3 AuditEvent; Novel Improvements #4 |
| Pre-computed extraction scaffolds | Step 7; Novel Improvements #5 |
| FHIR-native storage from non-FHIR EMR | Step 3; Novel Improvements #1 |
| Vector embeddings at ingest | Step 3 (pgvector); Novel Improvements #9 |
| Provenance tracking with char offsets | Step 3 ExtractedObservation; Novel Improvements #2 |
| Hooks/affordances making Tasks 2/3 easier | Step 7 endpoints; Novel Improvements #5 and #10 |
| EMR-to-CDSS integration patterns | Key Findings B and C; Step 7 |
| Commercial-product comparisons (CONTINUUM/Eleos/Bells/Lyssn) | Key Findings E |
| Step-by-step order of operations | Recommendations table |
| Time per step | Recommendations table |
| Pitfalls to avoid | Step 1 pitfall; staged decision points; Caveats |
| Specific Python libraries | Step 9 |
| Repository structure | Step 2 |
| Environment/secrets management | Step 10 |
| Competitive landscape characterization of Perspectives | Key Findings D and E |