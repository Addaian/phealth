"""
Build Anthropic Citations-API document blocks for the narration prompt.

The Citations API contract (playbook §A4 / phase_3_PRD.md §5.6): each
piece of evidence we want Claude to be able to cite ships as a
``document`` content block on the user message. We attach a JSON
``context`` field that round-trips our internal coordinates -- the
``CitationValidator`` (M7.2) parses this back to verify each cited
span against the local DB.

Shape:
    {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": snippet},
        "title": "doc:<uuid>#chars:<start>-<end>",
        "context": json.dumps({"doc_id": "<uuid>", "char_start": int, "char_end": int}),
        "citations": {"enabled": True},
    }

The function is generic: it accepts any citation-shaped iterable
(AsamEvidence rows for the ASAM endpoint, EpFinding.evidence_pointers
for the TJC endpoint, plain Citation dataclasses, etc.) via a
``Protocol`` so the two reasoning paths can share one helper.
"""

from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID


class CitationLike(Protocol):
    """Anything with the four char-offset fields can be packaged as a
    Citations-API document. ``AsamEvidence``, ``EpFinding.evidence_pointers``
    Citation dataclasses, and ``Phase 2's ProvenanceRead`` all satisfy this.
    """

    document_id: UUID
    char_start: int
    char_end: int
    snippet: str


def build_evidence_document(citation: CitationLike) -> dict:
    """Build one Citations-API document block from a single citation.

    Exposed separately so callers can intersperse documents with other
    content blocks (the standard pattern is documents-first, question-last
    per phase_3_PRD.md §5.6).
    """
    context = json.dumps(
        {
            "doc_id": str(citation.document_id),
            "char_start": citation.char_start,
            "char_end": citation.char_end,
        }
    )
    return {
        "type": "document",
        "source": {
            "type": "text",
            "media_type": "text/plain",
            "data": citation.snippet,
        },
        # The title is human-readable for logs/debugging; the machine-
        # readable mapping rides on ``context`` (where Claude can't
        # accidentally hallucinate over it).
        "title": f"doc:{citation.document_id}#chars:{citation.char_start}-{citation.char_end}",
        "context": context,
        "citations": {"enabled": True},
    }


def build_evidence_documents(citations: list[CitationLike]) -> list[dict]:
    """Build the full list of Citations-API document blocks.

    Order is preserved -- callers concerned about which document Claude
    picks first should pre-sort (e.g., highest-confidence evidence first).
    """
    return [build_evidence_document(c) for c in citations]


def parse_document_context(document_block: dict) -> dict:
    """Inverse of ``build_evidence_document``: parse the ``context``
    JSON back into a typed dict for the CitationValidator.

    Returns ``{doc_id: str, char_start: int, char_end: int}``.
    Raises ``ValueError`` if the context is malformed -- the validator
    treats that as a hard failure.
    """
    context_raw = document_block.get("context")
    if not isinstance(context_raw, str):
        raise ValueError(f"document block context is missing or not a string: {context_raw!r}")
    try:
        parsed = json.loads(context_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"document block context is not valid JSON: {context_raw!r}") from exc
    if not all(k in parsed for k in ("doc_id", "char_start", "char_end")):
        raise ValueError(f"document block context missing required keys: {parsed!r}")
    return parsed


__all__ = [
    "CitationLike",
    "build_evidence_document",
    "build_evidence_documents",
    "parse_document_context",
]
