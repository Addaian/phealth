"""
Shared FastAPI dependencies for the API layer.

``SessionDep`` injects a request-scoped database session; ``PatientDep`` loads
the Patient named by a route's ``{patient_id}`` path parameter (or raises 404);
``audit_read`` (M2) schedules an ``AuditEvent`` insert after every read so the
substrate has a HIPAA-style accounting of every disclosure.

All three use the ``Annotated[..., Depends(...)]`` form so the ``Depends()``
call stays out of the argument default (which ruff's B008 flags).
"""

import uuid
from typing import Annotated

from fastapi import BackgroundTasks, Depends, HTTPException, Request, status
from sqlmodel import Session

from app.db.models import AuditEvent, Patient
from app.db.session import engine, get_session

# A request-scoped database session.
SessionDep = Annotated[Session, Depends(get_session)]


def get_patient_or_404(patient_id: uuid.UUID, session: SessionDep) -> Patient:
    """Dependency: load the Patient for a route's ``{patient_id}`` or raise 404."""
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Patient {patient_id} not found")
    return patient


# The Patient named by a route's ``{patient_id}`` path parameter; 404 if absent.
PatientDep = Annotated[Patient, Depends(get_patient_or_404)]


# ---------------------------------------------------------------------------
# Audit-on-read (Phase 2 §5.6)
# ---------------------------------------------------------------------------
# When the URL has no patient_id (e.g. /api/v1/patients list), AuditEvent's
# non-null resource_id column gets a sentinel zero UUID. Reads against
# /api/v1/patients can still be located via resource_type (the path).
_NULL_RESOURCE_ID = uuid.UUID(int=0)


def _parse_uuid(value: str | None) -> uuid.UUID:
    """Best-effort UUID parse; falls back to the sentinel zero UUID."""
    if value is None:
        return _NULL_RESOURCE_ID
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return _NULL_RESOURCE_ID


def audit_read(request: Request, background_tasks: BackgroundTasks) -> None:
    """Dependency: schedule one ``AuditEvent(action='read')`` per request.

    The HIPAA accounting-of-disclosures rule effectively requires us to log
    every read; we do it via ``BackgroundTasks`` so the audit write doesn't
    sit on the response's critical path. The background task opens its own
    session because the request session is torn down by the time it runs.

    The audit dep is registered *after* ``require_api_key`` on each router, so
    a 401 (no/wrong key) short-circuits before this dep ever runs -- those
    failures correctly do not appear in the audit log. The PRD's stricter
    "only successful disclosures" filter is not strictly enforced for 4xx/5xx
    that occur *after* this dep schedules (e.g. a 404 from ``PatientDep`` or a
    500 from the endpoint body), which is an MVP-acceptable over-inclusion.

    Pool sizing: each successful GET checks out two connections from the
    SQLAlchemy pool -- the request session (released when the response is
    sent) plus the background audit session (released when the audit write
    commits, just after). The defaults in ``app/db/session.py`` (pool_size=5
    + max_overflow=10) absorb this for the MVP; a deployment running real
    load should size the pool to ``~2 * concurrent_request_target``.
    """
    path = str(request.url.path)
    method = request.method
    query = str(request.url.query)
    resource_id = _parse_uuid(request.path_params.get("patient_id"))

    def _write() -> None:
        with Session(engine) as session:
            session.add(
                AuditEvent(
                    actor="api-key",
                    action="read",
                    resource_type=path,
                    resource_id=resource_id,
                    payload={"method": method, "query": query},
                )
            )
            session.commit()

    background_tasks.add_task(_write)
