"""
FHIR R4 API router (Phase 2 PRD §5.3 + §6.2).

Two surfaces co-exist on this module:

  * **Legacy** ``GET /api/v1/patients/{id}/fhir-bundle`` (Phase 1) -- returns
    a single transaction Bundle. Kept for the Phase 1 sample-JSON
    regeneration path; the canonical replacement is ``$everything`` below.

  * **Strict FHIR namespace** ``/fhir/*`` -- per-resource read + search
    endpoints matching FHIR R4 search semantics:
      - ``GET /fhir/Patient/{id}``                 -- read
      - ``GET /fhir/DocumentReference/{id}``       -- read
      - ``GET /fhir/DocumentReference``            -- search (``patient``,
                                                      ``type``, ``_count``,
                                                      ``cursor``)
      - ``GET /fhir/Observation/{id}``             -- read
      - ``GET /fhir/Observation``                  -- search (``patient``,
                                                      ``code``, ``_count``,
                                                      ``cursor``)
      - ``GET /fhir/Provenance``                   -- search (``target``)
      - ``GET /fhir/Patient/{id}/$everything``     -- Synthea-shaped Bundle
      - ``GET /fhir/Patient/{id}/$export``         -- NDJSON Bulk Data stream
      - ``GET /fhir/metadata``                     -- CapabilityStatement
                                                      (auth-free per spec)

Pagination is cursor-based (see ``app/api/pagination.py``). Each search
Bundle carries ``link[]`` with ``relation="self"`` always, plus
``relation="next"`` when more rows remain. Default page sizes (PRD §5.9):
DocumentReference=20, Observation=50. ``_count`` is the FHIR-standard
page-size parameter.

Errors emerge as FHIR ``OperationOutcome`` payloads via ``app/api/errors.py``
which branches on the ``/fhir/`` path prefix.
"""

import json
import uuid
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlmodel import select

from app.api.deps import PatientDep, SessionDep, audit_read
from app.api.pagination import build_searchset_bundle, decode_cursor, encode_cursor
from app.core.security import require_api_key
from app.db.models import ClinicalDocument, Encounter, ExtractedObservation, Patient
from app.fhir.mappers import (
    build_capability_statement,
    build_everything_bundle,
    build_patient_bundle,
    to_fhir_clinical_impression,
    to_fhir_document_reference,
    to_fhir_encounter,
    to_fhir_observation,
    to_fhir_patient,
    to_fhir_provenance,
)

# FHIR R4 wire media type. The exception handler in app/api/errors.py emits
# the same for OperationOutcome -- keeps the surface single-content-type.
_FHIR_JSON = "application/fhir+json"

# Default page sizes per resource type (PRD §5.9). MAX is the hard cap so a
# pathological ``_count=10000`` from a client cannot blow the buffer.
_DEFAULT_COUNT: dict[str, int] = {
    "DocumentReference": 20,
    "Observation": 50,
    "Provenance": 50,
}
_MAX_COUNT = 100


# Router carries no prefix so the legacy /api/v1/patients/{id}/fhir-bundle
# route can co-exist with the new /fhir/* paths. Each route below states its
# full path explicitly.
router = APIRouter(
    tags=["fhir"],
    dependencies=[Depends(require_api_key), Depends(audit_read)],
)

# Auth-free router for /fhir/metadata only. The FHIR R4 spec requires the
# CapabilityStatement to be retrievable without authentication so a client
# can discover whether to authenticate before it tries to call anything else.
# This is a deliberate, documented exception to the Phase 2 §5.11 "auth on
# every read" rule -- the CapabilityStatement contains no PHI.
metadata_router = APIRouter(tags=["fhir"])


def _serialise(resource: Any) -> dict[str, Any]:
    """Render a fhir.resources model as the dict form a Bundle entry expects."""
    return resource.model_dump(mode="json", by_alias=True, exclude_none=True)


def _self_url(request: Request) -> str:
    """The URL the client used, with all query params preserved. Used for link.self."""
    return str(request.url)


def _clamp_count(count: int, default: int) -> int:
    """Bound ``_count`` into ``[1, _MAX_COUNT]`` with ``default`` as the fallback."""
    if count <= 0:
        return default
    return min(count, _MAX_COUNT)


