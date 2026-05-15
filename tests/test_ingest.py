"""
M6 acceptance tests for the ingestion pipeline.

Runs the full orchestrator against the committed SimplePractice export and
asserts the M6 acceptance criteria (implementation plan §M6):

  1. Four documents ingested, each with non-empty raw_text and split sections.
  2. All seven clinical scales extracted, every offset a valid provenance span.
  3. All six ASAM 4th-edition dimensions covered, every evidence snippet
     round-tripping against raw_text (PRD §5.5).
  4. The intentional compliance gaps flagged in the TJC coverage matrix, with
     at least one EP satisfied for contrast.

Plus the PRD §7 idempotency guarantee: a re-ingest creates no duplicate rows.

These run inside the rolled-back ``db_session`` fixture and start by clearing
any rows from prior runs *within that transaction* — so each test sees a clean
slate and the developer's database is left untouched.
"""

from sqlalchemy import delete, func
from sqlmodel import select

from app.db.models import (
    AsamAssessment,
    AsamEvidence,
    AuditEvent,
    ClinicalDocument,
    Encounter,
    ExtractedObservation,
    LlmInvocation,
    Patient,
    TjcAuditResult,
    TjcCoverage,
)
from app.ingest.simplepractice_zip import ingest_export

# Child-to-parent order, so foreign-key constraints are satisfied on delete.
# Task 3 tables (AsamAssessment, TjcAuditResult, LlmInvocation) FK back
# to Patient and must be cleared first; live Compute runs leave rows
# behind that would otherwise block `DELETE FROM patient`.
_CLEAR_ORDER = (
    AsamAssessment,
    TjcAuditResult,
    LlmInvocation,
    AsamEvidence,
    ExtractedObservation,
    TjcCoverage,
    ClinicalDocument,
    Encounter,
    AuditEvent,
    Patient,
)

# The data tables idempotency applies to. AuditEvent is deliberately excluded:
# it is an append-only event log, so it *should* grow on every ingest run --
# even a re-ingest that changes no data is itself an audited event (PRD §7).
_DATA_TABLES = (
    Patient,
    Encounter,
    ClinicalDocument,
    ExtractedObservation,
    AsamEvidence,
    TjcCoverage,
)


def _clear(session) -> None:
    """Empty every pipeline table within the test's (rolled-back) transaction."""
    for model in _CLEAR_ORDER:
        session.execute(delete(model))
    session.flush()


def _data_row_counts(session) -> dict[str, int]:
    """Row count per data table (excludes the append-only AuditEvent log)."""
    return {
        model.__name__: session.exec(select(func.count()).select_from(model)).one()
        for model in _DATA_TABLES
    }


def _audit_count(session) -> int:
    """Number of rows in the event-sourced AuditEvent log."""
    return session.exec(select(func.count()).select_from(AuditEvent)).one()


def test_full_pipeline_ingests_synthetic_chart(db_session, export_root):
    """The orchestrator ingests the synthetic chart and meets every M6 criterion."""
    _clear(db_session)
    summary = ingest_export(export_root, db_session)
    db_session.flush()
    assert summary["documents_ingested"] == 4
    assert summary["documents_skipped"] == 0

    # 1. Four documents, all with non-empty raw_text and split sections.
    documents = db_session.exec(select(ClinicalDocument)).all()
    assert len(documents) == 4
    assert {doc.document_type for doc in documents} == {"bps_intake", "soap", "dap", "dsap"}
    assert all(doc.raw_text.strip() and doc.sections for doc in documents)
    raw_by_doc = {doc.id: doc.raw_text for doc in documents}

    # 2. All seven scales extracted; every observation offset is a valid span.
    scale_codes = {
        obs.code
        for obs in db_session.exec(
            select(ExtractedObservation).where(ExtractedObservation.code_system == "LOINC")
        ).all()
    }
    assert len(scale_codes) == 7
    for obs in db_session.exec(select(ExtractedObservation)).all():
        assert 0 <= obs.char_start < obs.char_end <= len(raw_by_doc[obs.document_id])

    # 3. All six ASAM dimensions covered; every evidence snippet round-trips.
    evidence = db_session.exec(select(AsamEvidence)).all()
    assert {row.dimension for row in evidence} == {1, 2, 3, 4, 5, 6}
    for row in evidence:
        assert raw_by_doc[row.document_id][row.char_start : row.char_end] == row.snippet

    # 4. The intentional compliance gaps are flagged, with satisfied EPs for contrast.
    coverage = db_session.exec(select(TjcCoverage)).all()
    gap_codes = {row.ep_code for row in coverage if row.status == "gap"}
    expected_gaps = {"CTS.03.01.09", "CTS.03.01.03", "NPSG.15.01.01", "R3-25", "RC.01.02.01"}
    assert expected_gaps <= gap_codes
    assert any(row.status == "satisfied" for row in coverage)


def test_ingest_is_idempotent(db_session, export_root):
    """Re-ingesting the same export creates no duplicate data rows (PRD §7).

    The data tables are unchanged on the second run; the AuditEvent log, being
    append-only, grows -- the re-ingest is itself a recorded event.
    """
    _clear(db_session)

    first = ingest_export(export_root, db_session)
    db_session.flush()
    data_counts_after_first = _data_row_counts(db_session)
    audit_after_first = _audit_count(db_session)

    second = ingest_export(export_root, db_session)
    db_session.flush()
    data_counts_after_second = _data_row_counts(db_session)
    audit_after_second = _audit_count(db_session)

    assert first["documents_ingested"] == 4
    assert second["documents_ingested"] == 0
    assert second["documents_skipped"] == 4
    # No duplicate data rows...
    assert data_counts_after_first == data_counts_after_second
    # ...but the event-sourced audit log records the re-ingest.
    assert audit_after_second > audit_after_first
