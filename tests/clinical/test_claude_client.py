"""
M6 acceptance tests for the Claude client.

These tests **never** make a real Claude API call. The SDK is mocked at
two layers:

  * ``anthropic.Anthropic`` instance is constructed normally (no real
    requests fly because we replace ``messages.with_raw_response.create``).
  * ``messages.with_raw_response.create`` is monkey-patched with a fake
    that returns / raises whatever the test wants.

The circuit-breaker tests inject a deterministic clock so we don't have
to ``time.sleep`` past the recovery window.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import anthropic
import httpx
import pytest
from sqlmodel import select

from app.clinical.llm.claude_client import (
    CircuitBreaker,
    ClaudeBreakerOpen,
    ClaudeClient,
    ClaudeRateLimited,
    ClaudeUnavailable,
)
from app.db.models import LlmInvocation

# ────────────────────────────────────────────────────────────────────────
# Helpers -- fake SDK objects.
# ────────────────────────────────────────────────────────────────────────


class _FakeUsage:
    """Lookalike for anthropic.types.Usage with just the fields we need."""

    def __init__(
        self,
        input_tokens: int = 100,
        output_tokens: int = 50,
        cache_read_input_tokens: int | None = 0,
        cache_creation_input_tokens: int | None = 0,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read_input_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens


class _FakeMessage:
    """Lookalike for anthropic.types.Message."""

    def __init__(self, usage: _FakeUsage | None = None):
        self.usage = usage or _FakeUsage()
        self.content = []
        self.id = "msg_test"

    def model_dump_json(self) -> str:
        return '{"id":"msg_test","content":[]}'


class _FakeRawResponse:
    """Lookalike for the raw-response wrapper returned by with_raw_response."""

    def __init__(self, message: _FakeMessage, headers: dict[str, str] | None = None):
        self._message = message
        self.headers = httpx.Headers(headers or {"anthropic-ratelimit-requests-remaining": "999"})

    def parse(self) -> _FakeMessage:
        return self._message


def _fake_sdk_returning(message: _FakeMessage | None = None) -> anthropic.Anthropic:
    """Build an anthropic.Anthropic whose messages.with_raw_response.create
    returns a fake raw response.

    Constructing with a fake api_key is fine because we replace the
    network call entirely.
    """
    sdk = anthropic.Anthropic(api_key="test")
    fake_raw = _FakeRawResponse(message or _FakeMessage())
    sdk.messages.with_raw_response.create = MagicMock(return_value=fake_raw)  # type: ignore[method-assign]
    return sdk


def _fake_sdk_raising(exc_factory) -> anthropic.Anthropic:
    """Build an anthropic.Anthropic whose call raises a fresh exception.

    The wrapper ignores call args so MagicMock doesn't try to pass them
    to the factory (factory takes no args).
    """
    sdk = anthropic.Anthropic(api_key="test")

    def _raise(*_args, **_kwargs):
        raise exc_factory()

    sdk.messages.with_raw_response.create = MagicMock(side_effect=_raise)  # type: ignore[method-assign]
    return sdk


def _rate_limit_error() -> anthropic.RateLimitError:
    """Construct a realistic RateLimitError without touching the network."""
    response = httpx.Response(
        429, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def _api_status_error() -> anthropic.APIStatusError:
    """Construct a 500-equivalent APIStatusError."""
    response = httpx.Response(
        500, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    return anthropic.APIStatusError("server error", response=response, body=None)


class _ManualClock:
    """Time source the tests advance by hand. Replaces time.monotonic."""

    def __init__(self, t: float = 0.0):
        self._t = t

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _client_with(
    sdk: anthropic.Anthropic, db_session, *, breaker: CircuitBreaker | None = None
) -> ClaudeClient:
    """Construct a ClaudeClient bound to a test SDK + test DB session."""
    return ClaudeClient(
        sdk_client=sdk,
        breaker=breaker,
        # Wrap the rolled-back db_session in a no-op generator that yields
        # it back. The session_factory contract is "callable that returns
        # something the client can pull a session out of".
        session_factory=lambda: iter([db_session]),
    )


# ────────────────────────────────────────────────────────────────────────
# 1. Happy path.
# ────────────────────────────────────────────────────────────────────────


def test_successful_call_returns_response_and_logs_invocation(db_session):
    """A successful Claude call writes one LlmInvocation row and returns
    a ClaudeResponse with tokens + latency."""
    sdk = _fake_sdk_returning(_FakeMessage(_FakeUsage(input_tokens=120, output_tokens=40)))
    client = _client_with(sdk, db_session)
    patient_id = uuid4()

    response = client.messages_create(
        endpoint="asam-loc",
        patient_id=patient_id,
        messages=[{"role": "user", "content": "test"}],
    )

    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 40
    assert response.response_hash  # non-empty xxhash
    assert response.latency_ms >= 0
    # Rate-limit headers are surfaced.
    assert "anthropic-ratelimit-requests-remaining" in response.rate_limit_headers

    # Exactly one LlmInvocation row was written for THIS patient. Filtering
    # by patient_id makes the test robust to pre-existing rows committed by
    # other test runs or by live curl demos against the dev DB.
    rows = list(
        db_session.exec(select(LlmInvocation).where(LlmInvocation.patient_id == patient_id))
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.endpoint == "asam-loc"
    assert row.prompt_tokens == 120
    assert row.completion_tokens == 40
    assert row.error_class is None
    assert row.citation_validation_passed is True


def test_successful_call_resets_breaker(db_session):
    """A success after partial failures clears the failure counter."""
    clock = _ManualClock()
    breaker = CircuitBreaker(failure_threshold=3, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False  # 2 < threshold

    sdk = _fake_sdk_returning()
    client = _client_with(sdk, db_session, breaker=breaker)
    client.messages_create(
        endpoint="asam-loc",
        patient_id=uuid4(),
        messages=[{"role": "user", "content": "x"}],
    )
    # Success clears the failure history -- so even a single failure later
    # won't open the breaker.
    breaker.record_failure()
    assert breaker.is_open() is False


# ────────────────────────────────────────────────────────────────────────
# 2. Failure paths surface typed exceptions.
# ────────────────────────────────────────────────────────────────────────


def test_rate_limit_error_raises_claude_rate_limited(db_session):
    sdk = _fake_sdk_raising(_rate_limit_error)
    client = _client_with(sdk, db_session)
    patient_id = uuid4()
    with pytest.raises(ClaudeRateLimited):
        client.messages_create(
            endpoint="asam-loc",
            patient_id=patient_id,
            messages=[{"role": "user", "content": "x"}],
        )
    # Failure path still logs an invocation row (with error_class set).
    rows = list(
        db_session.exec(select(LlmInvocation).where(LlmInvocation.patient_id == patient_id))
    )
    assert len(rows) == 1
    assert rows[0].error_class == "ClaudeRateLimited"


def test_status_error_raises_claude_unavailable(db_session):
    sdk = _fake_sdk_raising(_api_status_error)
    client = _client_with(sdk, db_session)
    with pytest.raises(ClaudeUnavailable):
        client.messages_create(
            endpoint="asam-loc",
            patient_id=uuid4(),
            messages=[{"role": "user", "content": "x"}],
        )


# ────────────────────────────────────────────────────────────────────────
# 3. Circuit breaker.
# ────────────────────────────────────────────────────────────────────────


def test_breaker_opens_after_threshold_failures(db_session):
    """Three rate-limit failures in the window open the breaker.
    The next call short-circuits with ClaudeBreakerOpen without
    invoking the SDK."""
    clock = _ManualClock()
    breaker = CircuitBreaker(failure_threshold=3, window_seconds=60, clock=clock)
    sdk = _fake_sdk_raising(_rate_limit_error)
    client = _client_with(sdk, db_session, breaker=breaker)

    for _ in range(3):
        with pytest.raises(ClaudeRateLimited):
            client.messages_create(
                endpoint="asam-loc",
                patient_id=uuid4(),
                messages=[{"role": "user", "content": "x"}],
            )

    assert breaker.is_open() is True
    # The 4th call short-circuits -- the SDK is NOT invoked again.
    sdk_calls_before = sdk.messages.with_raw_response.create.call_count  # type: ignore[attr-defined]
    with pytest.raises(ClaudeBreakerOpen):
        client.messages_create(
            endpoint="asam-loc",
            patient_id=uuid4(),
            messages=[{"role": "user", "content": "x"}],
        )
    sdk_calls_after = sdk.messages.with_raw_response.create.call_count  # type: ignore[attr-defined]
    assert sdk_calls_after == sdk_calls_before


def test_breaker_recovers_after_cooldown(db_session):
    """After recovery_seconds, the breaker enters half-open and the next
    call is allowed through."""
    clock = _ManualClock()
    breaker = CircuitBreaker(
        failure_threshold=3, window_seconds=60, recovery_seconds=60, clock=clock
    )

    # Open the breaker.
    for _ in range(3):
        breaker.record_failure()
    assert breaker.is_open() is True

    # Advance time past recovery.
    clock.advance(61)
    assert breaker.is_open() is False  # half-open

    # A successful call closes the breaker fully.
    sdk = _fake_sdk_returning()
    client = _client_with(sdk, db_session, breaker=breaker)
    client.messages_create(
        endpoint="asam-loc",
        patient_id=uuid4(),
        messages=[{"role": "user", "content": "x"}],
    )
    # Failures after a healthy call don't immediately re-open.
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False


def test_breaker_window_expires_old_failures(db_session):
    """A failure outside the rolling window does not count toward the threshold."""
    clock = _ManualClock()
    breaker = CircuitBreaker(failure_threshold=3, window_seconds=60, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(61)  # the two earlier failures roll out of the window.
    breaker.record_failure()
    assert breaker.is_open() is False


# ────────────────────────────────────────────────────────────────────────
# 4. LlmInvocation row capture detail.
# ────────────────────────────────────────────────────────────────────────


def test_llm_invocation_captures_cache_token_fields(db_session):
    """Prompt-cache reads (Anthropic billed at 0.1×) are surfaced."""
    sdk = _fake_sdk_returning(
        _FakeMessage(
            _FakeUsage(
                input_tokens=200,
                output_tokens=100,
                cache_read_input_tokens=180,  # cached
                cache_creation_input_tokens=20,
            )
        )
    )
    client = _client_with(sdk, db_session)
    # Filter by the test's own patient_id so any pre-existing
    # LlmInvocation rows from live demo runs do not pollute the lookup.
    pid = uuid4()
    client.messages_create(
        endpoint="tjc-audit",
        patient_id=pid,
        messages=[{"role": "user", "content": "x"}],
    )
    row = db_session.exec(select(LlmInvocation).where(LlmInvocation.patient_id == pid)).first()
    assert row.cache_read_tokens == 180
    assert row.cache_creation_tokens == 20


def test_llm_invocation_records_endpoint_and_patient_id(db_session):
    """endpoint + patient_id are the only DB-level columns for slicing."""
    sdk = _fake_sdk_returning()
    client = _client_with(sdk, db_session)
    pid = uuid4()
    client.messages_create(
        endpoint="asam-loc",
        patient_id=pid,
        messages=[{"role": "user", "content": "x"}],
    )
    row = db_session.exec(select(LlmInvocation).where(LlmInvocation.patient_id == pid)).first()
    assert row.endpoint == "asam-loc"
    assert row.patient_id == pid


# ────────────────────────────────────────────────────────────────────────
# 5. Pure CircuitBreaker unit tests.
# ────────────────────────────────────────────────────────────────────────


def test_circuit_breaker_closed_by_default():
    breaker = CircuitBreaker(clock=_ManualClock())
    assert breaker.is_open() is False


def test_circuit_breaker_record_success_clears_state():
    clock = _ManualClock()
    breaker = CircuitBreaker(failure_threshold=3, clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    # After success, two more failures shouldn't open the breaker
    # (the counter started fresh).
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open() is False