def _paginate(rows: list, count: int, cursor_id: uuid.UUID | None) -> tuple[list, str | None]:
    """Apply cursor-based pagination to an id-sorted row list.

    Returns ``(page_rows, next_cursor)``. ``next_cursor`` is ``None`` when no
    further rows remain after this page. Callers pass an id-sorted ``rows``
    list; we rely on that ordering for the slice to be deterministic.

    MVP limitation: the search handlers above load all matching rows then
    slice in Python. For Marcus's 30-resource chart this is invisible; at
    real patient volumes the limit + cursor predicate should be pushed into
    SQL (``ORDER BY id LIMIT count + 1`` and filter by ``id > cursor``).
    Deferred until the dataset warrants it -- the wire shape (cursor,
    Bundle.link[next]) is forward-compatible with the SQL-side fix.
    """
    remaining = rows
    if cursor_id is not None:
        remaining = [row for row in remaining if row.id > cursor_id]
    page = remaining[:count]
    next_cursor: str | None = None
    if len(remaining) > count:
        next_cursor = encode_cursor(page[-1].id)
    return page, next_cursor


# ---------------------------------------------------------------------------
# Shared patient-compartment loader (Phase 2 review).
# ---------------------------------------------------------------------------


def _load_patient_compartment(
    session: SessionDep, patient_id: uuid.UUID
) -> tuple[list[Encounter], list[ClinicalDocument], list[ExtractedObservation]]:
    """Return the (encounters, documents, LOINC observations) tuple for a patient.

    Three handlers -- /api/v1/patients/{id}/fhir-bundle, $everything, $export
    -- ran the same three SELECTs in copy-pasted form. Centralised here as one
    helper with three queries: Encounters, ClinicalDocuments, and one IN-list
    query across the patient's document_ids for LOINC-coded observations.
    The previous per-document observation loop was N+1 against document count.

    Filtered to ``code_system == "LOINC"`` to match the FHIR Observation
    surface scope (entity-tagger rows live on /api/v1/observations only).
    """
    encounters = list(
        session.exec(select(Encounter).where(Encounter.patient_id == patient_id)).all()
    )
    documents = list(
        session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_id)
        ).all()
    )
    document_ids = [doc.id for doc in documents]
    if not document_ids:
        return encounters, documents, []
    observations = list(
        session.exec(
            select(ExtractedObservation).where(
                ExtractedObservation.document_id.in_(document_ids),  # type: ignore[attr-defined]
                ExtractedObservation.code_system == "LOINC",
            )
        ).all()
    )
    return encounters, documents, observations


def _parse_reference(value: str | None, resource_type: str) -> uuid.UUID | None:
    """Parse a FHIR search reference value: ``ResourceType/{id}`` or bare ``{id}``.

    Returns ``None`` for absent or malformed values -- consistent with the
    cursor policy of "do something, don't 400 on opaque-shaped query strings".
    Shared by the ``patient`` and ``target`` search-parameter handlers.
    """
    if value is None:
        return None
    candidate = value.removeprefix(f"{resource_type}/")
    try:
        return uuid.UUID(candidate)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Legacy: /api/v1/patients/{id}/fhir-bundle (retired in M9)
# ---------------------------------------------------------------------------


@router.get("/api/v1/patients/{patient_id}/fhir-bundle")
def get_patient_fhir_bundle(patient: PatientDep, session: SessionDep) -> dict:
    """Return all of a patient's data as a FHIR R4 transaction Bundle.

    The Bundle contains the Patient, every Encounter, a DocumentReference per
    chart document, a ClinicalImpression per progress note, an Observation
    per LOINC-coded extracted scale result, and a Provenance per Observation
    (M10). Re-derived from the relational rows on each request via
    app.fhir.mappers -- the same mappers the ingester uses, so the served
    Bundle and the stored substrate stay consistent.
    """
    encounters, documents, observations = _load_patient_compartment(session, patient.id)
    bundle = build_patient_bundle(patient, encounters, documents, observations)
    return _serialise(bundle)


# ---------------------------------------------------------------------------
# /fhir/Patient/{id} -- bare resource read
# ---------------------------------------------------------------------------


@router.get("/fhir/Patient/{patient_id}")
def read_patient(patient_id: uuid.UUID, session: SessionDep) -> JSONResponse:
    """Return one FHIR R4 Patient resource."""
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Patient/{patient_id} not found",
        )
    return JSONResponse(_serialise(to_fhir_patient(patient)), media_type=_FHIR_JSON)


