"""
HTML routes for the demo UI.

Every UI route is an ``async def`` because it dispatches one or more
internal calls against the JSON API via an
``httpx.AsyncClient(transport=ASGITransport(app=...))`` instance held on
``app.state.internal_client`` (wired in ``app/main.py``). Calling the
real handlers — instead of the underlying DB helpers — keeps the
architectural story honest: the UI is just another API client.

Routes:
  * ``GET  /ui/``                                -- patient list + nav.
  * ``GET  /ui/patients/{id}/chart``              -- canonical /chart view.
  * ``GET  /ui/patients/{id}/asam-loc``           -- cached ASAM result or "Compute" CTA.
  * ``POST /ui/patients/{id}/asam-loc/compute``   -- proxy POST, redirect to GET.
  * ``GET  /ui/patients/{id}/tjc-audit``          -- cached TJC audit or "Compute" CTA.
  * ``POST /ui/patients/{id}/tjc-audit/compute``  -- proxy POST, redirect to GET.

The UI router carries NO auth dependency. Auth is enforced on the JSON
API surface; the UI's internal client attaches ``X-API-Key`` per call.
See ``ui/__init__.py`` for the rationale.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from app.api.deps import SessionDep
from app.core.config import get_settings
from app.db.models import (
    AsamAssessment,
    Patient,
    TjcAuditResult,
)

# ────────────────────────────────────────────────────────────────────────
# Template engine — points at ui/templates/ (this file's sibling dir).
# ────────────────────────────────────────────────────────────────────────

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)


# ────────────────────────────────────────────────────────────────────────
# Internal-API helpers.
# ────────────────────────────────────────────────────────────────────────


def _internal_client(request: Request) -> httpx.AsyncClient:
    """Return the in-process httpx client wired in ``app/main.py``."""
    client = getattr(request.app.state, "internal_client", None)
    if client is None:  # pragma: no cover -- only fires if startup never ran
        raise RuntimeError("internal_client not initialised; check app/main.py lifespan")
    return client


def _api_headers() -> dict[str, str]:
    """Headers for every internal call: API key + JSON accept.

    Pulled fresh on each call so a settings reload (rare; tests do it)
    takes effect immediately. The key never leaves the process; UI users
    don't see it.
    """
    return {
        "X-API-Key": get_settings().ingest_api_key,
        "Accept": "application/json",
    }


async def _get_json(request: Request, path: str) -> tuple[int, Any]:
    """Internal GET helper. Returns ``(status_code, parsed_body_or_none)``.

    A 304 (from the ETag middleware) returns body ``None`` -- callers
    that care about that distinction should branch on the status. For
    the UI's purposes we treat 304 as "no body to render" and fall back
    to a fresh fetch with the conditional header stripped; in practice
    the UI never sends ``If-None-Match`` so this is defensive.
    """
    client = _internal_client(request)
    response = await client.get(path, headers=_api_headers())
    if response.status_code == 304:
        return 304, None
    if response.status_code >= 400:
        return response.status_code, response.json()
    return response.status_code, response.json()


async def _post_json(
    request: Request,
    path: str,
    *,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any, dict[str, str]]:
    """Internal POST helper. Returns ``(status_code, body, headers)``."""
    client = _internal_client(request)
    response = await client.post(path, json=body or {}, headers=_api_headers())
    return response.status_code, response.json(), dict(response.headers)


# ────────────────────────────────────────────────────────────────────────
# Landing — GET /ui/
# ────────────────────────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, session: SessionDep) -> HTMLResponse:
    """Patient list + brand header. The single ingested patient (Marcus
    Reyes) is the only entry; multi-patient discovery is out of scope
    for the brief."""
    patients = session.exec(select(Patient)).all()
    rows = [
        {
            "id": str(patient.id),
            "given_name": patient.given_name,
            "family_name": patient.family_name,
            "external_id": patient.external_id,
        }
        for patient in patients
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "patients": rows,
            "page_title": "Patients",
        },
    )


# ────────────────────────────────────────────────────────────────────────
# Chart — GET /ui/patients/{id}/chart
# ────────────────────────────────────────────────────────────────────────


@router.get("/patients/{patient_id}/chart", response_class=HTMLResponse)
async def chart_view(request: Request, patient_id: UUID) -> HTMLResponse:
    """Render the canonical ``/api/v1/patients/{id}/chart`` response."""
    code, body = await _get_json(request, f"/api/v1/patients/{patient_id}/chart")
    if code == 404:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")
    if code >= 400:
        raise HTTPException(status_code=code, detail=str(body))
    return templates.TemplateResponse(
        request,
        "chart.html",
        {
            "chart": body,
            "patient_id": str(patient_id),
            "page_title": f"Chart — {body['patient']['name']['given'][0]} "
            f"{body['patient']['name']['family']}",
        },
    )


# ────────────────────────────────────────────────────────────────────────
# ASAM — GET /ui/patients/{id}/asam-loc
# ────────────────────────────────────────────────────────────────────────


def _latest_asam_for(patient_id: UUID, session: SessionDep) -> AsamAssessment | None:
    """Return the most-recent persisted AsamAssessment for the patient.

    The POST endpoint already de-duplicates by ``(patient_id,
    evidence_hash, model_version)`` so the "latest" row is by
    computed_at. Used to decide whether to show the result or the
    "Compute" CTA on page load.
    """
    rows = session.exec(select(AsamAssessment).where(AsamAssessment.patient_id == patient_id)).all()
    if not rows:
        return None
    return max(rows, key=lambda row: row.computed_at)


@router.get("/patients/{patient_id}/asam-loc", response_class=HTMLResponse)
async def asam_view(
    request: Request,
    patient_id: UUID,
    session: SessionDep,
) -> HTMLResponse:
    """Render the most-recent ASAM assessment, or a Compute CTA."""
    # Confirm patient exists (gives a clean 404 separate from "no assessment yet").
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")

    latest = _latest_asam_for(patient_id, session)
    if latest is None:
        return templates.TemplateResponse(
            request,
            "asam.html",
            {
                "patient_id": str(patient_id),
                "patient_name": f"{patient.given_name} {patient.family_name}",
                "assessment": None,
                "page_title": "ASAM Level of Care",
            },
        )
    # Fetch the full read shape (includes citations + dimensions) through the
    # GET endpoint so the UI sees exactly what an API client would.
    code, body = await _get_json(request, f"/api/v1/asam-assessments/{latest.id}")
    if code >= 400:
        raise HTTPException(status_code=code, detail=str(body))
    return templates.TemplateResponse(
        request,
        "asam.html",
        {
            "patient_id": str(patient_id),
            "patient_name": f"{patient.given_name} {patient.family_name}",
            "assessment": body,
            "page_title": "ASAM Level of Care",
        },
    )


@router.post("/patients/{patient_id}/asam-loc/compute")
async def asam_compute(
    request: Request,
    patient_id: UUID,
) -> RedirectResponse:
    """Trigger an ASAM computation (cached or fresh) and redirect to GET.

    Always sends ``force_recompute: false`` so repeat clicks hit the
    cache instead of running Claude unnecessarily. A future iteration
    could expose a "Force recompute" checkbox on the form.
    """
    code, body, _ = await _post_json(
        request,
        f"/api/v1/patients/{patient_id}/asam-loc",
        body={"force_recompute": False},
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=str(body))
    return RedirectResponse(
        url=f"/ui/patients/{patient_id}/asam-loc",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ────────────────────────────────────────────────────────────────────────
# TJC — GET /ui/patients/{id}/tjc-audit
# ────────────────────────────────────────────────────────────────────────


def _latest_tjc_for(patient_id: UUID, session: SessionDep) -> TjcAuditResult | None:
    """Return the most-recent persisted TjcAuditResult for the patient."""
    rows = session.exec(select(TjcAuditResult).where(TjcAuditResult.patient_id == patient_id)).all()
    if not rows:
        return None
    return max(rows, key=lambda row: row.computed_at)


@router.get("/patients/{patient_id}/tjc-audit", response_class=HTMLResponse)
async def tjc_view(
    request: Request,
    patient_id: UUID,
    session: SessionDep,
) -> HTMLResponse:
    """Render the most-recent TJC audit, or a Compute CTA."""
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail=f"Patient {patient_id} not found")

    latest = _latest_tjc_for(patient_id, session)
    if latest is None:
        return templates.TemplateResponse(
            request,
            "tjc.html",
            {
                "patient_id": str(patient_id),
                "patient_name": f"{patient.given_name} {patient.family_name}",
                "audit": None,
                "page_title": "TJC Compliance Audit",
            },
        )
    code, body = await _get_json(request, f"/api/v1/tjc-audits/{latest.id}")
    if code >= 400:
        raise HTTPException(status_code=code, detail=str(body))
    return templates.TemplateResponse(
        request,
        "tjc.html",
        {
            "patient_id": str(patient_id),
            "patient_name": f"{patient.given_name} {patient.family_name}",
            "audit": body,
            "page_title": "TJC Compliance Audit",
        },
    )


@router.post("/patients/{patient_id}/tjc-audit/compute")
async def tjc_compute(
    request: Request,
    patient_id: UUID,
) -> RedirectResponse:
    """Trigger a TJC audit (cached or fresh) and redirect to GET."""
    code, body, _ = await _post_json(
        request,
        f"/api/v1/patients/{patient_id}/tjc-audit",
        body={"force_recompute": False},
    )
    if code >= 400:
        raise HTTPException(status_code=code, detail=str(body))
    return RedirectResponse(
        url=f"/ui/patients/{patient_id}/tjc-audit",
        status_code=status.HTTP_303_SEE_OTHER,
    )


__all__ = ["router"]
