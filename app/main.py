"""
FastAPI application entry point.

Wires the API routers together and exposes the liveness probe. Run locally with:

    uvicorn app.main:app --reload

or via docker compose (see docker-compose.yml). Interactive API docs are at
``/docs`` once the server is up.

In M4 (repo scaffold) the routers are deliberately thin — they exist so the
application wiring is proven. Routes are implemented in M6 (ingest) and M8
(read API).
"""

from fastapi import FastAPI

from app.api import fhir as fhir_api
from app.api import ingest, notes, patients

app = FastAPI(
    title="Perspectives Health — Clinical Ingestion Substrate",
    description=(
        "FHIR-R4-shaped ingestion and extraction substrate over the "
        "SimplePractice Data Export. Phase 1 of the intern technical "
        "assessment — see documents/phase_1_PRD.md."
    ),
    version="0.1.0",
)

app.include_router(ingest.router)
app.include_router(patients.router)
app.include_router(notes.router)
app.include_router(fhir_api.router)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe. Returns 200 with a static body; touches no dependencies."""
    return {"status": "ok"}
