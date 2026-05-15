"""
Shared "patient compartment" SQL helpers.

The pattern "load the patient's documents, then load every
ExtractedObservation for those documents" appeared in five places
during Phase 1-3 development:

  * ``app/api/patients.py::get_patient_observations``
  * ``app/api/fhir.py::_load_patient_compartment``
  * ``app/clinical/shared/evidence_hash.py::compute_evidence_hash``
  * ``app/clinical/asam/risk_ratings.py::_load_snapshot``
  * ``app/clinical/tjc/audit_functions.py::load_snapshot``

The bodies were near-identical: one SELECT on ClinicalDocument
filtered by patient_id, then one IN-list SELECT on
ExtractedObservation. Centralised here so the IN-list semantics
(filtering, ordering) live in one place; future SQL-side fixes
(pushing the LIMIT into the query, switching to a CTE, etc.) update
one helper instead of five.

This module deliberately does NOT depend on any clinical-reasoning
package -- it lives under ``app/clinical/shared`` rather than
``app/db/queries`` only because the consumers cluster around the
``app/clinical/*`` tree. If a non-clinical caller ever needs the
same shape, move this to ``app/db/queries.py`` -- nothing here is
clinical.
"""

from __future__ import annotations

from uuid import UUID

from sqlmodel import Session, select

from app.db.models import ClinicalDocument, ExtractedObservation


def load_patient_documents(db: Session, patient_id: UUID) -> list[ClinicalDocument]:
    """Return every ClinicalDocument for ``patient_id``.

    Ordering is the natural id order returned by Postgres; callers that
    need a specific order (e.g., most-recent first) should sort the
    list themselves.
    """
    return list(db.exec(select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_id)))


def load_patient_observations(
    db: Session,
    patient_id: UUID,
    *,
    code_system: str | None = None,
) -> list[ExtractedObservation]:
    """Return every ExtractedObservation for the patient's documents.

    Optional ``code_system`` filter narrows to a single code system
    (the FHIR Observation surface uses ``code_system="LOINC"`` to
    exclude entity-tagger rows). When the patient has no documents,
    returns an empty list with a single SELECT, not zero.

    Two SELECTs total (docs IN-list, then observations IN-list). The
    older copy-pasted versions sometimes used a 3-query patient ->
    documents -> observations pattern; this collapses that to two by
    operating on ``ExtractedObservation.document_id.in_(...)`` directly.
    """
    docs = load_patient_documents(db, patient_id)
    doc_ids = [doc.id for doc in docs]
    if not doc_ids:
        return []
    query = select(ExtractedObservation).where(
        ExtractedObservation.document_id.in_(doc_ids)  # type: ignore[attr-defined]
    )
    if code_system is not None:
        query = query.where(ExtractedObservation.code_system == code_system)
    return list(db.exec(query))


__all__ = ["load_patient_documents", "load_patient_observations"]
