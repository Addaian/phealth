"""
Pydantic models for the ASAM Level-of-Care endpoint.

Two layered shapes, intentionally separated:

  * ``AsamRationale`` and friends -- what **Claude emits** in its
    structured-output tool_use payload. Narrative text, per-subdimension
    rationale, and inline citation objects.
  * ``AsamAssessmentRead`` and friends -- what the **API returns**.
    Combines the rule engine's outputs (level, ratings, rules_fired)
    with the LLM narrative on top. Matches phase_3_PRD.md §6.1
    verbatim.

Why the split: the LLM only ever produces the narrative-shaped subset.
The structural facts (level, ratings, modifier flags, rules fired,
evidence_hash, etc.) come from the deterministic engine in M2-M4. The
M8 narration code threads them together in the response_builder.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ────────────────────────────────────────────────────────────────────────
# Inline citation -- used in both shapes (LLM emits it, response returns it).
# ────────────────────────────────────────────────────────────────────────


class Citation(BaseModel):
    """One char-offset citation into a ClinicalDocument's raw_text.

    Emitted by the LLM in its tool_use payload (one per evidence
    pointer). The CitationValidator verifies each one against the local
    DB before the assessment is persisted (phase_3_PRD.md §5.6).
    """

    document_id: UUID = Field(description="ClinicalDocument UUID this citation points into.")
    char_start: int = Field(ge=0, description="0-indexed start offset in raw_text.")
    char_end: int = Field(ge=0, description="0-indexed end offset in raw_text (exclusive).")
    snippet: str = Field(min_length=1, description="The verbatim text being cited.")


# ────────────────────────────────────────────────────────────────────────
# LLM-emitted shape (the AsamRationale tool_use schema).
# ────────────────────────────────────────────────────────────────────────


class SubdimensionRationale(BaseModel):
    """Per-subdimension narration emitted by Claude.

    ``name`` is the rubric Subdimension enum value (snake_case, e.g.,
    "dim1_withdrawal"). The narration code joins this back against the
    rule-engine output to attach the rating + min_level.
    """

    name: str = Field(description="Subdimension key (snake_case, matches Subdimension enum).")
    rationale: str = Field(
        min_length=1,
        description="One-sentence narrative explaining the subdimension's rating.",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="Evidence citations. Empty for default-on-missing dims.",
    )


class DimensionRationale(BaseModel):
    """Per-dimension narration emitted by Claude."""

    dimension: int = Field(ge=1, le=6, description="ASAM dimension number 1-6.")
    rationale: str = Field(
        min_length=1,
        description="Dimension-level narrative (≤40 words per PRD §6.6).",
    )
    subdimensions: list[SubdimensionRationale] = Field(default_factory=list)


class AsamRationale(BaseModel):
    """The full LLM tool_use payload.

    This is the schema passed to ``call_with_schema``. Claude is forced
    to emit a tool_use block whose ``input`` matches this shape.
    Validation failure -> ``StructuredOutputError`` -> M8's degraded
    fallback path (phase_3_PRD.md §5.9).
    """

    overall_rationale: str = Field(
        min_length=1,
        description="High-level narrative (≤120 words per PRD §6.6).",
    )
    dimensions: list[DimensionRationale] = Field(
        default_factory=list,
        description="Per-dimension narration. Order: dimensions 1-5 (Dim 6 has no ratings).",
    )
    # NOTE: LLM-emitted confidence is informational only. The engine's
    # confidence (PRD §6.7) is computed deterministically and overrides
    # this in the final response.
    confidence: Literal["high", "moderate", "low"] = Field(
        default="moderate",
        description="LLM self-rated confidence in the narrative.",
    )


# ────────────────────────────────────────────────────────────────────────
# Final API response shape (phase_3_PRD.md §6.1).
# ────────────────────────────────────────────────────────────────────────


class Recommendation(BaseModel):
    """The recommended Level-of-Care, exactly as it appears in the API response."""

    level: str = Field(description="Canonical level token, e.g. '3.7', '3.7-BIO', '2.5-COE'.")
    level_display: str = Field(description="Human-readable name from MIN_LOC_DISPLAY.")
    modifiers: list[str] = Field(
        default_factory=list, description="Applied modifiers, e.g. ['COE']."
    )
    co_occurring_enhanced: bool
    biomedical_enhanced: bool


class SubdimensionRead(BaseModel):
    """Final-response per-subdimension entry (PRD §6.1)."""

    name: str
    rating: str
    min_level: str
    rationale: str
    citations: list[Citation] = Field(default_factory=list)


class DimensionRead(BaseModel):
    """Final-response per-dimension entry (PRD §6.1)."""

    dimension: int = Field(ge=1, le=6)
    name: str
    subdimensions: list[SubdimensionRead]
    rationale: str
    confidence: Literal["high", "moderate", "low"]


class AsamAssessmentRead(BaseModel):
    """The complete API response for ``POST /api/v1/patients/{id}/asam-loc``.

    Matches phase_3_PRD.md §6.1 verbatim. Built by
    ``app.clinical.shared.response_builder.build_asam_response`` from
    the rule engine's ``LevelDecision`` + the LLM's ``AsamRationale``.
    """

    id: UUID
    patient_id: UUID
    computed_at: datetime
    evidence_hash: str
    model_version: str
    cached: bool

    recommendation: Recommendation
    dimensions: list[DimensionRead]
    rules_fired: list[str]
    rationale: str
    confidence: Literal["high", "moderate", "low"]

    # ok | unavailable | degraded -- phase_3_PRD.md §5.9.
    rationale_status: Literal["ok", "unavailable", "degraded"] = "ok"
    rationale_warnings: list[str] = Field(default_factory=list)

    # Self-link + FHIR cross-link surface (PRD §6.1).
    links: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "Citation",
    "SubdimensionRationale",
    "DimensionRationale",
    "AsamRationale",
    "Recommendation",
    "SubdimensionRead",
    "DimensionRead",
    "AsamAssessmentRead",
]
