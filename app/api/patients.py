"""
Patient-facing read API.

Exposes a patient's demographics, intake assessment, extracted observations,
ASAM evidence index, and TJC coverage matrix (Phase 2 PRD §5.3). Every route
hangs off the ``{patient_id}`` path parameter and uses the ``PatientDep``
dependency, which 404s a missing patient before the handler runs.

Every route also requires ``X-API-Key`` (Phase 2 PRD §5.11): Phase 1 only
guarded the ingest endpoints; Phase 2 extends the key to every read so the
audit-on-read story has a principal on every request.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select

from app.api.chart_schemas import (
    AsamSummary,
    Author,
    ChartCoding,
    ChartIntake,
    ChartMeta,
    ChartObservation,
    ChartPatient,
    ChartRead,
    LoincCoding,
    PatientIdentifier,
    PatientName,
    TimelineNote,
    TjcCoverageSummary,
)
from app.api.completeness import classify_all, completeness_score
from app.api.deps import PatientDep, SessionDep, audit_read
from app.api.schemas import (
    AsamDimensionRead,
    IntakeRead,
    ObservationRead,
    PatientRead,
    ProvenanceRead,
    ScaleRead,
    SectionRead,
    SectionStatus,
    TjcCoverageRead,
)
from app.core.security import require_api_key
from app.db.models import (
    AsamEvidence,
    ClinicalDocument,
    ExtractedObservation,
    Patient,
    TjcCoverage,
)
from app.fhir.mappers import _CHART_TZ, LOINC_DOCUMENT_TYPE
from app.ingest.seed import load_seed

# Phase 2 extraction version stamped onto every /chart response's meta. Bumped
# whenever the response shape or the per-section/per-observation extraction
# logic changes in a reviewer-visible way. Decoupled from the pyproject
# version because the API contract evolves on a separate cadence.
EXTRACTION_VERSION = "phase2-v1.0.0"

# Code-system URIs for the canonical chart observations. ``internal`` covers
# the entity-tagger rows (substances/medications/diagnoses), which are not
# emitted by /chart -- only LOINC-coded scales are. The map is exhaustive so
# a future expansion does not silently fall through.
_CODE_SYSTEM_URI: dict[str, str] = {
    "LOINC": "http://loinc.org",
    "SNOMED": "http://snomed.info/sct",
    "RxNorm": "http://www.nlm.nih.gov/research/umls/rxnorm",
    "internal": "http://perspectiveshealth.ai/CodeSystem/extracted",
}

router = APIRouter(
    prefix="/api/v1/patients",
    tags=["patients"],
    dependencies=[Depends(require_api_key), Depends(audit_read)],
)


def _patient_read(patient: Patient) -> PatientRead:
    """Project a Patient row into the read schema (shared by list and detail)."""
    return PatientRead(
        id=patient.id,
        external_id=patient.external_id,
        given_name=patient.given_name,
        family_name=patient.family_name,
        birth_date=patient.birth_date,
        gender=patient.gender,
        fhir=patient.fhir_json,
    )


@router.get("", response_model=list[PatientRead])
def list_patients(session: SessionDep) -> list[PatientRead]:
    """List every patient in the system -- the discovery endpoint a reviewer
    hits after `POST /ingest/*` to find the freshly-ingested patient's id."""
    return [_patient_read(patient) for patient in session.exec(select(Patient)).all()]


@router.get("/{patient_id}", response_model=PatientRead)
def get_patient(patient: PatientDep) -> PatientRead:
    """Patient demographics plus the stored canonical FHIR R4 Patient resource."""
    return _patient_read(patient)


