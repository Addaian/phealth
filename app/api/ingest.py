"""
Ingest API router — accepts a SimplePractice Data Export and starts background
ingestion.

Routes are implemented in M6 (ingestion pipeline). In M4 this module exists so
the application wiring is in place: ``app.main`` includes this router, and the
ingest endpoints will be guarded by the ``X-API-Key`` dependency from
``app.core.security``.

Planned route:
    POST /ingest/simplepractice-zip  -> 202 Accepted, backgrounds the ingest job.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/ingest", tags=["ingest"])
