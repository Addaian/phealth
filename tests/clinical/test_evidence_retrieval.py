"""
M7 unit tests for the Citations-API document-block builder.

The builder is small, but the JSON-round-trip of the ``context`` field
is load-bearing for the CitationValidator (M7.2): if the keys or shape
drift, the validator silently fails open. Tests pin both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from app.clinical.shared.evidence_retrieval import (
    build_evidence_document,
    build_evidence_documents,
    parse_document_context,
)


@dataclass
class _Citation:
    """Minimal CitationLike for tests."""

    document_id: UUID
    char_start: int
    char_end: int
    snippet: str


def test_build_evidence_document_shape_matches_citations_api_contract():
    """The block must have the four keys Claude's Citations API expects."""
    cite = _Citation(uuid4(), 100, 200, "PHQ-9 administered at intake...")
    block = build_evidence_document(cite)
    assert block["type"] == "document"
    assert block["source"] == {
        "type": "text",
        "media_type": "text/plain",
        "data": "PHQ-9 administered at intake...",
    }
    assert block["citations"] == {"enabled": True}
    # Title is human-readable; the machine-readable mapping is in context.
    assert "title" in block
    assert isinstance(block["context"], str)


def test_context_round_trip_preserves_doc_id_and_offsets():
    """context JSON round-trips through parse_document_context."""
    doc_id = uuid4()
    cite = _Citation(doc_id, 12397, 12587, "snippet text")
    block = build_evidence_document(cite)
    parsed = parse_document_context(block)
    assert parsed["doc_id"] == str(doc_id)
    assert parsed["char_start"] == 12397
    assert parsed["char_end"] == 12587


def test_build_evidence_documents_preserves_order():
    """Output order matches input order -- callers pre-sort if needed."""
    cites = [_Citation(uuid4(), i * 100, i * 100 + 50, f"snippet {i}") for i in range(5)]
    blocks = build_evidence_documents(cites)
    assert len(blocks) == 5
    for i, block in enumerate(blocks):
        assert block["source"]["data"] == f"snippet {i}"


def test_parse_document_context_rejects_missing_keys():
    """Defensive: a block without doc_id/char_start/char_end raises."""
    bad_block = {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": "x"},
        "context": json.dumps({"doc_id": "abc"}),  # missing char_start, char_end
    }
    with pytest.raises(ValueError):
        parse_document_context(bad_block)


def test_parse_document_context_rejects_non_json_context():
    """Defensive: a context that isn't valid JSON raises."""
    bad_block = {"context": "not-json"}
    with pytest.raises(ValueError):
        parse_document_context(bad_block)


def test_parse_document_context_rejects_missing_context():
    """Defensive: a block with no context at all raises."""
    with pytest.raises(ValueError):
        parse_document_context({})