@router.get("/{patient_id}/intake", response_model=IntakeRead)
def get_patient_intake(patient: PatientDep, session: SessionDep) -> IntakeRead:
    """The BPS intake assessment: full raw text, sectioned JSON, and the
    per-section trinary completeness status + document-level score (PRD §5.5).

    Template-listed sections that the chart did not include are surfaced as
    ``looked_but_missing`` placeholders so a reviewer sees the gap, not a
    silently-absent key.
    """
    intake = session.exec(
        select(ClinicalDocument).where(
            ClinicalDocument.patient_id == patient.id,
            ClinicalDocument.document_type == "bps_intake",
        )
    ).first()
    if intake is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No intake assessment for patient {patient.id}",
        )

    statuses = classify_all(intake.sections, intake.document_type)
    return IntakeRead(
        document_id=intake.id,
        authored_on=intake.authored_on,
        author_name=intake.author_name,
        author_role=intake.author_role,
        raw_text=intake.raw_text,
        sections=_enrich_sections(intake, statuses, with_provenance=False),
        completeness_score=completeness_score(statuses),
    )


def _enrich_sections(
    document: ClinicalDocument,
    statuses: dict[str, SectionStatus],
    *,
    with_provenance: bool,
) -> dict[str, SectionRead]:
    """Build the SectionRead dict for a document under the PRD §5.5 contract.

    For every key in ``statuses`` (union of template + present sections):
      * if the section is stored on the document, project its text + span;
      * otherwise emit a placeholder so the trinary status (looked_but_missing
        / not_assessed) is carried even when there's no text to show.

    ``with_provenance`` attaches a ``ProvenanceRead`` to "found" sections so
    /chart can surface the section's source span uniformly with observation
    provenance. /intake leaves it off -- the section's char_span already
    serves as the citation for that surface.
    """
    enriched: dict[str, SectionRead] = {}
    for key, section_status in statuses.items():
        stored = document.sections.get(key)
        if stored is not None:
            char_span = stored.get("char_span", [0, 0])
            provenance: ProvenanceRead | None = None
            if with_provenance and section_status == "found":
                provenance = ProvenanceRead(
                    document_id=document.id,
                    char_start=char_span[0],
                    char_end=char_span[1],
                    snippet=stored.get("text", "") or "",
                )
            enriched[key] = SectionRead(
                title=stored.get("title", key),
                text=stored.get("text"),
                char_span=char_span,
                status=section_status,
                provenance=provenance,
            )
        else:
            # Template-listed section absent from this chart -- placeholder
            # carries the trinary status; text/char_span/provenance are null.
            enriched[key] = SectionRead(
                title=key.replace("_", " ").title(),
                text=None,
                char_span=[0, 0],
                status=section_status,
                provenance=None,
            )
    return enriched


@router.get("/{patient_id}/observations", response_model=list[ObservationRead])
def get_patient_observations(
    patient: PatientDep, session: SessionDep
) -> list[ExtractedObservation]:
    """A flat list of every observation extracted from the patient's documents.

    Covers both the LOINC-coded scale results and the entity-tagger rows
    (substances / medications / diagnoses), each with char-offset provenance.
    """
    # Two-step join: collect this patient's document ids, then fetch all
    # observations against them in one IN-list query. The earlier per-document
    # loop was N+1 against the document count.
    # select(ClinicalDocument.id) yields raw UUIDs (not rows), so the
    # comprehension element is the id itself -- no .id attribute to access.
    document_ids = list(
        session.exec(
            select(ClinicalDocument.id).where(ClinicalDocument.patient_id == patient.id)
        ).all()
    )
    if not document_ids:
        return []
    return list(
        session.exec(
            select(ExtractedObservation).where(
                ExtractedObservation.document_id.in_(document_ids)  # type: ignore[attr-defined]
            )
        ).all()
    )


