"""
Tests for the audit-on-read dependency (Phase 2 §5.6).

Every successful read against an audited router (``/api/v1/*`` or ``/fhir/*``)
must write one ``AuditEvent(action='read')`` row. The audit is scheduled via
``BackgroundTasks``, so the write happens just after the response is returned;
the TestClient processes background tasks synchronously, so the count check
below is reliable.

The audit row is committed by the background task into its own session (the
request session is torn down by the time the task runs), so this test queries
via a fresh ``Session(engine)`` -- ``db_session`` (the rolled-back transaction)
would not see the row.

This means each test run leaves audit rows behind in the dev database, which
is correct for an append-only event log and acceptable for the MVP.
"""

from sqlalchemy import func
from sqlmodel import Session, select

from app.db.models import AuditEvent
from app.db.session import engine


def _count_reads_for(path: str) -> int:
    with Session(engine) as session:
        return session.exec(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.resource_type == path, AuditEvent.action == "read")
        ).one()


def test_successful_api_v1_read_writes_one_audit_event(api_client, ingested_patient):
    """A 200 GET on /api/v1/* writes exactly one AuditEvent(action='read')."""
    path = f"/api/v1/patients/{ingested_patient.id}"
    before = _count_reads_for(path)

    response = api_client.get(path)
    assert response.status_code == 200

    assert _count_reads_for(path) == before + 1


def test_unauthorized_request_does_not_audit(client, ingested_patient):
    """A 401 (missing X-API-Key) short-circuits before the audit dep runs."""
    path = f"/api/v1/patients/{ingested_patient.id}/intake"
    before = _count_reads_for(path)

    response = client.get(path)
    assert response.status_code == 401

    assert _count_reads_for(path) == before  # no audit row added
