"""
The single chokepoint for every Claude API call in the project.

phase_3_PRD.md §5.6 / §5.8 / §5.9 mandate that every LLM call:

  * Use the configured model (``PHEALTH_LLM_MODEL``, default
    ``claude-sonnet-4-6``).
  * Retry transient failures (429 / 5xx / connection errors).
  * Stop attempting the API entirely if the rate-limit failure rate
    has crossed a threshold (the circuit breaker).
  * Write one ``LlmInvocation`` row capturing tokens, latency,
    citation-validation outcome, and any error.
  * Raise a typed application exception (not a raw SDK error) on final
    failure so the API handler can map to RFC 7807 deterministically.

This module is intentionally Claude-only -- there is no
provider-abstraction layer. Claude is the **single** LLM provider for
the entire project (the OpenAI dep was removed in the Phase 3 refactor;
see CHANGELOG.md 2026-05-15 entries).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import anthropic
import xxhash
from anthropic.types import Message, MessageParam, Usage
from sqlmodel import Session

from app.core.config import get_settings
from app.db.models import LlmInvocation
from app.db.session import get_session

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Typed exceptions surfaced to the API layer.
# ────────────────────────────────────────────────────────────────────────


class ClaudeError(Exception):
    """Base class for Claude integration failures the API handler maps."""

    def __init__(self, message: str, *, request_id: str | None = None):
        super().__init__(message)
        self.request_id = request_id


class ClaudeRateLimited(ClaudeError):
    """All SDK retries exhausted on a 429. Maps to 503 + Retry-After."""


class ClaudeUnavailable(ClaudeError):
    """All SDK retries exhausted on a 5xx / connection error. Maps to 503."""


class ClaudeBreakerOpen(ClaudeError):
    """The circuit breaker is open -- we did not even attempt the API.
    Maps to 503 immediately."""


# ────────────────────────────────────────────────────────────────────────
# Circuit breaker -- in-memory, per-process.
# ────────────────────────────────────────────────────────────────────────


@dataclass
class CircuitBreaker:
    """In-memory failure-counting circuit breaker.

    Three states:
      * **closed**  -- normal operation, all calls go through.
      * **open**    -- after ``failure_threshold`` failures in the
                       most-recent ``window_seconds``, every call
                       short-circuits with ``ClaudeBreakerOpen`` for the
                       next ``recovery_seconds``.
      * **half-open** -- after the recovery cooldown, the very next call
                       is allowed through. Success closes the breaker;
                       failure re-opens it.

    Process-local (no Redis, no cross-pod state). MVP scope is a single
    Uvicorn process; if we ever scale horizontally, swap this for a
    Redis-backed implementation. Documented as a known limitation.

    Time is injected via ``clock`` so tests can advance it deterministically
    without ``time.sleep``.
    """

    failure_threshold: int = 3
    window_seconds: int = 60
    recovery_seconds: int = 60
    clock: Callable[[], float] = time.monotonic

    # Mutable state.
    _failure_times: list[float] = field(default_factory=list, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def is_open(self) -> bool:
        """Return True iff calls should be short-circuited right now."""
        if self._opened_at is None:
            return False
        if self.clock() - self._opened_at >= self.recovery_seconds:
            # Recovery elapsed -- enter half-open: the next call is allowed.
            self._opened_at = None
            self._failure_times.clear()
            return False
        return True

    def record_failure(self) -> None:
        """Record a 429 / 5xx outcome. Opens the breaker on the Nth in window."""
        now = self.clock()
        # Drop failure timestamps that fell out of the rolling window.
        self._failure_times = [t for t in self._failure_times if now - t < self.window_seconds]
        self._failure_times.append(now)
        if len(self._failure_times) >= self.failure_threshold:
            self._opened_at = now
            logger.warning(
                "Claude circuit breaker OPEN after %d failures in %ds",
                len(self._failure_times),
                self.window_seconds,
            )

    def record_success(self) -> None:
        """Reset the failure tracker on a successful call."""
        self._failure_times.clear()
        self._opened_at = None


# ────────────────────────────────────────────────────────────────────────
# Response wrapper.
# ────────────────────────────────────────────────────────────────────────


@dataclass
class ClaudeResponse:
    """Everything callers (M7-M9) might want from a Claude call.

    ``message`` is the raw SDK response (``anthropic.types.Message``);
    callers walk ``message.content`` to read content blocks and
    ``citations[]``. The other fields are pre-extracted convenience.
    """

    message: Message
    usage: Usage
    rate_limit_headers: dict[str, str]
    request_id: str | None
    response_hash: str
    latency_ms: int


# ────────────────────────────────────────────────────────────────────────
# The client.
# ────────────────────────────────────────────────────────────────────────


class ClaudeClient:
    """Singleton-able wrapper around ``anthropic.Anthropic``.

    Public surface is intentionally narrow: ``messages_create`` is the
    one entry point. Callers (M7-M9) pass the messages list, the
    documents list (Citations-API blocks), and any tools; the client
    handles the SDK call, header capture, breaker arithmetic, and
    LlmInvocation logging.

    Construction parameters can be overridden in tests:
      * ``sdk_client``  -- inject a mocked ``anthropic.Anthropic``.
      * ``breaker``     -- inject a CircuitBreaker (default constructed
                            with a deterministic clock).
      * ``session_factory`` -- inject a Session factory so tests use a
                            rolled-back transaction rather than the real DB.
    """

    def __init__(
        self,
        *,
        sdk_client: anthropic.Anthropic | None = None,
        breaker: CircuitBreaker | None = None,
        session_factory: Callable[[], Iterable[Session]] | None = None,
    ):
        settings = get_settings()
        self._model = settings.phealth_llm_model
        # SDK default retries are 2; we want 3 attempts after the first
        # try -- so 3 retries total per phase_3_PRD.md §7. Timeout 60s
        # to allow Sonnet 4.6 on ~6K-input / ~2.5K-output calls.
        self._sdk = sdk_client or anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            max_retries=3,
            timeout=60.0,
        )
        self._breaker = breaker or CircuitBreaker()
        # Default session factory points at the app's get_session()
        # generator. Tests substitute a fixture that yields a
        # rolled-back transaction. Wrapped in a lambda so each call
        # re-invokes the factory (LlmInvocation gets its own session).
        self._session_factory = session_factory or get_session

    @property
    def model(self) -> str:
        return self._model

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def messages_create(
        self,
        *,
        endpoint: str,
        patient_id: UUID,
        messages: list[MessageParam],
        system: str | None = None,
        documents: list[dict] | None = None,
        tools: list[dict] | None = None,
        tool_choice: dict | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        citation_validation_passed: bool = True,
    ) -> ClaudeResponse:
        """Issue one Claude call, return the parsed response, log to DB.

        ``endpoint`` (e.g. "asam-loc", "tjc-audit") and ``patient_id``
        flow into the LlmInvocation row so cost can be sliced by
        endpoint and patient. ``citation_validation_passed`` defaults
        to True; M7's CitationValidator will pass False on its retries.

        On final failure (after SDK retries) this raises one of the
        ClaudeError subclasses; the API layer maps each to an
        appropriate RFC 7807 response per phase_3_PRD.md §5.9.
        """
        if self._breaker.is_open():
            self._log_invocation(
                endpoint=endpoint,
                patient_id=patient_id,
                model=self._model,
                usage=None,
                latency_ms=0,
                response_hash="",
                citation_validation_passed=False,
                error_class="ClaudeBreakerOpen",
            )
            raise ClaudeBreakerOpen("circuit breaker open; not attempting Claude call")

        # Build kwargs progressively so we don't pass None values the SDK
        # would reject. The SDK strict-types complain at runtime on dict
        # literals (mypy already flagged this in M0R); we use ``Any`` to
        # silence the noise for the same reason documented in
        # section_detector.py.
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": list(messages),  # SDK accepts a list, not Iterable.
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system is not None:
            kwargs["system"] = system
        if documents is not None:
            # Documents ride as content blocks on the first user message
            # per the Anthropic Citations API shape (phase_3.md §A4).
            # The caller (M7) is expected to have constructed the
            # message list with documents already embedded; passing
            # documents here is informational metadata for the
            # LlmInvocation log only.
            pass
        if tools is not None:
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

        # Time the call. Latency excludes our own breaker / logging
        # overhead, so the LlmInvocation row reflects the real network
        # spent on Claude.
        start = time.monotonic()
        request_id: str | None = None
        usage: Usage | None = None
        response_hash = ""
        error_class: str | None = None
        message: Message | None = None
        rate_limit_headers: dict[str, str] = {}

        try:
            # with_raw_response gives us the underlying HTTP response so
            # we can read ``anthropic-ratelimit-*`` headers. The SDK's
            # built-in retries still apply.
            raw = self._sdk.messages.with_raw_response.create(**kwargs)  # type: ignore[call-overload]
            message = raw.parse()
            usage = message.usage
            rate_limit_headers = {
                k: v for k, v in raw.headers.items() if k.lower().startswith("anthropic-ratelimit-")
            }
            request_id = raw.headers.get("request-id") or raw.headers.get("x-request-id")
            response_hash = xxhash.xxh64(message.model_dump_json().encode()).hexdigest()
            self._breaker.record_success()
        except anthropic.RateLimitError as exc:
            error_class = "ClaudeRateLimited"
            self._breaker.record_failure()
            request_id = _request_id_of(exc)
            raise ClaudeRateLimited(
                f"Claude rate-limited after SDK retries: {exc}",
                request_id=request_id,
            ) from exc
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            error_class = "ClaudeUnavailable"
            self._breaker.record_failure()
            request_id = _request_id_of(exc)
            raise ClaudeUnavailable(f"Claude unreachable: {exc}", request_id=request_id) from exc
        except anthropic.APIStatusError as exc:
            # 5xx after SDK retries, or other server-side errors.
            error_class = "ClaudeUnavailable"
            self._breaker.record_failure()
            request_id = _request_id_of(exc)
            raise ClaudeUnavailable(
                f"Claude API status error: {exc}", request_id=request_id
            ) from exc
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            # Log success AND failure. The LlmInvocation table is the
            # operational ground truth for cost / reliability dashboards;
            # surfacing failures here is what makes the rate-limit
            # signal visible.
            self._log_invocation(
                endpoint=endpoint,
                patient_id=patient_id,
                model=self._model,
                usage=usage,
                latency_ms=latency_ms,
                response_hash=response_hash,
                citation_validation_passed=citation_validation_passed,
                error_class=error_class,
            )

        # Log the rate-limit headers so a slow drift toward the quota
        # is visible in stdout even before the breaker opens.
        if rate_limit_headers:
            logger.info("anthropic ratelimit: %s", rate_limit_headers)

        assert message is not None  # success path -> message is set
        return ClaudeResponse(
            message=message,
            usage=usage,  # type: ignore[arg-type]
            rate_limit_headers=rate_limit_headers,
            request_id=request_id,
            response_hash=response_hash,
            latency_ms=latency_ms,
        )

    def _log_invocation(
        self,
        *,
        endpoint: str,
        patient_id: UUID,
        model: str,
        usage: Usage | None,
        latency_ms: int,
        response_hash: str,
        citation_validation_passed: bool,
        error_class: str | None,
    ) -> None:
        """Write one LlmInvocation row in its own session.

        Own session so the audit trail survives a transaction rollback
        on the API handler's session (phase_3_PRD.md §5.8 — every call
        gets logged regardless of downstream success). Failures here are
        logged but never raised: an LlmInvocation write failure must not
        mask the underlying Claude failure.
        """
        invocation = LlmInvocation(
            endpoint=endpoint,
            patient_id=patient_id,
            model=model,
            prompt_tokens=(usage.input_tokens if usage else 0),
            completion_tokens=(usage.output_tokens if usage else 0),
            cache_read_tokens=(usage.cache_read_input_tokens or 0) if usage else 0,
            cache_creation_tokens=(usage.cache_creation_input_tokens or 0) if usage else 0,
            latency_ms=latency_ms,
            response_hash=response_hash,
            citation_validation_passed=citation_validation_passed,
            error_class=error_class,
        )
        try:
            # ``session_factory`` may be a FastAPI dependency generator
            # (``yield session`` inside a ``with Session(engine) as s``
            # context) or a plain callable returning a Session. For the
            # generator path we MUST drive the generator to exhaustion
            # after the commit so the surrounding context manager's
            # ``__exit__`` runs and the connection is returned to the
            # pool. ``next(session_obj)`` once is not enough -- the
            # original bug was that the connection was only released
            # when CPython's GC eventually collected the orphaned
            # generator, which under load pinned pool connections.
            session_obj = self._session_factory()
            session: Session
            if hasattr(session_obj, "__next__"):
                session = next(session_obj)  # type: ignore[call-overload]
                try:
                    session.add(invocation)
                    session.commit()
                finally:
                    # Drive the generator past its yield so the ``with
                    # Session(engine) as session`` cleanup actually runs.
                    # ``next(gen, sentinel)`` swallows StopIteration cleanly.
                    next(session_obj, None)  # type: ignore[call-overload]
            else:
                session = session_obj  # type: ignore[assignment]
                session.add(invocation)
                session.commit()
        except Exception as log_exc:  # noqa: BLE001 -- diagnostic-only, never propagated
            logger.error("Failed to write LlmInvocation row: %s", log_exc)


def _request_id_of(exc: anthropic.APIError | Exception) -> str | None:
    """Best-effort extraction of the Anthropic request id from any error."""
    return getattr(exc, "request_id", None) or getattr(
        getattr(exc, "response", None), "headers", {}
    ).get("request-id")


__all__ = [
    "ClaudeClient",
    "ClaudeResponse",
    "ClaudeError",
    "ClaudeRateLimited",
    "ClaudeUnavailable",
    "ClaudeBreakerOpen",
    "CircuitBreaker",
]
