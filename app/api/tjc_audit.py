"""
TJC compliance-audit endpoints.

Mirrors ``app.api.asam_loc`` in structure. Two routes:

  * ``POST /api/v1/patients/{patient_id}/tjc-audit``  -> TjcAuditRead.
  * ``GET  /api/v1/tjc-audits/{audit_id}``            -> TjcAuditRead (304 on If-None-Match).

Same cache key shape (``patient_id`` + ``evidence_hash`` +
``model_version``); same error-handling tiers (503 / 502 / 422); same
ETag pattern. The TJC-specific work is in ``run_audit`` (M5) +
``narrate_tjc`` (M9).
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
from app.clinical.llm.claude_client import (
    ClaudeBreakerOpen,
    ClaudeClient,
    ClaudeRateLimited,
    ClaudeUnavailable,
)
from app.clinical.llm.structured_output import StructuredOutputError
from app.clinical.shared.evidence_hash import compute_evidence_hash
from app.clinical.shared.narration_outcome import NarrationOutcome
from app.clinical.shared.response_builder import build_tjc_response
from app.clinical.tjc.audit_functions import EpFinding
from app.clinical.tjc.narration import narrate_tjc
from app.clinical.tjc.runner import run_audit
from app.clinical.tjc.schemas import (
    TjcAuditRead,
    TjcAuditResponse,
    TjcFindingNarration,
)
from app.core.config import get_settings
from app.core.security import require_api_key
from app.db.models import TjcAuditResult

# ────────────────────────────────────────────────────────────────────────
# Request body schema.
# ────────────────────────────────────────────────────────────────────────


class TjcAuditRequest(BaseModel):
    """POST body. Default empty; ``force_recompute`` bypasses the cache."""

    force_recompute: bool = Field(
        default=False,
        description="When true, ignore any cached TjcAuditResult row and recompute.",
    )


# ────────────────────────────────────────────────────────────────────────
# Routers.
# ────────────────────────────────────────────────────────────────────────


patient_router = APIRouter(
    prefix="/api/v1/patients",
    tags=["tjc"],
    dependencies=[Depends(require_api_key), Depends(audit_read)],
)


audit_router = APIRouter(
    prefix="/api/v1/tjc-audits",
    tags=["tjc"],
    dependencies=[Depends(require_api_key), Depends(audit_read)],
)


ClaudeClientDep = Annotated[ClaudeClient, Depends(get_claude_client)]


# ────────────────────────────────────────────────────────────────────────
# Helpers.
# ────────────────────────────────────────────────────────────────────────


def _etag_for(evidence_hash: str, model_version: str) -> str:
    return f'W/"{evidence_hash}-{model_version}"'


def _row_to_response(row: TjcAuditResult, *, cached: bool) -> TjcAuditRead:
    """Hydrate TjcAuditRead from a persisted row. Same pattern as ASAM."""
    payload = {
        "id": row.id,
        "patient_id": row.patient_id,
        "computed_at": row.computed_at,
        "evidence_hash": row.evidence_hash,
        "model_version": row.model_version,
        "cached": cached,
        "summary": row.summary,
        "findings": row.findings,
        "rationale_status": row.rationale_status,
        "rationale_warnings": row.rationale_warnings,
        "links": {
            "self": f"/api/v1/tjc-audits/{row.id}",
            "fhir_bundle": (
                f"/fhir/DetectedIssue?subject=Patient/{row.patient_id}&category=tjc-audit"
            ),
        },
    }
    return TjcAuditRead.model_validate(payload)


def _lookup_cached(
    db: Session,
    *,
    patient_id: UUID,
    evidence_hash: str,
    model_version: str,
) -> TjcAuditResult | None:
    """Return the cached TjcAuditResult for this cache key, or None.

    Used both for the pre-compute cache check and for the
    post-IntegrityError recovery path (see ``post_tjc_audit``).
    Mirrors the cache key on the UNIQUE constraint.
    """
    return db.exec(
        select(TjcAuditResult).where(
            (TjcAuditResult.patient_id == patient_id)
            & (TjcAuditResult.evidence_hash == evidence_hash)
            & (TjcAuditResult.model_version == model_version)
        )
    ).first()


def _persist(
    db: Session,
    *,
    response: TjcAuditRead,
    evidence_hash: str,
    model_version: str,
) -> TjcAuditResult:
    """Insert one TjcAuditResult row mirroring the response payload.

    Adds the row and flushes (no commit) -- the caller wraps this in
    ``db.begin_nested()`` and commits the outer transaction after the
    SAVEPOINT releases cleanly. Flushing here is important: it issues
    the INSERT immediately so any UNIQUE-constraint violation
    surfaces inside the SAVEPOINT for the recovery path to catch.
    """
    row = TjcAuditResult(
        id=response.id,
        patient_id=response.patient_id,
        evidence_hash=evidence_hash,
        findings=[f.model_dump(mode="json") for f in response.findings],
        summary=response.summary.model_dump(mode="json"),
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
    findings: list[EpFinding],
) -> NarrationOutcome:
    """Run TJC narration with phase_3_PRD.md §5.9 error mapping.

    Shared 503 / 502 mapping for rate-limit / breaker / malformed-output
    failures lives in ``raise_http_for_claude_error``. ``ClaudeUnavailable``
    is handled inline because the degraded payload is TJC-specific
    (a TjcAuditResponse with one finding per EP, no citations; the
    response builder falls back to engine evidence_pointers for
    positive findings so reviewers still see citation context).
    """
    try:
        return narrate_tjc(
            client=client,
            db=db,
            patient_id=patient_id,
            findings=findings,
        )
    except ClaudeUnavailable:
        empty_response = TjcAuditResponse(
            findings=[
                TjcFindingNarration(
                    ep_code=f.ep_code,
                    narrative=f.finding_template,
                    citations=[],
                )
                for f in findings
            ]
        )
        return NarrationOutcome(
            rationale=empty_response,
            validation=None,  # type: ignore[arg-type]
            status="unavailable",
            warnings=["claude_unavailable"],
        )
    except (ClaudeBreakerOpen, ClaudeRateLimited, StructuredOutputError) as exc:
        raise_http_for_claude_error(exc)
        raise  # unreachable; mypy needs the explicit re-raise for return-path narrowing


# ────────────────────────────────────────────────────────────────────────
# POST /api/v1/patients/{patient_id}/tjc-audit
# ────────────────────────────────────────────────────────────────────────


@patient_router.post(
    "/{patient_id}/tjc-audit",
    response_model=TjcAuditRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="compute_tjc_audit",
    summary="Compute (or fetch cached) Joint Commission compliance audit.",
)
def post_tjc_audit(
    patient: PatientDep,
    db: SessionDep,
    claude: ClaudeClientDep,
    response: Response,
    body: TjcAuditRequest | None = None,
) -> TjcAuditRead:
    """Compute or return the cached TJC audit."""
    body = body or TjcAuditRequest()
    settings = get_settings()
    model_version = settings.phealth_llm_model

    evidence_hash = compute_evidence_hash(patient.id, db, kind="tjc")
    if not evidence_hash:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Patient {patient.id} has no TJC coverage rows; "
                "cannot compute a compliance audit. Re-ingest the chart and retry."
            ),
        )

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
            response.headers["Location"] = f"/api/v1/tjc-audits/{cached_row.id}"
            return _row_to_response(cached_row, cached=True)

    # Run the deterministic core: 13 audit predicates.
    findings = run_audit(patient.id, db)
    outcome = _try_narrate(claude, db, patient.id, findings)

    api_response = build_tjc_response(
        patient_id=patient.id,
        evidence_hash=evidence_hash,
        model_version=model_version,
        cached=False,
        findings=findings,
        narration=outcome,
        computed_at=datetime.now(UTC).replace(tzinfo=None),
    )

    # Concurrent-insert recovery: a rapid double-click can put two
    # requests through the cache check before either commits. The
    # UNIQUE(patient_id, evidence_hash, model_version) constraint
    # rejects the loser's INSERT. We wrap the persist in a SAVEPOINT
    # so the failure only rolls back the failed INSERT, not the
    # surrounding transaction; we then re-read the winning row and
    # return it as a cache hit (200) rather than 500ing.
    try:
        with db.begin_nested():
            _persist(
                db,
                response=api_response,
                evidence_hash=evidence_hash,
                model_version=model_version,
            )
        # SAVEPOINT released cleanly; commit the outer transaction.
        db.commit()
    except IntegrityError:
        # begin_nested has already rolled the SAVEPOINT back; the outer
        # transaction is intact.
        winning_row = _lookup_cached(
            db,
            patient_id=patient.id,
            evidence_hash=evidence_hash,
            model_version=model_version,
        )
        if winning_row is None:
            raise HTTPException(
                status_code=500,
                detail="Concurrent insert race lost without recoverable cache row; retry.",
            ) from None
        response.status_code = status.HTTP_200_OK
        response.headers["ETag"] = _etag_for(evidence_hash, model_version)
        response.headers["Location"] = f"/api/v1/tjc-audits/{winning_row.id}"
        return _row_to_response(winning_row, cached=True)

    response.status_code = status.HTTP_201_CREATED
    response.headers["ETag"] = _etag_for(evidence_hash, model_version)
    response.headers["Location"] = f"/api/v1/tjc-audits/{api_response.id}"
    if outcome.status == "unavailable":
        response.headers["x-rationale-status"] = "unavailable"
        response.headers["x-rule-engine-only"] = "true"
    return api_response


# ────────────────────────────────────────────────────────────────────────
# GET /api/v1/tjc-audits/{audit_id}
# ────────────────────────────────────────────────────────────────────────


def _get_audit_or_404(audit_id: UUID, db: Session) -> TjcAuditResult:
    row = db.get(TjcAuditResult, audit_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Audit {audit_id} not found")
    return row


@audit_router.get(
    "/{audit_id}",
    response_model=TjcAuditRead,
    operation_id="get_tjc_audit",
    summary="Fetch a persisted TJC audit by id; 304 on If-None-Match.",
)
def get_tjc_audit(
    audit_id: UUID,
    db: SessionDep,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> TjcAuditRead | Response:
    """Read a cached TjcAuditResult by id. 304 on matching If-None-Match."""
    row = _get_audit_or_404(audit_id, db)
    etag = _etag_for(row.evidence_hash, row.model_version)
    if if_none_match == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED)
    response.headers["ETag"] = etag
    return _row_to_response(row, cached=True)


__all__ = ["patient_router", "audit_router", "TjcAuditRequest"]
