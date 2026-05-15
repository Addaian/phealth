"""
FastAPI application entry point.

Wires the API routers + cross-cutting concerns together and exposes the
liveness probe. Run locally with:

    uvicorn app.main:app --reload

or via docker compose (see docker-compose.yml). Interactive API docs at
``/docs``; FHIR capability discovery at ``/fhir/metadata`` (auth-free);
optional server-rendered demo UI at ``/ui`` (see ``ui/router.py`` -- a
sibling package, not a subpackage of ``app``).

Cross-cutting concerns are installed in declaration order:
  1. ``register_exception_handlers`` -- RFC 7807 / OperationOutcome
     dispatcher (must precede the routers so dep-raised exceptions
     also dispatch through it).
  2. ``ETagMiddleware`` -- weak ETag + ``If-None-Match`` → 304 on every
     idempotent JSON GET (streaming media types are skipped).

Routers themselves carry the ``require_api_key`` and ``audit_read`` deps;
see ``app/api/deps.py`` and ``app/core/security.py``.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import asam_loc, ingest, notes, patients, tjc_audit
from app.api import fhir as fhir_api
from app.api.errors import register_exception_handlers
from app.api.etag import ETagMiddleware

# ``ui`` is a sibling package -- a thin server-rendered surface that
# consumes the JSON API in-process (see ``ui/router.py``). Imported
# here only to be mounted on the app below; ``app/*`` itself does not
# depend on ``ui``.
from ui.router import router as ui_router

# Surface application-level INFO logs (ingestion progress, audit events). uvicorn
# configures only its own loggers and leaves the root logger without a handler,
# so app-module INFO records would otherwise be dropped. basicConfig installs a
# root StreamHandler at INFO; it is a no-op if a handler is already present.
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Application lifespan.

    Opens the in-process httpx client used by the demo UI (``ui`` package).
    The transport is ``ASGITransport(app=app)``, so the UI's "API calls"
    are routed through the same exception handlers, middleware and
    dependencies as any external client -- without crossing a real
    socket. Closed on shutdown.
    """
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://internal.invalid")
    app.state.internal_client = client
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(
    title="Perspectives Health — Clinical Ingestion Substrate",
    description=(
        "FHIR-R4-shaped ingestion and extraction substrate over the "
        "SimplePractice Data Export. Phase 1 of the intern technical "
        "assessment — see documents/phase_1_PRD.md."
    ),
    version="0.1.0",
    lifespan=_lifespan,
)

# Register the unified exception handlers BEFORE the routers so any HTTPException
# raised by a route dependency (e.g. require_api_key, get_patient_or_404) is
# caught by our dispatcher and not FastAPI's default ``{"detail": ...}`` envelope.
register_exception_handlers(app)

# ETag middleware (Phase 2 §5.7). Stacked outermost so it sees the *final*
# rendered body, including bodies produced by the exception handlers above
# (those carry a status >= 400 and are skipped by the middleware).
app.add_middleware(ETagMiddleware)

app.include_router(ingest.router)
app.include_router(patients.router)
app.include_router(notes.router)
app.include_router(fhir_api.router)
# Auth-free CapabilityStatement (FHIR R4 spec requirement, see app/api/fhir.py).
app.include_router(fhir_api.metadata_router)

# Phase 3 — clinical decision endpoints (POST /asam-loc + POST /tjc-audit, plus
# GET /asam-assessments/{id} and GET /tjc-audits/{id}). Each module exposes two
# routers because the POST and GET hang off different prefixes
# (/api/v1/patients vs /api/v1/asam-assessments).
app.include_router(asam_loc.patient_router)
app.include_router(asam_loc.assessment_router)
app.include_router(tjc_audit.patient_router)
app.include_router(tjc_audit.audit_router)

# Optional server-rendered demo UI. Auth lives on the JSON API surface;
# the UI's internal client supplies X-API-Key on every call (see
# ``ui/router.py``). Static assets are served under ``/ui/static``;
# the directory lives at the repo root (a sibling of ``app/``), so the
# path resolves through ``parent.parent`` from this file.
app.include_router(ui_router)
app.mount(
    "/ui/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "ui" / "static")),
    name="ui-static",
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 with a static body; touches no dependencies."""
    return {"status": "ok"}


@app.get("/api/v1/capabilities", tags=["meta"])
def capabilities() -> dict:
    """Self-describe the served surface — what a reviewer can call.

    Auth-free (parallel to ``/fhir/metadata`` in M9). Phase 2 §5.9 default
    page sizes are echoed here.
    """
    return {
        "phase": "phase-2",
        "fhir_version": "R4 (via fhir.resources.R4B)",
        "surfaces": {
            "api_v1": {
                "base": "/api/v1",
                "errors": "application/problem+json (RFC 7807)",
                "pagination": {
                    "timeline_default": 50,
                    "documents_default": 20,
                    "observations_default": 50,
                    "max": 100,
                },
            },
            "fhir": {
                "base": "/fhir",
                "errors": "OperationOutcome",
                "spec": "FHIR R4B",
            },
        },
    }
