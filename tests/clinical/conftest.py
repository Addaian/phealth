"""
Shared Phase 3 test fixtures.

The five clinical test modules (`test_claude_client`, `test_*_narration`,
`test_endpoints_*`, `test_demo_flow`, `test_fhir_clinical_decision`) all
need the same mocked-Anthropic-SDK plumbing: build a fake `Message`
that carries a tool_use block with a canned payload, wrap it in a
fake raw response carrying rate-limit headers, and inject a
ClaudeClient that returns those into the app via dependency_overrides.

Before this conftest, that code lived in copy-pasted form across four
files. Pulled here so a change to (e.g.) the Citations API shape in
the SDK only updates one place.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from sqlmodel import Session

import app.api.deps_clinical as deps_clinical
from app.clinical.llm.claude_client import ClaudeClient
from app.main import app

# ────────────────────────────────────────────────────────────────────────
# Fake Anthropic objects -- shaped to satisfy the bits of the SDK the
# ClaudeClient reads. SimpleNamespace is enough; we never call
# ``isinstance`` against the real Message / Usage / RawMessageStream
# classes from inside the client.
# ────────────────────────────────────────────────────────────────────────


def fake_usage(
    input_tokens: int = 4000,
    output_tokens: int = 800,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> SimpleNamespace:
    """Build a stand-in for ``anthropic.types.Usage``."""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
    )


def fake_tool_use_message(
    payload: dict,
    *,
    tool_name: str = "submit_structured_response",
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    """Build a stand-in ``Message`` whose only content block is a forced tool_use.

    Matches the shape ``call_with_schema`` expects: one tool_use block
    named ``submit_structured_response`` (the structured-output tool the
    project uses), with ``input`` being the payload Claude would have
    emitted. Plain-text content blocks are intentionally NOT included
    because forced tool-use suppresses them in practice.
    """
    return SimpleNamespace(
        usage=usage or fake_usage(),
        content=[
            SimpleNamespace(
                type="tool_use",
                name=tool_name,
                input=payload,
                id="tool_use_1",
            )
        ],
        id="msg_test",
        model_dump_json=lambda: '{"id":"msg_test"}',
    )


def fake_text_only_message() -> SimpleNamespace:
    """Build a ``Message`` with only text content -- no tool_use.

    Used to exercise the structured-output parser's "missing tool_use"
    failure path.
    """
    return SimpleNamespace(
        usage=fake_usage(),
        content=[SimpleNamespace(type="text", text="hello", citations=None)],
        id="msg_test",
        model_dump_json=lambda: '{"id":"msg_test"}',
    )


def fake_raw_response(message: SimpleNamespace) -> SimpleNamespace:
    """Wrap a fake message in the ``with_raw_response.create`` return shape.

    The real SDK returns a ``RawResponse`` object exposing ``.parse()``
    and ``.headers``; the client only touches those two surfaces.
    """
    return SimpleNamespace(
        parse=lambda: message,
        headers=httpx.Headers({"anthropic-ratelimit-requests-remaining": "999"}),
    )


# ────────────────────────────────────────────────────────────────────────
# ClaudeClient factories.
# ────────────────────────────────────────────────────────────────────────


def make_mocked_claude_client(
    db_session: Session,
    *,
    side_effect: Any = None,
    payloads: list[dict] | None = None,
) -> ClaudeClient:
    """Build a ClaudeClient whose SDK is mocked to a queued sequence.

    Exactly one of ``side_effect`` (a callable / iterable of exceptions
    or fakes) or ``payloads`` (a list of tool-use payloads) should be
    supplied. The fake SDK is wired so successive calls return / raise
    successive items.

    The client's session_factory is bound to the test's rolled-back
    ``db_session``; LlmInvocation rows written by the client land in
    that transaction and rollback at test end.
    """
    sdk = anthropic.Anthropic(api_key="test")
    if side_effect is not None:
        sdk.messages.with_raw_response.create = MagicMock(  # type: ignore[method-assign]
            side_effect=side_effect
        )
    else:
        responses = [fake_raw_response(fake_tool_use_message(p)) for p in (payloads or [])]
        sdk.messages.with_raw_response.create = MagicMock(  # type: ignore[method-assign]
            side_effect=responses
        )
    return ClaudeClient(sdk_client=sdk, session_factory=lambda: iter([db_session]))


@pytest.fixture
def mocked_client_factory(db_session, api_client):
    """Yield an installer that overrides ``get_claude_client`` per-test.

    Each call replaces the FastAPI dependency with a fresh mocked
    client (so the test can swap the SDK mid-test, e.g., first POST
    succeeds, second POST raises). Cleanup runs at fixture teardown
    via ``api_client``'s broad ``dependency_overrides.clear()``.
    """
    override_target = deps_clinical.get_claude_client

    def _install(*, side_effect: Any = None, payloads: list[dict] | None = None) -> ClaudeClient:
        client = make_mocked_claude_client(db_session, side_effect=side_effect, payloads=payloads)
        app.dependency_overrides[override_target] = lambda: client
        return client

    yield _install