@router.get("/{patient_id}/asam-evidence", response_model=list[AsamDimensionRead])
def get_patient_asam_evidence(patient: PatientDep, session: SessionDep) -> list[AsamDimensionRead]:
    """ASAM 4th-edition evidence spans, grouped by dimension."""
    # select(ClinicalDocument.id) yields raw UUIDs (not rows), so the
    # comprehension element is the id itself -- no .id attribute to access.
    document_ids = list(
        session.exec(
            select(ClinicalDocument.id).where(ClinicalDocument.patient_id == patient.id)
        ).all()
    )
    if not document_ids:
        return []
    evidence = list(
        session.exec(
            select(AsamEvidence).where(
                AsamEvidence.document_id.in_(document_ids)  # type: ignore[attr-defined]
            )
        ).all()
    )

    dimension_names = {
        entry["number"]: entry["name"] for entry in load_seed("asam_dimensions.yaml")["dimensions"]
    }
    grouped: dict[int, list[AsamEvidence]] = {}
    for row in evidence:
        grouped.setdefault(row.dimension, []).append(row)

    return [
        AsamDimensionRead(
            dimension=dimension,
            name=dimension_names.get(dimension, ""),
            evidence=grouped[dimension],
        )
        for dimension in sorted(grouped)
    ]


@router.get("/{patient_id}/tjc-coverage", response_model=list[TjcCoverageRead])
def get_patient_tjc_coverage(patient: PatientDep, session: SessionDep) -> list[TjcCoverageRead]:
    """The Joint Commission coverage matrix: one row per EP, with evidence pointers."""
    coverage = session.exec(select(TjcCoverage).where(TjcCoverage.patient_id == patient.id)).all()
    ep_titles = {entry["code"]: entry["title"] for entry in load_seed("tjc_eps.yaml")["eps"]}

    return [
        TjcCoverageRead(
            ep_code=row.ep_code,
            title=ep_titles.get(row.ep_code, ""),
            status=row.status,
            rationale=row.rationale,
            evidence_document_id=row.evidence_document_id,
            evidence_char_start=row.evidence_char_start,
            evidence_char_end=row.evidence_char_end,
        )
        for row in coverage
    ]


# ---------------------------------------------------------------------------
# Canonical GET /api/v1/patients/{id}/chart (Phase 2 PRD §5.2 + §6.1)
# ---------------------------------------------------------------------------


def _loinc_for(doc_type: str) -> LoincCoding:
    """LOINC document-type coding per PRD §6.2; falls back to a stub display.

    Sources its (code, display) pairs from ``app.fhir.mappers.LOINC_DOCUMENT_TYPE``
    so the FHIR DocumentReference.type and the /chart loinc_type stay in sync.
    """
    code, display = LOINC_DOCUMENT_TYPE.get(doc_type, ("11526-1", "Document"))
    return LoincCoding(code=code, display=display)


def _provenance_from(observation: ExtractedObservation, raw_text: str) -> ProvenanceRead:
    """Build a ProvenanceRead by slicing the source document's raw_text.

    The PRD §5.5 round-trip invariant holds by construction here: the snippet
    is *the slice itself*, so ``raw_text[start:end] == snippet`` always.
    """
    return ProvenanceRead(
        document_id=observation.document_id,
        char_start=observation.char_start,
        char_end=observation.char_end,
        snippet=raw_text[observation.char_start : observation.char_end],
    )


def _scale_from(observation: ExtractedObservation, raw_text: str) -> ScaleRead:
    """Build a ScaleRead from a LOINC-coded ExtractedObservation.

    Severity is null at this layer -- a per-instrument severity bucketer
    (e.g. PHQ-9 0..4 minimal, 5..9 mild, ...) is Phase 3 territory. The
    ``items`` slot is also null because Phase 1 stores only the total score,
    not the per-item responses.
    """
    return ScaleRead(
        instrument=observation.display,
        score=observation.value_quantity,
        severity=None,
        status="found",
        items=None,
        provenance=_provenance_from(observation, raw_text),
    )


