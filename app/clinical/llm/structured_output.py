"""
Schema-constrained Claude responses.

phase_3_PRD.md §5.6 calls for Anthropic's native **Structured Outputs**
(``response_format={"type": "json_schema", ...}``) as the primary
schema-enforcement path. The anthropic Python SDK at version 0.102.0
does not yet expose ``response_format`` as a top-level parameter on
``messages.create``; this module therefore implements the **sanctioned
fallback** path the PRD documents (§5.6 last paragraph): forced
tool-use with a single tool whose ``input_schema`` is the response
model's ``model_json_schema()``.

This is structurally equivalent to native Structured Outputs:
  * Claude is constrained to emit exactly one tool_use block.
  * That block's ``input`` payload is JSON validated against the schema.
  * We parse the payload through the same Pydantic model.

When/if the SDK ships ``response_format``, swap the body of
``call_with_schema`` to use it directly; the module's public surface
stays identical and no caller has to change.

phase_3_implementation_plan.md M7 lists this as the M7 deliverable,
and the same tool-use pattern is already exercised in production for
the section-detector fallback (``app/ingest/section_detector.py``).
"""

from __future__ import annotations

from typing import Any, TypeVar
from uuid import UUID

from anthropic.types import MessageParam
from pydantic import BaseModel, ValidationError

from app.clinical.llm.claude_client import ClaudeClient, ClaudeResponse


class StructuredOutputError(Exception):
    """Raised when the structured-output payload could not be coerced
    into the requested Pydantic model. The API handler maps this to a
    502 + RFC 7807 ``/errors/llm-malformed`` per phase_3_PRD.md §5.9.
    """

    def __init__(self, message: str, *, raw_payload: Any | None = None):
        super().__init__(message)
        self.raw_payload = raw_payload


T = TypeVar("T", bound=BaseModel)


# The fixed tool name used for forced structured output. Visible only to
# Claude (the model sees it as the tool to call) and to our parser. Not
# user-facing.
_TOOL_NAME = "submit_structured_response"


def call_with_schema(
    client: ClaudeClient,
    response_model: type[T],
    *,
    endpoint: str,
    patient_id: UUID,
    messages: list[MessageParam],
    system: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    citation_validation_passed: bool = True,
) -> tuple[T, ClaudeResponse]:
    """Call Claude with a Pydantic-schema-constrained response.

    Returns a tuple of ``(parsed_model, raw_response)`` so the caller
    can both consume the typed payload and access the response message
    for citation walking (M7.2's CitationValidator runs over the raw
    response, not the parsed model).

    Raises ``StructuredOutputError`` if Claude does not emit the forced
    tool_use block or if the payload fails Pydantic validation.
    Underlying Claude failures (rate limit, breaker open, etc.) bubble
    up as the usual ``ClaudeError`` subclasses.
    """
    # Build the tool from the Pydantic model. Pydantic's
    # model_json_schema() emits a draft-2020 JSON schema; Anthropic's
    # tool input_schema accepts the same dialect (per their docs).
    schema = response_model.model_json_schema()
    tool: dict[str, Any] = {
        "name": _TOOL_NAME,
        "description": (
            f"Submit the structured response in the {response_model.__name__} shape. "
            "Every field in the schema must be present; do not omit optional fields "
            "without providing a documented value."
        ),
        "input_schema": schema,
    }

    response = client.messages_create(
        endpoint=endpoint,
        patient_id=patient_id,
        messages=messages,
        system=system,
        tools=[tool],
        tool_choice={"type": "tool", "name": _TOOL_NAME},
        max_tokens=max_tokens,
        temperature=temperature,
        citation_validation_passed=citation_validation_passed,
    )

    # Walk content blocks for the forced tool_use payload. Claude may
    # emit a leading text block (e.g., an empty pre-tool narration);
    # we tolerate that and pick out the tool_use.
    payload: Any | None = None
    for block in response.message.content:
        is_tool_use = getattr(block, "type", None) == "tool_use"
        is_our_tool = getattr(block, "name", None) == _TOOL_NAME
        if is_tool_use and is_our_tool:
            payload = getattr(block, "input", None)
            break

    if payload is None:
        raise StructuredOutputError(f"Claude response had no tool_use block for {_TOOL_NAME!r}")

    try:
        parsed = response_model.model_validate(payload)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"Claude tool_use payload failed {response_model.__name__} validation: {exc}",
            raw_payload=payload,
        ) from exc

    return parsed, response


__all__ = [
    "call_with_schema",
    "StructuredOutputError",
]
