"""
Char-offset provenance helpers.

Every ``ExtractedObservation``, ``AsamEvidence`` row, and ``TjcCoverage``
evidence pointer records ``(char_start, char_end)`` offsets into a
``ClinicalDocument.raw_text`` (PRD §5.5). This module is the small shared
vocabulary for that: the :class:`Span` type and the round-trip check.

Design note — why there is no offset *remapping* here
-----------------------------------------------------
The implementation plan anticipated a need to map offsets "across cleaning
steps" (PDF extraction -> page-artifact removal -> normalization). We avoid
that entirely by defining ``raw_text`` as the *post-cleaning canonical text*:
``pdf_parser`` produces the cleaned text once, the orchestrator stores it as
``ClinicalDocument.raw_text``, and every extractor runs its regexes against
that single string. An offset is therefore just an index into ``raw_text`` --
no remapping, no drift. The provenance guarantee reduces to:
``raw_text[span.char_start:span.char_end]`` equals the recorded snippet, which
:func:`verify_roundtrip` checks.
"""

import re
from typing import NamedTuple


class Span(NamedTuple):
    """A half-open ``[char_start, char_end)`` range into a document's raw_text."""

    char_start: int
    char_end: int

    def snippet(self, raw_text: str) -> str:
        """Return the substring of ``raw_text`` this span points at."""
        return raw_text[self.char_start : self.char_end]


def span_of_match(match: re.Match[str]) -> Span:
    """Build a :class:`Span` from a regex match.

    The offsets are valid for whatever string the regex was run against — so
    extractors must run their patterns directly on ``raw_text`` for the span
    to be a true provenance pointer.
    """
    return Span(match.start(), match.end())


def verify_roundtrip(raw_text: str, span: Span, expected_snippet: str) -> bool:
    """Return True if ``raw_text[span]`` equals ``expected_snippet``.

    This is the provenance guarantee from PRD §5.5 — used by
    tests/test_provenance.py and as a defensive assertion inside the extractors.
    """
    return raw_text[span.char_start : span.char_end] == expected_snippet