def _build_chart_observations(
    observations: list[ExtractedObservation],
    raw_text_by_document: dict,  # uuid.UUID -> str
    document_authored_on: dict,  # uuid.UUID -> datetime
) -> list[ChartObservation]:
    """Project LOINC-coded observations into the canonical /chart shape.

    Only LOINC-coded rows make it into the /chart observations list -- the
    entity-tagger rows (substances/medications/diagnoses) are accessible via
    /api/v1/patients/{id}/observations but would clutter the chart's top-level
    list and lack a stable code system for the canonical shape.
    """
    chart_observations: list[ChartObservation] = []
    for observation in observations:
        if observation.code_system != "LOINC":
            continue
        raw_text = raw_text_by_document.get(observation.document_id, "")
        authored_on = document_authored_on.get(observation.document_id)
        chart_observations.append(
            ChartObservation(
                id=observation.id,
                code=ChartCoding(
                    system=_CODE_SYSTEM_URI.get(observation.code_system, ""),
                    code=observation.code,
                    display=observation.display,
                ),
                value_quantity=observation.value_quantity,
                value_string=observation.value_string,
                effective_date=authored_on.date() if authored_on else None,
                provenance=_provenance_from(observation, raw_text),
            )
        )
    return chart_observations


def _build_timeline(documents: list[ClinicalDocument]) -> list[TimelineNote]:
    """Project progress notes (not the intake) into the canonical timeline shape.

    ``sections`` is flattened to a ``label -> text`` dict here: the chart
    summary is meant to be skim-readable, so the trinary status (which lives
    on /intake) is omitted from the timeline summary.
    """
    progress_notes = sorted(
        (doc for doc in documents if doc.document_type != "bps_intake"),
        key=lambda doc: doc.authored_on,
    )
    timeline: list[TimelineNote] = []
    for note in progress_notes:
        sections = {
            key: section.get("text", "")
            for key, section in note.sections.items()
            if isinstance(section, dict)
        }
        timeline.append(
            TimelineNote(
                document_id=note.id,
                date=note.authored_on,
                format=note.document_type.upper(),
                loinc_type=_loinc_for(note.document_type),
                author=Author(name=note.author_name, npi=None),
                sections=sections,
                full_text=note.raw_text,
            )
        )
    return timeline


def _build_intake(
    intake: ClinicalDocument, observations_for_intake: list[ExtractedObservation]
) -> tuple[ChartIntake, float]:
    """Build the canonical ChartIntake plus the document-level completeness score.

    The (intake, score) pair lets the meta block reuse the score without
    re-running ``classify_all`` -- the intake handler and the chart handler
    converge on the same trinary classification via ``_enrich_sections``.
    """
    statuses = classify_all(intake.sections, intake.document_type)
    scales = [
        _scale_from(observation, intake.raw_text)
        for observation in observations_for_intake
        if observation.code_system == "LOINC"
    ]

    chart_intake = ChartIntake(
        document_id=intake.id,
        document_type="biopsychosocial",  # human-readable label per PRD §6.1
        encounter_date=intake.authored_on,
        author=Author(name=intake.author_name, npi=None),
        loinc_type=_loinc_for(intake.document_type),
        full_text=intake.raw_text,
        sections=_enrich_sections(intake, statuses, with_provenance=True),
        scales=scales,
    )
    return chart_intake, completeness_score(statuses)


def _build_asam_summary(evidence: list[AsamEvidence]) -> AsamSummary:
    """Per-dimension evidence counts across all the patient's documents.

    Phase 2 surfaces *counts* (zero or positive integers). Severity
    classification -- the dimension-level risk that drives LOC -- is Phase 3.
    """
    counts: dict[int, int] = {}
    for row in evidence:
        counts[row.dimension] = counts.get(row.dimension, 0) + 1
    return AsamSummary(
        dim_1=counts.get(1, 0),
        dim_2=counts.get(2, 0),
        dim_3=counts.get(3, 0),
        dim_4=counts.get(4, 0),
        dim_5=counts.get(5, 0),
        dim_6=counts.get(6, 0),
    )


def _build_tjc_summary(coverage: list[TjcCoverage]) -> TjcCoverageSummary:
    """Counts + the EP codes of any gaps."""
    gap_codes = [row.ep_code for row in coverage if row.status == "gap"]
    covered = sum(1 for row in coverage if row.status == "satisfied")
    return TjcCoverageSummary(
        covered_eps=covered,
        total_eps=len(coverage),
        gap_eps=gap_codes,
    )


