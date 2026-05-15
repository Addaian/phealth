"""
M7 unit tests for the structured-output wrapper.

Tests verify the tool-use-as-structured-output path end-to-end with a
mocked SDK:

  * The wrapper constructs a single forced tool whose ``input_schema``
    matches the Pydantic model's ``model_json_schema()``.
  * The wrapper parses the tool_use block's ``input`` into the model.
  * Schema-violating payloads raise ``StructuredOutputError``.
  * Missing tool_use block raises ``StructuredOutputError``.

The same mocked-SDK helper pattern from ``test_claude_client.py`` is
used (in-process, zero network).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal
from unittest.mock import MagicMock
from uuid import uuid4

import anthropic
import httpx
import pytest
from pydantic import BaseModel, Field

from app.clinical.llm.claude_client import ClaudeClient
from app.clinical.llm.structured_output import (
    StructuredOutputError,
    call_with_schema,
)

# ─── Pydantic models for testing ─────────────────────────────────────────


class _Greeting(BaseModel):
    """A tiny model the structured-output call targets."""

    greeting: str = Field(min_length=1)
    confidence: Literal["high", "moderate", "low"] = "high"


# ─── Fake SDK helpers (mirroring test_claude_client.py shapes) ──────────


def _fake_message_with_tool_use(payload: dict, tool_name: str = "submit_structured_response"):
    """Build a fake message.content with one tool_use block."""
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        content=[
            SimpleNamespace(
                type="tool_use",
                name=tool_name,
                input=payload,
                id="tool_use_1",
            ),
        ],
        id="msg_test",
        model_dump_json=lambda: '{"id":"msg_test"}',
    )


def _fake_message_with_text_only():
    """Build a fake message that has only text content -- no tool_use."""
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        content=[SimpleNamespace(type="text", text="hello", citations=None)],
        id="msg_test",
        model_dump_json=lambda: '{"id":"msg_test"}',
    )


def _fake_raw_response(message):
    return SimpleNamespace(
        parse=lambda: message,
        headers=httpx.Headers({"anthropic-ratelimit-requests-remaining": "999"}),
    )


def _client_returning_payload(payload: dict, db_session) -> ClaudeClient:
    sdk = anthropic.Anthropic(api_key="test")
    sdk.messages.with_raw_response.create = MagicMock(  # type: ignore[method-assign]
        return_value=_fake_raw_response(_fake_message_with_tool_use(payload))
    )
    return ClaudeClient(sdk_client=sdk, session_factory=lambda: iter([db_session]))


def _client_returning_text_only(db_session) -> ClaudeClient:
    sdk = anthropic.Anthropic(api_key="test")
    sdk.messages.with_raw_response.create = MagicMock(  # type: ignore[method-assign]
        return_value=_fake_raw_response(_fake_message_with_text_only())
    )
    return ClaudeClient(sdk_client=sdk, session_factory=lambda: iter([db_session]))


# ─── Happy path ──────────────────────────────────────────────────────────


def test_call_with_schema_returns_parsed_model(db_session):
    """The wrapper parses the tool_use payload through Pydantic and
    returns (parsed_model, raw_response)."""
    client = _client_returning_payload(
        {"greeting": "hello world", "confidence": "high"}, db_session
    )
    parsed, response = call_with_schema(
        client,
        _Greeting,
        endpoint="test",
        patient_id=uuid4(),
        messages=[{"role": "user", "content": "say hi"}],
    )
    assert isinstance(parsed, _Greeting)
    assert parsed.greeting == "hello world"
    assert parsed.confidence == "high"
    assert response.usage.input_tokens == 100


def test_call_with_schema_passes_forced_tool_choice(db_session):
    """The wrapper must force the tool via tool_choice = {type: tool, name: ...}.

    Otherwise Claude could decline to call it and return free-text instead.
    """
    client = _client_returning_payload({"greeting": "ok"}, db_session)
    call_with_schema(
        client,
        _Greeting,
        endpoint="test",
        patient_id=uuid4(),
        messages=[{"role": "user", "content": "x"}],
    )
    # Inspect the kwargs of the (only) SDK call.
    sdk_mock = client._sdk.messages.with_raw_response.create  # type: ignore[attr-defined]
    kwargs = sdk_mock.call_args.kwargs
    assert kwargs["tool_choice"] == {
        "type": "tool",
        "name": "submit_structured_response",
    }
    assert len(kwargs["tools"]) == 1
    tool = kwargs["tools"][0]
    assert tool["name"] == "submit_structured_response"
    # input_schema must come from the Pydantic model's JSON schema.
    expected_schema = _Greeting.model_json_schema()
    assert tool["input_schema"] == expected_schema


# ─── Failure modes ───────────────────────────────────────────────────────


def test_call_with_schema_raises_on_missing_tool_use(db_session):
    """If Claude doesn't emit the forced tool_use block, raise the typed
    StructuredOutputError so the API layer can map to 502 + RFC 7807."""
    client = _client_returning_text_only(db_session)
    with pytest.raises(StructuredOutputError):
        call_with_schema(
            client,
            _Greeting,
            endpoint="test",
            patient_id=uuid4(),
            messages=[{"role": "user", "content": "x"}],
        )


def test_call_with_schema_raises_on_pydantic_validation_failure(db_session):
    """A payload that fails the Pydantic model validation -> StructuredOutputError.

    Here the schema requires ``greeting`` with min_length=1; an empty
    string fails that constraint.
    """
    client = _client_returning_payload({"greeting": "", "confidence": "high"}, db_session)
    with pytest.raises(StructuredOutputError) as exc_info:
        call_with_schema(
            client,
            _Greeting,
            endpoint="test",
            patient_id=uuid4(),
            messages=[{"role": "user", "content": "x"}],
        )
    # The raw payload is preserved on the exception for debugging.
    assert exc_info.value.raw_payload == {"greeting": "", "confidence": "high"}


def test_call_with_schema_raises_on_missing_required_field(db_session):
    """Pydantic rejects a payload that omits a required field."""
    client = _client_returning_payload({"confidence": "high"}, db_session)  # no greeting
    with pytest.raises(StructuredOutputError):
        call_with_schema(
            client,
            _Greeting,
            endpoint="test",
            patient_id=uuid4(),
            messages=[{"role": "user", "content": "x"}],
        )
