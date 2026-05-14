"""
FHIR API router.

In Phase 1 the FHIR transaction Bundle is served as GET /patients/{id}/fhir-bundle
(implemented in M7, assembled by ``app.fhir.mappers``). This router carries no
prefix so it can also host future per-resource endpoints (e.g.
GET /fhir/Patient/{id}) without a structural change — every relational row
already stores its canonical ``fhir_json``, so adding those is a near no-op.

This module exists in M4 for wiring.
"""

from fastapi import APIRouter

router = APIRouter(tags=["fhir"])
