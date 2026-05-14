"""
API-key authentication for state-changing endpoints.

The ingest endpoints (POST /ingest/*) are guarded by a shared secret passed in
the ``X-API-Key`` header. Read-only GET endpoints are intentionally
unauthenticated for the local demo — see PRD §5.4 and implementation plan §M8.
This is not production auth; it is a deliberate, documented MVP boundary.
"""

from fastapi import Header, HTTPException, status

from app.core.config import get_settings


def require_api_key(x_api_key: str = Header(...)) -> None:
    """FastAPI dependency: reject the request unless ``X-API-Key`` matches config.

    Raises HTTP 401 if the header is missing or wrong. Attach to a route with
    ``dependencies=[Depends(require_api_key)]``.
    """
    if x_api_key != get_settings().ingest_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key.",
        )