# ---------------------------------------------------------------------------
# /fhir/DocumentReference -- read + search
# ---------------------------------------------------------------------------


@router.get("/fhir/DocumentReference/{document_id}")
def read_document_reference(document_id: uuid.UUID, session: SessionDep) -> JSONResponse:
    """Return one FHIR R4 DocumentReference resource."""
    document = session.get(ClinicalDocument, document_id)
    if document is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"DocumentReference/{document_id} not found",
        )
    return JSONResponse(
        _serialise(to_fhir_document_reference(document)),
        media_type=_FHIR_JSON,
    )


@router.get("/fhir/DocumentReference")
def search_document_references(
    request: Request,
    session: SessionDep,
    patient: str | None = Query(default=None, description="Patient/{id} or {id}"),
    type: str | None = Query(default=None, description="Document type (e.g. soap, bps_intake)"),
    count: int = Query(
        default=_DEFAULT_COUNT["DocumentReference"], alias="_count", description="Page size"
    ),
    cursor: str | None = Query(default=None, description="Opaque pagination cursor"),
) -> JSONResponse:
    """Search FHIR DocumentReference resources.

    Filters: ``patient`` (accepts either ``Patient/{id}`` or a bare uuid),
    ``type`` (document_type literal: ``bps_intake`` / ``soap`` / ``dap`` /
    ``dsap``). Both filters are AND'd; absent filters mean "no constraint".

    Pagination is cursor-based (see app/api/pagination.py).
    """
    count = _clamp_count(count, _DEFAULT_COUNT["DocumentReference"])
    cursor_id = decode_cursor(cursor)

    query = select(ClinicalDocument)
    patient_uuid = _parse_reference(patient, "Patient")
    if patient_uuid is not None:
        query = query.where(ClinicalDocument.patient_id == patient_uuid)
    if type is not None:
        query = query.where(ClinicalDocument.document_type == type)

    # Pull all matches sorted by id (UUID order). For Marcus's chart this is
    # 4 rows -- query realism scales with the dataset, not the page size.
    rows = sorted(session.exec(query).all(), key=lambda doc: doc.id)
    total = len(rows)
    page, next_cursor = _paginate(rows, count, cursor_id)

    bundle = build_searchset_bundle(
        resources=[_serialise(to_fhir_document_reference(doc)) for doc in page],
        resource_type="DocumentReference",
        total=total,
        self_url=_self_url(request),
        next_cursor=next_cursor,
    )
    return JSONResponse(_serialise(bundle), media_type=_FHIR_JSON)


# ---------------------------------------------------------------------------
# /fhir/Observation -- read + search
# ---------------------------------------------------------------------------


@router.get("/fhir/Observation/{observation_id}")
def read_observation(observation_id: uuid.UUID, session: SessionDep) -> JSONResponse:
    """Return one FHIR R4 Observation resource (LOINC-coded only)."""
    observation = session.get(ExtractedObservation, observation_id)
    if observation is None or observation.code_system != "LOINC":
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Observation/{observation_id} not found",
        )
    # The Observation resource needs the patient + encounter ids; we get
    # them from the parent document.
    document = session.get(ClinicalDocument, observation.document_id)
    if document is None:
        # Should never happen given the FK, but raise OperationOutcome cleanly.
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail=f"Observation/{observation_id} parent document missing",
        )
    fhir_observation = to_fhir_observation(
        observation, str(document.patient_id), str(document.encounter_id)
    )
    return JSONResponse(_serialise(fhir_observation), media_type=_FHIR_JSON)


