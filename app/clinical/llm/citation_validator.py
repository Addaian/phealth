"""
Post-hoc validation of Claude's Citations-API output against the local DB.

The Anthropic Citations API guarantees the *boundary contract*: every
citation Claude emits points to a span of a document we provided. That
is not sufficient for our purposes -- the canonical truth lives in the
``ClinicalDocument.raw_text`` rows in Postgres, and Claude could have
been given a stale or buggy snippet. We re-verify every citation against
the DB before persisting the assessment (phase_3_PRD.md §5.6).

What the validator catches:
  * **unknown_doc**       -- ``context.doc_id`` is not a ClinicalDocument
                             we have, or doesn't belong to this patient.
  * **out_of_range**      -- the citation's char offsets fall outside
                             the document's raw_text.
  * **snippet_mismatch**  -- ``raw_text[char_start:char_end] != cited_text``.
                             This is the high-value signal: Claude
                             paraphrased instead of citing verbatim.

A clean validation lets the narration go to the user as-is. A failure
triggers M8's "retry once with corrective prompt; on second failure,
fall back to rule-engine-only output" path (phase_3_PRD.md §5.9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.clinical.shared.evidence_retrieval import parse_document_context
from app.db.models import ClinicalDocument

FailureType = str  # "unknown_doc" | "out_of_range" | "snippet_mismatch" | "malformed_context"


@dataclass
class CitationFailure:
    """One citation that failed validation."""

    failure_type: FailureType
    cited_text: str
    document_index: int
    detail: str


@dataclass
class ValidationResult:
    """Aggregate outcome -- exposed to M8/M9 narration retries."""

    passed: bool
    citations_checked: int
    failures: list[CitationFailure] = field(default_factory=list)


class CitationValidator:
    """Validates citations against the local DB.

    Two entry points, depending on how citations were emitted:

      * ``validate(message, documents_sent, patient_id)`` -- for
        citations attached to text blocks by Claude's **native Citations
        API**. Walks ``message.content`` and resolves snippet-relative
        offsets back to the original document via the ``context`` JSON
        we embedded.
      * ``validate_claims(claims, patient_id)`` -- for citations the
        LLM emitted **inline in a tool_use payload** (the playbook §D3
        shape; used by M8/M9 narration). Each claim already carries
        the original document_id and document-relative offsets; the
        validator just re-pulls the raw_text and compares.

    Both methods share the same failure-mode vocabulary (unknown_doc /
    out_of_range / snippet_mismatch / malformed_context) and return
    the same ``ValidationResult`` shape, so callers can swap which
    path produced their citations without changing downstream code.
    """

    def __init__(self, db: Session):
        self.db = db

    def validate(
        self,
        message: Any,
        *,
        documents_sent: list[dict],
        patient_id: UUID,
    ) -> ValidationResult:
        """Walk the response's content blocks and validate each citation.

        ``message`` is the ``anthropic.types.Message`` returned by the
        SDK. ``documents_sent`` is the documents-list that was attached
        to the user message in the prompt (each item's ``context`` field
        carries the doc_id + offsets we embedded).
        """
        failures: list[CitationFailure] = []
        citations_checked = 0

        for content_block in message.content:
            block_citations = getattr(content_block, "citations", None) or []
            for citation in block_citations:
                citations_checked += 1
                failure = self._check_one(
                    citation=citation,
                    documents_sent=documents_sent,
                    patient_id=patient_id,
                )
                if failure is not None:
                    failures.append(failure)

        return ValidationResult(
            passed=not failures,
            citations_checked=citations_checked,
            failures=failures,
        )

    # ── internal ─────────────────────────────────────────────────────

    def _check_one(
        self,
        *,
        citation: Any,
        documents_sent: list[dict],
        patient_id: UUID,
    ) -> CitationFailure | None:
        """Return a CitationFailure or None on success."""
        # Citation fields (anthropic.types.CitationCharLocation):
        cited_text: str = getattr(citation, "cited_text", "")
        document_index: int = getattr(citation, "document_index", -1)
        start_in_snippet: int = getattr(citation, "start_char_index", 0)
        end_in_snippet: int = getattr(citation, "end_char_index", 0)

        # 1. Resolve the document we sent at that index.
        if document_index < 0 or document_index >= len(documents_sent):
            return CitationFailure(
                failure_type="unknown_doc",
                cited_text=cited_text,
                document_index=document_index,
                detail=(
                    f"citation references document_index={document_index} "
                    f"but only {len(documents_sent)} documents were sent"
                ),
            )

        # 2. Parse our embedded context.
        try:
            context = parse_document_context(documents_sent[document_index])
        except ValueError as exc:
            return CitationFailure(
                failure_type="malformed_context",
                cited_text=cited_text,
                document_index=document_index,
                detail=str(exc),
            )

        doc_id_raw: str = context["doc_id"]
        snippet_char_start: int = context["char_start"]

        # 3. Verify the document belongs to this patient.
        try:
            doc_uuid = UUID(doc_id_raw)
        except (ValueError, TypeError):
            return CitationFailure(
                failure_type="unknown_doc",
                cited_text=cited_text,
                document_index=document_index,
                detail=f"context doc_id is not a valid UUID: {doc_id_raw!r}",
            )

        doc = self.db.get(ClinicalDocument, doc_uuid)
        if doc is None or doc.patient_id != patient_id:
            return CitationFailure(
                failure_type="unknown_doc",
                cited_text=cited_text,
                document_index=document_index,
                detail=(
                    f"ClinicalDocument {doc_uuid} not found "
                    f"or does not belong to patient {patient_id}"
                ),
            )

        # 4. Translate Claude's snippet-relative offsets back to the
        #    original document's coordinates and verify range.
        original_start = snippet_char_start + start_in_snippet
        original_end = snippet_char_start + end_in_snippet
        if original_end > len(doc.raw_text) or original_start < 0:
            return CitationFailure(
                failure_type="out_of_range",
                cited_text=cited_text,
                document_index=document_index,
                detail=(
                    f"char range [{original_start}:{original_end}] is outside "
                    f"raw_text of length {len(doc.raw_text)}"
                ),
            )

        # 5. The high-value check: does the cited span actually match
        #    what Claude says it does?
        actual = doc.raw_text[original_start:original_end]
        if actual != cited_text:
            return CitationFailure(
                failure_type="snippet_mismatch",
                cited_text=cited_text,
                document_index=document_index,
                detail=(
                    f"raw_text[{original_start}:{original_end}] = {actual!r} "
                    f"does not match cited_text={cited_text!r}"
                ),
            )
        return None

    # ──────────────────────────────────────────────────────────────────
    # Tool-use-emitted citation path (M8/M9 narration).
    # ──────────────────────────────────────────────────────────────────

    def validate_claims(self, claims: list[Any], patient_id: UUID) -> ValidationResult:
        """Validate citations the LLM provided inline in a tool_use payload.

        Each ``claim`` is anything with ``document_id`` (UUID), ``char_start``,
        ``char_end``, and ``snippet`` attributes -- the shape from the
        ``AsamRationale`` / ``TjcAuditResponse`` Pydantic models. The
        validator pulls the corresponding ``ClinicalDocument``, verifies
        patient ownership, range validity, and verbatim text match.

        Returns the same ``ValidationResult`` shape ``validate(message, ...)``
        does, so downstream retry / degradation logic is shared.
        """
        failures: list[CitationFailure] = []
        for index, claim in enumerate(claims):
            failure = self._check_claim(claim=claim, claim_index=index, patient_id=patient_id)
            if failure is not None:
                failures.append(failure)
        return ValidationResult(
            passed=not failures,
            citations_checked=len(claims),
            failures=failures,
        )

    def _check_claim(
        self, *, claim: Any, claim_index: int, patient_id: UUID
    ) -> CitationFailure | None:
        """Validate one tool-use-emitted citation. Mirrors _check_one but
        works against the *original* document coordinates (no
        snippet-relative offset translation needed -- the LLM gave us
        offsets into raw_text directly per the schema in
        ``app.clinical.asam.schemas.Citation``).
        """
        document_id = getattr(claim, "document_id", None)
        char_start = getattr(claim, "char_start", -1)
        char_end = getattr(claim, "char_end", -1)
        snippet = getattr(claim, "snippet", "")

        # 1. Document exists and belongs to this patient.
        if document_id is None:
            return CitationFailure(
                failure_type="unknown_doc",
                cited_text=snippet,
                document_index=claim_index,
                detail="claim missing document_id",
            )
        doc = self.db.get(ClinicalDocument, document_id)
        if doc is None or doc.patient_id != patient_id:
            return CitationFailure(
                failure_type="unknown_doc",
                cited_text=snippet,
                document_index=claim_index,
                detail=(
                    f"ClinicalDocument {document_id} not found "
                    f"or does not belong to patient {patient_id}"
                ),
            )

        # 2. Range valid.
        if char_end > len(doc.raw_text) or char_start < 0 or char_end < char_start:
            return CitationFailure(
                failure_type="out_of_range",
                cited_text=snippet,
                document_index=claim_index,
                detail=(
                    f"char range [{char_start}:{char_end}] is outside "
                    f"raw_text of length {len(doc.raw_text)}"
                ),
            )

        # 3. Verbatim match -- the keystone check.
        actual = doc.raw_text[char_start:char_end]
        if actual != snippet:
            return CitationFailure(
                failure_type="snippet_mismatch",
                cited_text=snippet,
                document_index=claim_index,
                detail=(
                    f"raw_text[{char_start}:{char_end}] = {actual!r} "
                    f"does not match snippet={snippet!r}"
                ),
            )
        return None


__all__ = [
    "CitationValidator",
    "ValidationResult",
    "CitationFailure",
    "FailureType",
]
