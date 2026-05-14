"""
Progress-note read API router.

Holds the timeline endpoint — progress notes ordered by ``authored_on``, each
with ``{date, author, type, sections, raw_text}`` (PRD §5.4). Mounted under the
``/patients`` prefix so the timeline reads as a sub-resource of the patient.
Routes are implemented in M8; this module exists in M4 for wiring.

Planned route:
    GET /patients/{id}/timeline
"""

from fastapi import APIRouter

router = APIRouter(prefix="/patients", tags=["notes"])