@router.get("/fhir/Observation")
def search_observations(
    request: Request,
    session: SessionDep,
    patient: str | None = Query(default=None),
    code: str | None = Query(default=None, description="LOINC code to filter on"),
    count: int = Query(default=_DEFAULT_COUNT["Observation"], alias="_count"),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    """Search FHIR Observation resources (LOINC-coded scales only).

    The entity-tagger rows (SNOMED / RxNorm / internal) are intentionally
    excluded from the strict-FHIR namespace -- only the LOINC-coded scale
    results land here, matching the canonical /chart shape.
    """
    count = _clamp_count(count, _DEFAULT_COUNT["Observation"])
    cursor_id = decode_cursor(cursor)

    query = select(ExtractedObservation).where(ExtractedObservation.code_system == "LOINC")
    if code is not None:
        query = query.where(ExtractedObservation.code == code)

    patient_uuid = _parse_reference(patient, "Patient")
    # patient filter requires a join through ClinicalDocument; resolve via a
    # subquery so we stay in one round-trip.
    if patient_uuid is not None:
        doc_ids = [
            doc.id
            for doc in session.exec(
                select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_uuid)
            ).all()
        ]
        # Empty doc_ids would yield an "IN ()" SQL error; short-circuit instead.
        if not doc_ids:
            empty = build_searchset_bundle(
                resources=[],
                resource_type="Observation",
                total=0,
                self_url=_self_url(request),
                next_cursor=None,
            )
            return JSONResponse(_serialise(empty), media_type=_FHIR_JSON)
        query = query.where(ExtractedObservation.document_id.in_(doc_ids))  # type: ignore[attr-defined]

    rows = sorted(session.exec(query).all(), key=lambda obs: obs.id)
    total = len(rows)
    page, next_cursor = _paginate(rows, count, cursor_id)

    # Building the FHIR Observation needs (patient_id, encounter_id) per row;
    # index the parent documents once so we don't N+1 the database.
    doc_ids_on_page = {obs.document_id for obs in page}
    if doc_ids_on_page:
        document_rows = session.exec(
            select(ClinicalDocument).where(ClinicalDocument.id.in_(doc_ids_on_page))  # type: ignore[attr-defined]
        ).all()
        document_by_id = {doc.id: doc for doc in document_rows}
    else:
        document_by_id = {}

    resources: list[dict[str, Any]] = []
    for observation in page:
        document = document_by_id[observation.document_id]
        resources.append(
            _serialise(
                to_fhir_observation(
                    observation,
                    str(document.patient_id),
                    str(document.encounter_id),
                )
            )
        )

    bundle = build_searchset_bundle(
        resources=resources,
        resource_type="Observation",
        total=total,
        self_url=_self_url(request),
        next_cursor=next_cursor,
    )
    return JSONResponse(_serialise(bundle), media_type=_FHIR_JSON)


# ---------------------------------------------------------------------------
# /fhir/Provenance -- search (resources arrive in M10)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /fhir/Patient/{id}/$everything (M9)
# ---------------------------------------------------------------------------


@router.get("/fhir/Patient/{patient_id}/$everything")
def patient_everything(patient: PatientDep, session: SessionDep) -> JSONResponse:
    """The FHIR R4 Patient/$everything operation.

    Returns one Bundle.searchset containing the patient and every resource
    in their compartment: Encounters, DocumentReferences, ClinicalImpressions
    (one per progress note), Observations (LOINC-coded scales only), and
    Provenance (added in M10 -- empty list passed for now). Reference order
    is parent-before-child so a sequential reader sees the patient first.

    The Phase 1 legacy ``/api/v1/patients/{id}/fhir-bundle`` returns a
    similar Bundle but as a ``transaction`` type; ``$everything`` is the
    canonical FHIR R4 surface and what M12's sample JSON ships against.
    """
    encounters, documents, observations = _load_patient_compartment(session, patient.id)
    bundle = build_everything_bundle(patient, encounters, documents, observations)
    return JSONResponse(_serialise(bundle), media_type=_FHIR_JSON)


# ---------------------------------------------------------------------------
# /fhir/metadata -- CapabilityStatement (auth-free, M9)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /fhir/Patient/{id}/$export (M13) -- synchronous NDJSON stub
# ---------------------------------------------------------------------------

# Media type for FHIR Bulk Data ND-JSON streams. The ETag middleware in
# app/api/etag.py excludes this media type from hashing -- buffering a stream
# to hash it would defeat the streaming.
_FHIR_NDJSON = "application/fhir+ndjson"


