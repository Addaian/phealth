"""
API-key authentication for state-changing endpoints.

The ingest endpoints (POST /ingest/*) are guarded by a shared secret passed in
the ``X-API-Key`` header. Read-only GET endpoints are intentionally
unauthenticated for the local demo — see PRD §5.4 and implementation plan §M8.
This is not production auth; it is a deliberate, documented MVP boundary.
"""

from fastapi import Header, HTTPException, status

from app.core.config import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency: reject the request unless ``X-API-Key`` matches config.

    Raises HTTP 401 if the header is missing or wrong. The header is declared
    optional (``default=None``) so a *missing* key yields a clean 401 rather
    than FastAPI's 422-for-missing-required-header. Attach to a route with
    ``dependencies=[Depends(require_api_key)]``.
    """
    if x_api_key is None or x_api_key != get_settings().ingest_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key.",
        )
