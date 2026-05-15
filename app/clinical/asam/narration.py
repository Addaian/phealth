"""
ASAM narration: turn rule-engine output into a cited rationale via Claude.

This is the M8 milestone -- the first place the LLM is actually called
in the production flow. The narrate() function is the public entry
point; everything else is helper.

Flow:
  1. Build evidence-document blocks from AsamEvidence + the citations
     already attached to each ExtractedObservation.
  2. Build the prompt per phase_3_PRD.md §6.6 (XML-tagged sections,
     documents first, question last, refusal clause).
  3. Call ``call_with_schema(AsamRationale)`` -- tool-use forces a
     structured JSON payload.
  4. Extract every citation from the parsed rationale and validate
     against the local DB via ``CitationValidator.validate_claims``.
  5. On validation failure: retry **once** with a corrective prompt
     listing the broken citations. On second failure: degrade -- return
     a placeholder ``AsamRationale`` with ``rationale_status="degraded"``
     and a warning. (phase_3_PRD.md §5.9.)

Outcome record (``NarrationOutcome``) carries the parsed AsamRationale,
the ValidationResult, and the engine-level status so the response
builder can stamp ``rationale_status`` / ``rationale_warnings``
correctly on the final API response.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

from sqlmodel import Session, select

from app.clinical.asam.level_decision import LevelDecision
from app.clinical.asam.rubric import (
    MIN_LOC_BY_RATING,
    MIN_LOC_DISPLAY,
    RiskRating,
    Subdimension,
)
from app.clinical.asam.schemas import (
    AsamRationale,
    Citation,
    DimensionRationale,
    SubdimensionRationale,
)
from app.clinical.llm.citation_validator import CitationValidator, ValidationResult
from app.clinical.llm.claude_client import ClaudeClient
from app.clinical.llm.prompts import (
    ASAM_SYSTEM_ROLE,
    REFUSAL_CLAUSE,
    TOOL_OUTPUT_CLAUSE,
    xml,
)
from app.clinical.llm.structured_output import (
    StructuredOutputError,
    call_with_schema,
)
from app.clinical.shared.evidence_retrieval import build_evidence_document
from app.clinical.shared.narration_outcome import NarrationOutcome, RationaleStatus
from app.db.models import AsamEvidence, ClinicalDocument

# NarrationOutcome + RationaleStatus live in
# ``app.clinical.shared.narration_outcome`` so both the ASAM and TJC
# narration paths can share the dataclass without one importing the
# other (Phase 3 review cleanup -- previously the slot was typed as
# AsamRationale and TJC used cast(object, …) to fit a
# TjcAuditResponse into it). The Union typing is now explicit.

# ────────────────────────────────────────────────────────────────────────
# Evidence assembly: AsamEvidence rows -> Citations-API document blocks.
# ────────────────────────────────────────────────────────────────────────


@dataclass
class _EvidenceCitation:
    """Adapter that exposes the ``CitationLike`` protocol surface for
    an AsamEvidence row pulled from the DB. NOT frozen because the
    CitationLike Protocol expects settable attributes.
    """

    document_id: UUID
    char_start: int
    char_end: int
    snippet: str


def _load_evidence(
    patient_id: UUID, db: Session
) -> tuple[list[_EvidenceCitation], dict[UUID, ClinicalDocument]]:
    """Pull every AsamEvidence row for the patient, joined with its
    parent ClinicalDocument so we can build Citations-API blocks.

    Returns a list of evidence-citation adapters plus a lookup dict
    from document_id to ClinicalDocument (the narrate() function uses
    the dict to inject document_id into the prompt as a UUID string
    Claude can echo back in its citations).
    """
    # Two-step load: pull the patient's documents first, then their
    # AsamEvidence rows. Two SELECTs instead of one join, but easier to
    # type-check against SQLModel (the join() overloads collide with
    # mypy's view of the operator-overloaded `==` on Column attributes).
    docs = list(db.exec(select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_id)))
    docs_by_id: dict[UUID, ClinicalDocument] = {doc.id: doc for doc in docs}
    if not docs_by_id:
        return [], docs_by_id

    evidence_rows = list(
        db.exec(
            select(AsamEvidence).where(
                AsamEvidence.document_id.in_(docs_by_id.keys())  # type: ignore[attr-defined]
            )
        )
    )
    citations: list[_EvidenceCitation] = [
        _EvidenceCitation(
            document_id=ev.document_id,
            char_start=ev.char_start,
            char_end=ev.char_end,
            snippet=ev.snippet,
        )
        for ev in evidence_rows
    ]
    return citations, docs_by_id


# ────────────────────────────────────────────────────────────────────────
# Prompt assembly.
# ────────────────────────────────────────────────────────────────────────


def _build_dimensional_findings(
    ratings: dict[Subdimension, RiskRating],
) -> str:
    """Render the per-subdimension ratings table for the prompt.

    Each subdimension is one line: name, rating token, derived Min LoC.
    Stable order across calls so the prompt cache hits more often.
    """
    lines = []
    for subdim in Subdimension:
        rating = ratings.get(subdim)
        if rating is None:
            continue
        min_loc = MIN_LOC_BY_RATING[rating].value
        lines.append(f"  {subdim.value}: rating={rating.value} min_level={min_loc}")
    return "\n".join(lines) or "  (no subdimensions rated)"


def _build_recommendation_line(decision: LevelDecision) -> str:
    """The single-line summary of the engine's decision.

    The MIN_LOC_DISPLAY lookup strips any ``-COE`` suffix off the level
    token first -- COE is a modifier, not a separate level, so the
    display table is keyed by the base level (3.5, 2.5, 1.7, …). Same
    pattern as ``response_builder._resolve_min_loc_for_level``.
    """
    base_level = decision.level.removesuffix("-COE")
    level_display = next(
        (display for loc, display in MIN_LOC_DISPLAY.items() if loc.value == base_level),
        decision.level,
    )
    coe = " COE" if decision.co_occurring_enhanced else ""
    bio = " BIO" if decision.biomedical_enhanced else ""
    suffix = (coe + bio).strip() or "non-COE non-BIO"
    return f"Level {decision.level} ({level_display}); {suffix}"


def _build_user_message_text(
    *,
    decision: LevelDecision,
    ratings: dict[Subdimension, RiskRating],
) -> str:
    """Render the non-evidence text portion of the user message.

    The Citations-API document blocks are appended separately as
    ``content`` blocks; this function builds the framing prose.
    """
    return "\n\n".join(
        [
            xml("dimensional_findings", _build_dimensional_findings(ratings)),
            xml("rules_fired", "\n".join(f"  - {r}" for r in decision.rules_fired)),
            xml("recommendation", _build_recommendation_line(decision)),
            xml(
                "task",
                "Produce JSON matching the AsamRationale schema:\n"
                "  - overall_rationale (≤120 words)\n"
                "  - dimensions[*].rationale (≤40 words each)\n"
                "  - dimensions[*].subdimensions[*].rationale + citations\n"
                "  - confidence (high|moderate|low)",
            ),
            REFUSAL_CLAUSE,
            TOOL_OUTPUT_CLAUSE,
        ]
    )


def _build_messages(
    *,
    decision: LevelDecision,
    ratings: dict[Subdimension, RiskRating],
    evidence_documents: list[dict],
) -> list[dict]:
    """Build the messages list for the Claude call.

    Evidence documents go first (documents-first, question-last per
    Anthropic's prompt-engineering guide), then the framing text.
    Each evidence block carries its own ``context`` JSON so the LLM
    has the document_id + offsets visible inline.
    """
    user_text = _build_user_message_text(decision=decision, ratings=ratings)
    content: list[dict] = list(evidence_documents)  # documents first
    content.append({"type": "text", "text": user_text})
    return [{"role": "user", "content": content}]


# ────────────────────────────────────────────────────────────────────────
# Validator integration: extract every citation from the parsed rationale.
# ────────────────────────────────────────────────────────────────────────


def _all_citations(rationale: AsamRationale) -> list[Citation]:
    """Walk the AsamRationale and collect every Citation object the LLM
    emitted, in document order. The CitationValidator processes them
    flat -- the per-subdimension grouping is preserved separately in
    the response builder.
    """
    citations: list[Citation] = []
    for dim in rationale.dimensions:
        for subdim in dim.subdimensions:
            citations.extend(subdim.citations)
    return citations


# ────────────────────────────────────────────────────────────────────────
# Public entry point.
# ────────────────────────────────────────────────────────────────────────


def narrate(
    *,
    client: ClaudeClient,
    db: Session,
    patient_id: UUID,
    decision: LevelDecision,
    ratings: dict[Subdimension, RiskRating],
) -> NarrationOutcome:
    """Produce the cited AsamRationale around the engine's decision.

    On Claude failure (rate-limited, breaker open, malformed structured
    output) the function raises -- the API endpoint catches and maps to
    RFC 7807. On *citation-validation* failure the function attempts
    one corrective retry, then degrades to a placeholder rationale
    rather than failing the whole assessment (phase_3_PRD.md §5.9).
    """
    evidence_citations, _docs_by_id = _load_evidence(patient_id, db)
    evidence_documents = [build_evidence_document(c) for c in evidence_citations]
    validator = CitationValidator(db)

    # First attempt.
    messages = _build_messages(
        decision=decision, ratings=ratings, evidence_documents=evidence_documents
    )
    rationale, _response = call_with_schema(
        client,
        AsamRationale,
        endpoint="asam-loc",
        patient_id=patient_id,
        messages=messages,  # type: ignore[arg-type]
        system=ASAM_SYSTEM_ROLE,
        max_tokens=4096,
        temperature=0.0,
    )

    citations = _all_citations(rationale)
    validation = validator.validate_claims(citations, patient_id)
    if validation.passed:
        return NarrationOutcome(
            rationale=rationale,
            validation=validation,
            status="ok",
            warnings=[],
        )

    # Second attempt with a corrective prompt listing the broken citations.
    correction_text = (
        "Your previous response contained citations that failed validation:\n"
        + "\n".join(f"  - {f.failure_type}: {f.detail}" for f in validation.failures)
        + "\n\nProduce the response again. Every citation MUST round-trip exactly "
        "against the evidence text -- do not paraphrase spans."
    )
    retry_messages = messages + [
        {"role": "assistant", "content": json.dumps(rationale.model_dump(mode="json"))},
        {"role": "user", "content": correction_text},
    ]
    try:
        retry_rationale, _ = call_with_schema(
            client,
            AsamRationale,
            endpoint="asam-loc",
            patient_id=patient_id,
            messages=retry_messages,  # type: ignore[arg-type]
            system=ASAM_SYSTEM_ROLE,
            max_tokens=4096,
            temperature=0.0,
            citation_validation_passed=False,  # mark the retry in LlmInvocation
        )
    except StructuredOutputError:
        # Even the retry produced a malformed payload -- degrade.
        return _degraded_outcome(
            rationale=rationale,
            validation=validation,
            warning="retry_malformed_payload",
        )

    retry_citations = _all_citations(retry_rationale)
    retry_validation = validator.validate_claims(retry_citations, patient_id)
    if retry_validation.passed:
        return NarrationOutcome(
            rationale=retry_rationale,
            validation=retry_validation,
            status="ok",
            warnings=["citation_validation_retry"],
        )

    # Both attempts produced bad citations -- degrade.
    return _degraded_outcome(
        rationale=retry_rationale,
        validation=retry_validation,
        warning="citation_validation_failed",
    )


def _degraded_outcome(
    *,
    rationale: AsamRationale,
    validation: ValidationResult,
    warning: str,
) -> NarrationOutcome:
    """Build the degraded NarrationOutcome.

    Keeps the LLM's textual narrative (it's still useful context for the
    reviewer) but **strips every citation** -- the citations are what
    failed validation, so shipping them would leak unverified claims to
    the consumer.
    """
    degraded_rationale = _strip_citations(rationale)
    return NarrationOutcome(
        rationale=degraded_rationale,
        validation=validation,
        status="degraded",
        warnings=[warning],
    )


def _strip_citations(rationale: AsamRationale) -> AsamRationale:
    """Return a new AsamRationale with every citation list emptied out."""
    return AsamRationale(
        overall_rationale=rationale.overall_rationale,
        dimensions=[
            DimensionRationale(
                dimension=dim.dimension,
                rationale=dim.rationale,
                subdimensions=[
                    SubdimensionRationale(
                        name=sub.name,
                        rationale=sub.rationale,
                        citations=[],
                    )
                    for sub in dim.subdimensions
                ],
            )
            for dim in rationale.dimensions
        ],
        confidence=rationale.confidence,
    )


__all__ = [
    "narrate",
    "NarrationOutcome",
    "RationaleStatus",
]