def _iter_patient_resources_ndjson(
    patient: Patient,
    encounters: list[Encounter],
    documents: list[ClinicalDocument],
    observations: list[ExtractedObservation],
) -> Iterator[bytes]:
    """Yield one JSON-per-line for every resource in the patient compartment.

    The same data assembly as ``build_everything_bundle`` but as a streaming
    NDJSON generator: each resource is rendered to a single line of compact
    JSON, terminated by ``\\n``. Order matches $everything (parent before
    children) for client-side composability.

    Each yield is bytes so FastAPI's ``StreamingResponse`` does not have to
    re-encode -- the wire output is byte-identical to what a client would
    receive from a production Bulk Data export.
    """
    encounter_by_document = {document.id: document.encounter_id for document in documents}
    document_by_id = {document.id: document for document in documents}

    def line(resource: Any) -> bytes:
        return (json.dumps(_serialise(resource)) + "\n").encode()

    yield line(to_fhir_patient(patient))
    for encounter in encounters:
        yield line(to_fhir_encounter(encounter))
    for document in documents:
        yield line(to_fhir_document_reference(document))
    for document in documents:
        if document.document_type != "bps_intake":
            yield line(to_fhir_clinical_impression(document))
    for observation in observations:
        yield line(
            to_fhir_observation(
                observation,
                str(patient.id),
                str(encounter_by_document[observation.document_id]),
            )
        )
    for observation in observations:
        if observation.code_system != "LOINC":
            continue
        yield line(to_fhir_provenance(observation, document_by_id[observation.document_id]))


@router.get("/fhir/Patient/{patient_id}/$export")
def patient_export(patient: PatientDep, session: SessionDep) -> StreamingResponse:
    """Synchronous FHIR Bulk Data ``$export`` for a single patient.

    Streams the patient compartment as ``application/fhir+ndjson`` -- one
    JSON-per-line, no surrounding Bundle envelope. Demonstrates the wire
    shape of the FHIR Bulk Data IG (https://hl7.org/fhir/uv/bulkdata/);
    production would split this into the async kick-off + status + manifest
    pattern, but the resource shape on the wire is the same.

    The ``Content-Disposition: attachment`` header hints at file download
    semantics so ``curl -O`` and browser handlers do the right thing.
    """
    encounters, documents, observations = _load_patient_compartment(session, patient.id)
    return StreamingResponse(
        _iter_patient_resources_ndjson(patient, encounters, documents, observations),
        media_type=_FHIR_NDJSON,
        headers={
            "Content-Disposition": f"attachment; filename=Patient-{patient.id}.ndjson",
        },
    )


@metadata_router.get("/fhir/metadata")
def capability_statement() -> JSONResponse:
    """Return the FHIR R4 CapabilityStatement for this server.

    Auth-free per spec: a client must be able to discover the server's
    capabilities (including whether/how to authenticate) without sending
    credentials. The body carries no PHI.
    """
    statement = build_capability_statement()
    return JSONResponse(_serialise(statement), media_type=_FHIR_JSON)


@router.get("/fhir/Provenance")
def search_provenance(
    request: Request,
    session: SessionDep,
    target: str | None = Query(
        default=None,
        description=(
            "Filter to Provenance whose target is the given reference, "
            "e.g. ``Observation/{id}`` or bare ``{id}``."
        ),
    ),
) -> JSONResponse:
    """Search FHIR Provenance resources (M10).

    Each LOINC-coded ExtractedObservation produces one Provenance whose
    ``entity.extension`` carries the custom ``text-position-selector``
    extension (char_start / char_end). Filter on ``target`` to drill down
    to the Provenance for one observation.
    """
    target_id = _parse_reference(target, "Observation")

    query = select(ExtractedObservation).where(ExtractedObservation.code_system == "LOINC")
    if target_id is not None:
        query = query.where(ExtractedObservation.id == target_id)

    observations = list(session.exec(query).all())
    if not observations:
        empty = build_searchset_bundle(
            resources=[],
            resource_type="Provenance",
            total=0,
            self_url=_self_url(request),
            next_cursor=None,
        )
        return JSONResponse(_serialise(empty), media_type=_FHIR_JSON)

    # One ClinicalDocument lookup per distinct document_id; cheap for the
    # MVP's 4-doc chart, and bounded N+1 even for larger.
    document_ids = {observation.document_id for observation in observations}
    documents_by_id = {
        document.id: document
        for document in session.exec(
            select(ClinicalDocument).where(ClinicalDocument.id.in_(document_ids))  # type: ignore[attr-defined]
        ).all()
    }

    resources: list[dict[str, Any]] = []
    for observation in observations:
        document = documents_by_id[observation.document_id]
        resources.append(_serialise(to_fhir_provenance(observation, document)))

    bundle = build_searchset_bundle(
        resources=resources,
        resource_type="Provenance",
        total=len(resources),
        self_url=_self_url(request),
        next_cursor=None,
    )
    return JSONResponse(_serialise(bundle), media_type=_FHIR_JSON)
