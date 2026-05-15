"""
ASAM Level-of-Care prediction endpoints.

Two routes:

  * ``POST /api/v1/patients/{patient_id}/asam-loc``
    Runs the rule engine and Claude-narrated rationale, caches the
    result by ``(patient_id, evidence_hash, model_version)``, and
    returns the canonical ``AsamAssessmentRead`` (PRD §6.1).
    - 201 on fresh compute. Location header points at the GET-by-id.
    - 200 on cache hit.
    - 422 if the patient has no AsamEvidence.
    - 503 if Claude is rate-limited or the breaker is open.
    - 200 with degraded rationale if Claude is unreachable (5xx /
      connection error) -- the engine's recommendation is still
      authoritative; the narrative is just missing.
    - 502 if Claude returned a malformed structured-output payload.

  * ``GET /api/v1/asam-assessments/{assessment_id}``
    Reads a cached assessment. ``If-None-Match`` returns 304 when the
    ETag (``W/"{evidence_hash}-{model_version}"``) matches.

The endpoint maps engine + LLM outputs to RFC 7807 problems via the
Phase 2 unified error handler (path-prefix dispatch sends every
``/api/v1/*`` error through ``application/problem+json``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.deps import PatientDep, SessionDep, audit_read
from app.api.deps_clinical import get_claude_client, raise_http_for_claude_error
from app.clinical.asam.level_decision import decide
from app.clinical.asam.narration import narrate
from app.clinical.asam.risk_ratings import compute_risk_ratings
from app.clinical.asam.schemas import (
    AsamAssessmentRead,
    AsamRationale,
)
from app.clinical.llm.claude_client import (
    ClaudeBreakerOpen,
    ClaudeClient,
    ClaudeRateLimited,
    ClaudeUnavailable,
)
from app.clinical.llm.structured_output import StructuredOutputError
from app.clinical.shared.evidence_hash import compute_evidence_hash
from app.clinical.shared.narration_outcome import NarrationOutcome
from app.clinical.shared.response_builder import build_asam_response
from app.core.config import get_settings
from app.core.security import require_api_key
from app.db.models import AsamAssessment

# ────────────────────────────────────────────────────────────────────────
# Request body schema.
# ────────────────────────────────────────────────────────────────────────


class AsamLocRequest(BaseModel):
    """POST body. Default empty; ``force_recompute`` bypasses the cache."""

    force_recompute: bool = Field(
        default=False,
        description="When true, ignore any cached AsamAssessment row and recompute.",
    )


# ────────────────────────────────────────────────────────────────────────
# Routers.
#
# Two routers: one for POST /patients/{id}/asam-loc (auth + audit-on-read),
# one for GET /asam-assessments/{id} (same deps but different prefix).
# ────────────────────────────────────────────────────────────────────────


patient_router = APIRouter(
    prefix="/api/v1/patients",
    tags=["asam"],
    dependencies=[Depends(require_api_key), Depends(audit_read)],
)


assessment_router = APIRouter(
    prefix="/api/v1/asam-assessments",
    tags=["asam"],
    dependencies=[Depends(require_api_key), Depends(audit_read)],
)


ClaudeClientDep = Annotated[ClaudeClient, Depends(get_claude_client)]


# ────────────────────────────────────────────────────────────────────────
# Helpers.
# ────────────────────────────────────────────────────────────────────────


def _etag_for(evidence_hash: str, model_version: str) -> str:
    """Build the weak ETag used on both POST and GET responses."""
    return f'W/"{evidence_hash}-{model_version}"'


def _row_to_response(row: AsamAssessment, *, cached: bool) -> AsamAssessmentRead:
    """Hydrate the API response from a persisted AsamAssessment row.

    The row's ``dimensions`` JSONB carries the full per-dim shape that
    the API exposes, so the response is one straight model_validate.
    Setting ``cached=True`` on a cache-hit response satisfies PRD §5.7;
    the original ETag still matches.
    """
    payload = {
        "id": row.id,
        "patient_id": row.patient_id,
        "computed_at": row.computed_at,
        "evidence_hash": row.evidence_hash,
        "model_version": row.model_version,
        "cached": cached,
        "recommendation": {
            "level": row.recommended_level,
            "level_display": row.dimensions.get("_level_display", row.recommended_level),
            "modifiers": row.modifiers,
            "co_occurring_enhanced": "COE" in row.modifiers,
            "biomedical_enhanced": row.recommended_level.endswith("-BIO"),
        },
        "dimensions": row.dimensions.get("_dimensions_payload", []),
        "rules_fired": row.rules_fired,
        "rationale": row.rationale,
        "confidence": row.confidence,
        "rationale_status": row.rationale_status,
        "rationale_warnings": row.rationale_warnings,
        "links": {
            "self": f"/api/v1/asam-assessments/{row.id}",
            "fhir": f"/fhir/ClinicalImpression/{row.id}",
        },
    }
    return AsamAssessmentRead.model_validate(payload)


def _lookup_cached(
    db: Session,
    *,
    patient_id: UUID,
    evidence_hash: str,
    model_version: str,
) -> AsamAssessment | None:
    """Return the cached AsamAssessment row for this cache key, or None.

    Cache key matches the ``UNIQUE(patient_id, evidence_hash,
    model_version)`` constraint on the table — used both for the
    pre-compute cache check and for the post-IntegrityError recovery
    path (see ``post_asam_loc``).
    """
    return db.exec(
        select(AsamAssessment).where(
            (AsamAssessment.patient_id == patient_id)
            & (AsamAssessment.evidence_hash == evidence_hash)
            & (AsamAssessment.model_version == model_version)
        )
    ).first()


def _persist(
    db: Session,
    *,
    response: AsamAssessmentRead,
    evidence_hash: str,
    model_version: str,
) -> AsamAssessment:
    """Insert one AsamAssessment row mirroring the response payload.

    Adds the row and flushes -- the caller wraps this in
    ``db.begin_nested()`` (a SAVEPOINT) and commits the outer
    transaction afterwards. Flushing here is important: it issues the
    INSERT immediately, so any UNIQUE-constraint violation surfaces
    as ``IntegrityError`` inside the SAVEPOINT (which the route
    handler catches for the concurrent-insert recovery path).

    The row stores the response's full ``dimensions`` list under a
    private ``_dimensions_payload`` key in the ``dimensions`` JSONB
    column so cache hits can rehydrate the same shape. The same JSONB
    also holds the level display name (denormalized) so we don't need
    to re-derive it on read.
    """
    row = AsamAssessment(
        id=response.id,
        patient_id=response.patient_id,
        evidence_hash=evidence_hash,
        recommended_level=response.recommendation.level,
        modifiers=response.recommendation.modifiers,
        dimensions={
            "_dimensions_payload": [d.model_dump(mode="json") for d in response.dimensions],
            "_level_display": response.recommendation.level_display,
        },
        rules_fired=list(response.rules_fired),
        rationale=response.rationale,
        confidence=response.confidence,
        rationale_status=response.rationale_status,
        rationale_warnings=list(response.rationale_warnings),
        model_version=model_version,
        computed_at=response.computed_at,
    )
    db.add(row)
    db.flush()
    return row


def _try_narrate(
    client: ClaudeClient,
    db: Session,
    patient_id: UUID,
    decision,  # type: ignore[no-untyped-def]
    ratings,  # type: ignore[no-untyped-def]
) -> NarrationOutcome:
    """Run narration with phase_3_PRD.md §5.9 error mapping.

    Shared 503 / 502 mapping for rate-limit / breaker / malformed-output
    failures lives in ``raise_http_for_claude_error``. ``ClaudeUnavailable``
    is handled inline because the degraded payload is ASAM-specific
    (returns an ``AsamRationale`` stub with the engine recommendation
    preserved as the authoritative source).
    """
    try:
        return narrate(
            client=client,
            db=db,
            patient_id=patient_id,
            decision=decision,
            ratings=ratings,
        )
    except ClaudeUnavailable:
        return NarrationOutcome(
            rationale=AsamRationale(
                overall_rationale=(
                    "Rationale narration is currently unavailable. "
                    "The recommended level is the deterministic rule-engine output."
                ),
                dimensions=[],
                confidence="moderate",
            ),
            validation=None,  # type: ignore[arg-type]
            status="unavailable",
            warnings=["claude_unavailable"],
        )
    except (ClaudeBreakerOpen, ClaudeRateLimited, StructuredOutputError) as exc:
        raise_http_for_claude_error(exc)
        raise  # unreachable; mypy needs the explicit re-raise for return-path narrowing


# ────────────────────────────────────────────────────────────────────────
# POST /api/v1/patients/{patient_id}/asam-loc
# ────────────────────────────────────────────────────────────────────────


@patient_router.post(
    "/{patient_id}/asam-loc",
    response_model=AsamAssessmentRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="compute_asam_loc",
    summary="Compute (or fetch cached) ASAM Level-of-Care assessment.",
)
def post_asam_loc(
    patient: PatientDep,
    db: SessionDep,
    claude: ClaudeClientDep,
    response: Response,
    body: AsamLocRequest | None = None,
) -> AsamAssessmentRead:
    """Compute or return the cached ASAM Level-of-Care assessment."""
    body = body or AsamLocRequest()
    settings = get_settings()
    model_version = settings.phealth_llm_model

    # 1. Hash the contributing-row set. Empty hash means no evidence.
    evidence_hash = compute_evidence_hash(patient.id, db, kind="asam")
    if not evidence_hash:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Patient {patient.id} has no ASAM evidence rows; "
                "cannot compute a Level-of-Care recommendation. "
                "Re-ingest the chart and retry."
            ),
        )

    # 2. Cache lookup. Hit -> 200 with cached row's body + ETag.
    if not body.force_recompute:
        cached_row = _lookup_cached(
            db,
            patient_id=patient.id,
            evidence_hash=evidence_hash,
            model_version=model_version,
        )
        if cached_row is not None:
            response.status_code = status.HTTP_200_OK
            response.headers["ETag"] = _etag_for(evidence_hash, model_version)
            response.headers["Location"] = f"/api/v1/asam-assessments/{cached_row.id}"
            return _row_to_response(cached_row, cached=True)

    # 3. Run the deterministic core. This part NEVER fails on Marcus's
    #    chart (M3+M4 are 100% covered). If it does fail on a future
    #    patient with sparse data, the helpers default-on-missing so
    #    we still get a (possibly low-confidence) recommendation.
    ratings = compute_risk_ratings(patient.id, db)
    decision = decide(ratings)

    # 4. Narrate. Maps Claude failures to the right HTTP responses.
    outcome = _try_narrate(claude, db, patient.id, decision, ratings)

    # 5. Build the response shape.
    api_response = build_asam_response(
        patient_id=patient.id,
        evidence_hash=evidence_hash,
        model_version=model_version,
        cached=False,
        decision=decision,
        ratings=ratings,
        narration=outcome,
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )

    # 6. Persist. UNIQUE(patient_id, evidence_hash, model_version) on the
    #    table protects against duplicate inserts under concurrent
    #    requests; if we lose that race (a second click that arrives
    #    after we passed the cache check at step 2 but before this
    #    commit), the INSERT raises IntegrityError. We wrap the persist
    #    in a SAVEPOINT so the failure only rolls back the failed
    #    INSERT, not the surrounding transaction -- then re-read the
    #    winning row and return it as a cache hit. A rapid double-click
    #    therefore returns 200 with the same body rather than 500ing.
    try:
        with db.begin_nested():
            _persist(
                db,
                response=api_response,
                evidence_hash=evidence_hash,
                model_version=model_version,
            )
        # SAVEPOINT released cleanly; commit the outer transaction so
        # the row is durably persisted before we respond.
        db.commit()
    except IntegrityError:
        # begin_nested has already rolled the SAVEPOINT back; the outer
        # transaction (and any prior reads / writes) is intact.
        winning_row = _lookup_cached(
            db,
            patient_id=patient.id,
            evidence_hash=evidence_hash,
            model_version=model_version,
        )
        if winning_row is None:
            # The constraint fired but the row isn't visible. Should
            # be impossible under read-committed isolation (Postgres
            # default); surface a clear 500 rather than masking it.
            raise HTTPException(
                status_code=500,
                detail="Concurrent insert race lost without recoverable cache row; retry.",
            ) from None
        response.status_code = status.HTTP_200_OK
        response.headers["ETag"] = _etag_for(evidence_hash, model_version)
        response.headers["Location"] = f"/api/v1/asam-assessments/{winning_row.id}"
        return _row_to_response(winning_row, cached=True)

    response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = _etag_for(evidence_hash, model_version)
    response.headers["Location"] = f"/api/v1/asam-assessments/{api_response.id}"
    if outcome.status == "unavailable":
        response.headers["x-rationale-status"] = "unavailable"
        response.headers["x-rule-engine-only"] = "true"
    return api_response


# ────────────────────────────────────────────────────────────────────────
# GET /api/v1/asam-assessments/{assessment_id}
# ────────────────────────────────────────────────────────────────────────


def _get_assessment_or_404(assessment_id: UUID, db: Session) -> AsamAssessment:
    row = db.get(AsamAssessment, assessment_id)
    if row is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Assessment {assessment_id} not found"
        )
    return row


@assessment_router.get(
    "/{assessment_id}",
    response_model=AsamAssessmentRead,
    operation_id="get_asam_assessment",
    summary="Fetch a persisted ASAM assessment by id; 304 on If-None-Match.",
)
def get_asam_assessment(
    assessment_id: UUID,
    db: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> AsamAssessmentRead | Response:
    """Read a cached AsamAssessment by id.

    Honors ``If-None-Match`` per phase_3_PRD.md §5.7 -- a matching ETag
    short-circuits to 304 No Body. The ETag is
    ``W/"{evidence_hash}-{model_version}"`` (same as the POST endpoint
    emits), so reviewers can copy the POST's ETag and verify the GET
    returns 304.
    """
    row = _get_assessment_or_404(assessment_id, db)
    etag = _etag_for(row.evidence_hash, row.model_version)
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    return _row_to_response(row, cached=True)


# Convenience export: both routers mount into app.main.
__all__ = ["patient_router", "assessment_router", "AsamLocRequest"]
