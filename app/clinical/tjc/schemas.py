"""
Pydantic models for the TJC compliance-audit endpoint.

Same layering as the ASAM schemas (``app.clinical.asam.schemas``):

  * ``TjcAuditResponse`` -- the **LLM tool_use payload**, narrative-only.
    13 finding rows, each carrying a surveyor-RFI narrative + (for
    positive findings) citation list.
  * ``TjcAuditRead`` -- the **API response shape**, matching
    phase_3_PRD.md §6.2 verbatim. Combines the engine's EpFinding
    metadata (status, severity, planted_gap linkage) with the LLM's
    narrative on top.

The narration code in ``app.clinical.tjc.narration`` joins the LLM's
narrative to the engine's findings by ``ep_code``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.clinical.asam.schemas import Citation

# ────────────────────────────────────────────────────────────────────────
# LLM-emitted shape (TjcAuditResponse tool_use schema).
# ────────────────────────────────────────────────────────────────────────


class TjcFindingNarration(BaseModel):
    """One finding's surveyor-RFI narrative as emitted by Claude.

    Negative findings (the LLM is told ahead of time which are negative)
    must carry an empty citations list and use absence-language
    ("Documentation review did not identify ..."). Positive findings
    carry one or more citations into ClinicalDocument.raw_text.
    """

    ep_code: str = Field(
        description="The TJC Element of Performance code, matching one in EP_CATALOG.",
    )
    narrative: str = Field(
        min_length=1,
        description="Surveyor-RFI sentence(s) describing the finding.",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="Evidence citations for positive findings. Empty for absence findings.",
    )


class TjcAuditResponse(BaseModel):
    """The full LLM tool_use payload.

    Schema passed to ``call_with_schema``. Claude is forced to emit one
    ``TjcFindingNarration`` per EP it was prompted to narrate -- the
    M9 narration code expects the count to match the input EpFinding
    list. A mismatched count fails the response in the structured-output
    parser before reaching the response builder.
    """

    findings: list[TjcFindingNarration] = Field(
        default_factory=list,
        description="One narration per EP, in catalog order.",
    )


# ────────────────────────────────────────────────────────────────────────
# Final API response shape (phase_3_PRD.md §6.2).
# ────────────────────────────────────────────────────────────────────────


EpStatusLiteral = Literal["satisfied", "gap", "not_applicable", "ambiguous"]
SeverityLiteral = Literal["high", "moderate", "low", "none"]


class TjcFindingRead(BaseModel):
    """One finding row in the API response (phase_3_PRD.md §6.2)."""

    ep_code: str
    ep_domain: str
    status: EpStatusLiteral
    severity: SeverityLiteral
    negative_finding: bool
    narrative: str
    citations: list[Citation] = Field(default_factory=list)
    linked_planted_gap: str | None = Field(
        default=None,
        description="G1-G5 when this finding maps to one of the planted gaps in the seeded chart.",
    )


class TjcSummary(BaseModel):
    """Rollup counts that ship under ``summary`` in the API response."""

    total_eps_audited: int
    satisfied: int
    gap: int
    not_applicable: int
    ambiguous: int = 0
    overall_status: Literal["compliant", "non-compliant"]


class TjcAuditRead(BaseModel):
    """The complete API response for ``POST /api/v1/patients/{id}/tjc-audit``."""

    id: UUID
    patient_id: UUID
    computed_at: datetime
    evidence_hash: str
    model_version: str
    cached: bool

    summary: TjcSummary
    findings: list[TjcFindingRead]

    rationale_status: Literal["ok", "unavailable", "degraded"] = "ok"
    rationale_warnings: list[str] = Field(default_factory=list)

    links: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "TjcFindingNarration",
    "TjcAuditResponse",
    "TjcFindingRead",
    "TjcSummary",
    "TjcAuditRead",
    "EpStatusLiteral",
    "SeverityLiteral",
]
