"""
Progress-note read API.

Serves the patient timeline: the progress notes (SOAP / DAP / DSAP -- everything
that is not the BPS intake) ordered by ``authored_on``, each with the PRD §5.4
envelope ``{date, author, type, sections, raw_text}``.

Mounted under the ``/patients`` prefix so the timeline reads as a sub-resource
of the patient, alongside the other patient read endpoints.
"""

from fastapi import APIRouter, Depends
from sqlmodel import select

from app.api.deps import PatientDep, SessionDep, audit_read
from app.api.schemas import TimelineEntry
from app.core.security import require_api_key
from app.db.models import ClinicalDocument

router = APIRouter(
    prefix="/api/v1/patients",
    tags=["notes"],
    dependencies=[Depends(require_api_key), Depends(audit_read)],
)


@router.get("/{patient_id}/timeline", response_model=list[TimelineEntry])
def get_patient_timeline(patient: PatientDep, session: SessionDep) -> list[TimelineEntry]:
    """The patient's progress notes, oldest first.

    "Not the BPS intake" identifies the progress notes -- the only document
    types are bps_intake / soap / dap / dsap. Sorted in Python by
    ``authored_on`` to keep the query mypy-clean.
    """
    notes = session.exec(
        select(ClinicalDocument).where(
            ClinicalDocument.patient_id == patient.id,
            ClinicalDocument.document_type != "bps_intake",
        )
    ).all()
    ordered = sorted(notes, key=lambda note: note.authored_on)

    return [
        TimelineEntry(
            document_id=note.id,
            date=note.authored_on,
            author=note.author_name,
            type=note.document_type,
            sections=note.sections,
            raw_text=note.raw_text,
        )
        for note in ordered
    ]