@router.get("/{patient_id}/chart", response_model=ChartRead)
def get_patient_chart(patient: PatientDep, session: SessionDep) -> ChartRead:
    """The canonical Task 2 endpoint (PRD §5.2 + §6.1).

    Returns one consolidated JSON object with the brief's required shape
    (patient + intake + timeline) plus the extraction surplus (observations
    with provenance, scales, ASAM evidence summary, TJC coverage summary,
    trinary completeness score). All fields are deterministic functions of
    the row store -- no LLM at request time.

    Five row-store reads (ClinicalDocuments + per-document observations
    + AsamEvidence + TjcCoverage + Patient via the PatientDep dependency),
    then a single assembly pass.
    """
    documents = list(
        session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == patient.id)
        ).all()
    )

    intake = next((doc for doc in documents if doc.document_type == "bps_intake"), None)
    if intake is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"No intake assessment for patient {patient.id} -- cannot build chart",
        )

    # Single observations query across every document for this patient via
    # IN-list join. AsamEvidence + TjcCoverage are pulled in two parallel
    # queries; the assembly is then pure in-memory work.
    document_ids = [doc.id for doc in documents]
    observations = list(
        session.exec(
            select(ExtractedObservation).where(ExtractedObservation.document_id.in_(document_ids))  # type: ignore[attr-defined]
        ).all()
    )
    asam_evidence = list(
        session.exec(
            select(AsamEvidence).where(AsamEvidence.document_id.in_(document_ids))  # type: ignore[attr-defined]
        ).all()
    )
    tjc_coverage = list(
        session.exec(select(TjcCoverage).where(TjcCoverage.patient_id == patient.id)).all()
    )

    # Indexes used during assembly. raw_text_by_document supplies provenance
    # snippets; document_authored_on supplies the effective_date for each
    # LOINC observation in the canonical shape.
    raw_text_by_document = {doc.id: doc.raw_text for doc in documents}
    document_authored_on = {doc.id: doc.authored_on for doc in documents}

    intake_observations = [obs for obs in observations if obs.document_id == intake.id]
    chart_intake, score = _build_intake(intake, intake_observations)

    timeline = _build_timeline(documents)
    chart_observations = _build_chart_observations(
        observations, raw_text_by_document, document_authored_on
    )
    asam_summary = _build_asam_summary(asam_evidence)
    tjc_summary = _build_tjc_summary(tjc_coverage)

    chart_patient = ChartPatient(
        id=patient.id,
        identifiers=[PatientIdentifier(system="simplepractice", value=patient.external_id)],
        name=PatientName(given=[patient.given_name], family=patient.family_name),
        birth_date=patient.birth_date,
        gender=patient.gender,
        address=None,
        phone=None,
        preferred_language="en",
    )

    # meta.generated_at is the latest source-document timestamp (when this
    # chart was *finalized*), not the request-time wall clock. The latter
    # would defeat the ETag contract: a fresh ``datetime.now()`` on each
    # request would mutate the response bytes and force every If-None-Match
    # to miss. The naive ``authored_on`` is localised to America/Chicago so
    # the serialised offset matches the FHIR datetimes (mappers._fhir_datetime)
    # -- the same wall-clock value, identically labelled across surfaces.
    latest_authored = max(doc.authored_on for doc in documents)
    meta = ChartMeta(
        generated_at=latest_authored.replace(tzinfo=_CHART_TZ),
        extraction_version=EXTRACTION_VERSION,
        completeness_score=score,
        etag=None,  # ETag header (M4) is the source of truth; body field is a mirror
        audit_id=None,
        compliance_check_id=None,
    )

    return ChartRead(
        meta=meta,
        patient=chart_patient,
        intake=chart_intake,
        timeline=timeline,
        observations=chart_observations,
        asam_summary=asam_summary,
        tjc_coverage=tjc_summary,
    )
