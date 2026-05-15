"""
Phase 3 FastAPI dependencies -- Claude client + a shared NarrationOutcome
factory used by both ASAM and TJC endpoints to convert SDK-level Claude
failures into HTTP responses per phase_3_PRD.md §5.9.

Kept in its own module so the Phase 1/2 ``app/api/deps.py`` stays
focused on the shared dependencies (session, patient lookup, audit).
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import HTTPException

from app.clinical.llm.claude_client import (
    ClaudeBreakerOpen,
    ClaudeClient,
    ClaudeRateLimited,
)
from app.clinical.llm.structured_output import StructuredOutputError


@lru_cache(maxsize=1)
def get_claude_client() -> ClaudeClient:
    """Return the process-wide ClaudeClient singleton.

    Singleton because the circuit breaker is in-memory and per-process
    (phase_3_PRD.md §5.9): every request must share the same breaker
    so failure-counts are global, not per-request. ``lru_cache``
    handles the singleton; tests substitute via dependency_overrides
    on the FastAPI app.

    The client is constructed lazily (first request after startup)
    rather than eagerly at module load, so missing ANTHROPIC_API_KEY
    doesn't crash the import path. With a missing key the Anthropic
    SDK constructor succeeds but the first call raises
    ``AuthenticationError`` (an ``APIStatusError`` subclass), which
    the ClaudeClient catches and re-raises as ``ClaudeUnavailable`` --
    the endpoint then returns 200 with rule-engine-only output per
    phase_3_PRD.md §5.9.
    """
    return ClaudeClient()


def raise_http_for_claude_error(exc: Exception) -> None:
    """Map common ``ClaudeError`` subclasses to HTTPException per
    phase_3_PRD.md §5.9.

    The shared mapping is identical between the ASAM and TJC endpoints:

      * ``ClaudeBreakerOpen``    -> 503 + Retry-After: 60
      * ``ClaudeRateLimited``    -> 503 + Retry-After: 30
      * ``StructuredOutputError`` -> 502

    ``ClaudeUnavailable`` is intentionally NOT mapped here -- each
    endpoint degrades to its own engine-only payload (the ASAM and TJC
    degraded shapes differ), so the per-endpoint ``except
    ClaudeUnavailable`` block builds the right NarrationOutcome.

    Always raises -- the function only returns when ``exc`` is not one
    of the mapped types, in which case the caller must re-raise (the
    typical pattern is ``except (A, B, C) as exc: raise_http_for_claude_error(exc)``
    which guarantees exactly the mapped types reach here).
    """
    if isinstance(exc, ClaudeBreakerOpen):
        raise HTTPException(
            status_code=503,
            detail=f"Claude circuit breaker open: {exc}",
            headers={"Retry-After": "60"},
        ) from exc
    if isinstance(exc, ClaudeRateLimited):
        raise HTTPException(
            status_code=503,
            detail=f"Claude rate-limited: {exc}",
            headers={"Retry-After": "30"},
        ) from exc
    if isinstance(exc, StructuredOutputError):
        raise HTTPException(
            status_code=502,
            detail=f"Claude returned malformed structured output: {exc}",
        ) from exc
    # Not one of the mapped types -- the caller is mis-using the helper.
    raise exc


__all__ = ["get_claude_client", "raise_http_for_claude_error"]
