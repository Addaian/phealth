"""
Shared narration-outcome record used by both the ASAM and TJC narration paths.

The dataclass is kept here (rather than in either narration module) so
the two paths can share the retry / degrade vocabulary without one
narration module importing from the other. ``rationale`` is the union
of the ASAM and TJC tool-use payload types; consumers narrow it at
the call site (the response builders introspect by attribute access).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.clinical.asam.schemas import AsamRationale
from app.clinical.llm.citation_validator import ValidationResult
from app.clinical.tjc.schemas import TjcAuditResponse

# ok       -- citations validated; ship the LLM output as-is.
# degraded -- citations failed validation (twice); LLM text shipped
#             without citations, response carries ``rationale_status``
#             + warning marker.
# unavailable -- Claude unreachable (rate-limited / 5xx / breaker open);
#             engine output preserved, narrative is a stub.
RationaleStatus = Literal["ok", "unavailable", "degraded"]


@dataclass
class NarrationOutcome:
    """Aggregate result of one ``narrate()`` / ``narrate_tjc()`` call.

    The API endpoint reads ``status`` + ``warnings`` to stamp the
    response's ``rationale_status`` / ``rationale_warnings`` fields.

    ``rationale`` holds the LLM's structured-output payload:
      * ``AsamRationale`` on the ``/asam-loc`` path,
      * ``TjcAuditResponse`` on the ``/tjc-audit`` path.

    The response builders branch on which path produced it.
    """

    rationale: AsamRationale | TjcAuditResponse
    validation: ValidationResult
    status: RationaleStatus
    warnings: list[str]


__all__ = ["NarrationOutcome", "RationaleStatus"]
