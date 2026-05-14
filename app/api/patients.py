"""
Patient-facing read API router.

Exposes demographics, intake, observations, ASAM evidence, and TJC coverage
for a patient (PRD §5.4). Routes are implemented in M8; this module exists in
M4 to establish the wiring.

Planned routes:
    GET /patients/{id}
    GET /patients/{id}/intake
    GET /patients/{id}/observations
    GET /patients/{id}/asam-evidence
    GET /patients/{id}/tjc-coverage
    GET /patients/{id}/fhir-bundle   (assembled by app.fhir.mappers, see M7)
"""

from fastapi import APIRouter

router = APIRouter(prefix="/patients", tags=["patients"])
