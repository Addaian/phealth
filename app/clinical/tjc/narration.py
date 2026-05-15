"""
TJC compliance-audit narration: surveyor-RFI prose around the 13-EP findings.

Mirrors the ASAM narration (M8) but **batches all 13 EPs into one
Claude call** (phase_3_PRD.md §5.5 + playbook §B3). The single round
trip is ~$0.077 versus 13 individual calls; the batched prompt is
also where the surveyor-RFI register lives, applied uniformly across
all findings.

Flow:
  1. Take the engine's list of EpFinding objects (one per EP).
  2. Build the evidence-documents list from each finding's
     ``evidence_pointers`` (positive findings only; negative findings
     have empty pointer lists by contract).
  3. Build the prompt: the ep_catalog requirements, the per-EP
     finding_template + negative_finding flag, the surveyor-RFI
     register instructions, the refusal clause.
  4. Call ``call_with_schema(TjcAuditResponse)``. The LLM emits one
     ``TjcFindingNarration`` per EP, with citations on positives only.
  5. Validate every citation against the local DB
     (``CitationValidator.validate_claims``).
  6. On validation failure: retry once with a corrective prompt; on
     second failure, degrade (strip citations, mark
     rationale_status="degraded" + warning).

NOT included here (per the layering split): the join from LLM
narrative back to the engine's EpFinding metadata (status, severity,
planted_gap linkage). That join lives in the response_builder.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlmodel import Session

from app.clinical.asam.schemas import Citation
from app.clinical.llm.citation_validator import CitationValidator, ValidationResult
from app.clinical.llm.claude_client import ClaudeClient
from app.clinical.llm.prompts import REFUSAL_CLAUSE, TOOL_OUTPUT_CLAUSE, xml
from app.clinical.llm.structured_output import StructuredOutputError, call_with_schema
from app.clinical.shared.evidence_retrieval import build_evidence_document
from app.clinical.shared.narration_outcome import NarrationOutcome
from app.clinical.tjc.audit_functions import EpFinding
from app.clinical.tjc.ep_catalog import EP_BY_CODE
from app.clinical.tjc.schemas import (
    TjcAuditResponse,
    TjcFindingNarration,
)

# ────────────────────────────────────────────────────────────────────────
# System role -- TJC-specific (the ASAM system role would mis-cue the LLM).
# ────────────────────────────────────────────────────────────────────────


TJC_SYSTEM_ROLE = """\
You write Joint Commission compliance-audit findings for a Behavioral
Health Care organization. Your register is precise, neutral, and
citation-driven -- modeled on the Requirements-For-Improvement (RFI)
phrasing surveyors use in their final reports.

For each finding follow this template:
  "Standard [code], EP [n]: [met / not met / partially met]. Documentation
  review revealed [specific finding]. [time / context]."

Negative findings (absence of required documentation) MUST use the
phrase "Documentation review did not identify [requirement] during
the [time window]" -- never invent a citation span for "nothing was
found".

You are NOT making the compliance determination -- the rule engine did.
You are rendering its findings in surveyor language. Every positive
finding must cite the supporting evidence verbatim; every negative
finding must carry an empty citations list."""


# ────────────────────────────────────────────────────────────────────────
# Prompt assembly.
# ────────────────────────────────────────────────────────────────────────


def _format_finding_for_prompt(finding: EpFinding) -> str:
    """Render one EpFinding for the LLM in a compact, parseable form."""
    ep = EP_BY_CODE.get(finding.ep_code)
    requirement = ep.paraphrased_requirement if ep else "(unknown EP)"
    citation_marker = "absence-of-evidence (NO citation)" if finding.negative_finding else "citable"
    planted = f" [planted_gap={finding.linked_planted_gap}]" if finding.linked_planted_gap else ""
    return (
        f"  - ep_code: {finding.ep_code}\n"
        f"    domain: {finding.ep_domain}\n"
        f"    status: {finding.status}\n"
        f"    severity: {finding.severity}\n"
        f"    citation_mode: {citation_marker}{planted}\n"
        f"    requirement (paraphrased): {requirement}\n"
        f"    engine_template: {finding.finding_template}"
    )


def _build_user_message_text(findings: list[EpFinding]) -> str:
    """Build the framing text portion of the user message.

    The Citations-API document blocks are appended as content blocks
    separately; this function builds the prose around them.
    """
    findings_block = "\n\n".join(_format_finding_for_prompt(f) for f in findings)
    task = (
        "Produce JSON matching the TjcAuditResponse schema. Emit one "
        "TjcFindingNarration per EP listed below, in the same order. "
        "For each finding, fill in:\n"
        "  - ep_code (matching the engine's value)\n"
        "  - narrative (surveyor-RFI sentence(s))\n"
        "  - citations (verbatim spans only; empty list for absence findings)\n"
        "\nDo NOT change the EP's status or severity -- those are the "
        "engine's determination. Your job is the narrative wording only."
    )
    return "\n\n".join(
        [
            xml("findings_to_narrate", findings_block),
            xml("task", task),
            REFUSAL_CLAUSE,
            TOOL_OUTPUT_CLAUSE,
        ]
    )


def _build_evidence_documents(findings: list[EpFinding]) -> list[dict]:
    """Build Citations-API document blocks from every EpFinding's
    evidence_pointers.

    Negative findings contribute nothing (their pointers list is empty
    by contract). Positive findings contribute one document per
    pointer, tagged with the EP code in the title so Claude can match
    citations to their narrative.
    """
    blocks: list[dict] = []
    for finding in findings:
        for pointer in finding.evidence_pointers:
            block = build_evidence_document(pointer)
            # Override the title with EP context so the prompt is easier
            # for Claude to navigate. Title is human-readable only --
            # the machine-readable mapping (doc_id, char_start, char_end)
            # still lives in ``context`` JSON for the validator.
            block["title"] = f"ep:{finding.ep_code}#doc:{pointer.document_id}"
            blocks.append(block)
    return blocks


def _build_messages(findings: list[EpFinding]) -> list[dict]:
    """Build the messages list for the Claude call.

    Documents first (per Anthropic prompt-engineering guidance), then
    the framing text last.
    """
    content: list[dict] = list(_build_evidence_documents(findings))
    content.append({"type": "text", "text": _build_user_message_text(findings)})
    return [{"role": "user", "content": content}]


# ────────────────────────────────────────────────────────────────────────
# Citation extraction + retry helpers.
# ────────────────────────────────────────────────────────────────────────


def _all_citations(audit: TjcAuditResponse) -> list[Citation]:
    """Collect every citation across all findings, in finding order.

    The validator processes them flat; the per-finding grouping is
    recovered in the response_builder by ep_code.
    """
    citations: list[Citation] = []
    for finding in audit.findings:
        citations.extend(finding.citations)
    return citations


def _strip_citations(audit: TjcAuditResponse) -> TjcAuditResponse:
    """Return a TjcAuditResponse with every citation list emptied out.

    Used on the degraded path: the LLM's text is still useful, but the
    citations failed validation, so shipping them would mean leaking
    unverified claims.
    """
    return TjcAuditResponse(
        findings=[
            TjcFindingNarration(
                ep_code=f.ep_code,
                narrative=f.narrative,
                citations=[],
            )
            for f in audit.findings
        ]
    )


# ────────────────────────────────────────────────────────────────────────
# Public entry point.
# ────────────────────────────────────────────────────────────────────────


def narrate_tjc(
    *,
    client: ClaudeClient,
    db: Session,
    patient_id: UUID,
    findings: list[EpFinding],
) -> NarrationOutcome:
    """Produce the cited TJC audit narrative around the engine's findings.

    Returns a ``NarrationOutcome`` whose ``rationale`` field is actually
    a ``TjcAuditResponse`` (we reuse the M8 outcome dataclass because
    the retry/degrade machinery is identical). The response_builder
    introspects the type to pick the right join path.

    Failure semantics match M8 exactly:
      * Claude unreachable / breaker open / structured-output malformed
        -> raises (mapped to RFC 7807 by the API layer).
      * Citation validation fails twice -> degraded outcome.
    """
    validator = CitationValidator(db)
    messages = _build_messages(findings)

    audit, _response = call_with_schema(
        client,
        TjcAuditResponse,
        endpoint="tjc-audit",
        patient_id=patient_id,
        messages=messages,  # type: ignore[arg-type]
        system=TJC_SYSTEM_ROLE,
        max_tokens=4096,
        temperature=0.0,
    )

    citations = _all_citations(audit)
    validation = validator.validate_claims(citations, patient_id)
    if validation.passed:
        return _ok_outcome(audit, validation)

    # Retry once with a corrective prompt listing the broken citations.
    correction = (
        "Your previous response contained citations that failed "
        "validation:\n"
        + "\n".join(f"  - {f.failure_type}: {f.detail}" for f in validation.failures)
        + "\n\nProduce the response again. Every citation MUST round-trip "
        "exactly against the evidence text -- do not paraphrase spans. "
        "Negative findings MUST carry an empty citations list."
    )
    retry_messages = messages + [
        {"role": "assistant", "content": json.dumps(audit.model_dump(mode="json"))},
        {"role": "user", "content": correction},
    ]
    try:
        retry_audit, _ = call_with_schema(
            client,
            TjcAuditResponse,
            endpoint="tjc-audit",
            patient_id=patient_id,
            messages=retry_messages,  # type: ignore[arg-type]
            system=TJC_SYSTEM_ROLE,
            max_tokens=4096,
            temperature=0.0,
            citation_validation_passed=False,
        )
    except StructuredOutputError:
        return _degraded_outcome(
            audit=audit,
            validation=validation,
            warning="retry_malformed_payload",
        )

    retry_citations = _all_citations(retry_audit)
    retry_validation = validator.validate_claims(retry_citations, patient_id)
    if retry_validation.passed:
        outcome = _ok_outcome(retry_audit, retry_validation)
        outcome.warnings.append("citation_validation_retry")
        return outcome

    return _degraded_outcome(
        audit=retry_audit,
        validation=retry_validation,
        warning="citation_validation_failed",
    )


# ────────────────────────────────────────────────────────────────────────
# Internal: NarrationOutcome construction.
#
# NarrationOutcome lives in ``app.clinical.shared.narration_outcome``
# with ``rationale: AsamRationale | TjcAuditResponse``. The response
# builders branch on the runtime type to pick the right join path.
# ────────────────────────────────────────────────────────────────────────


def _ok_outcome(audit: TjcAuditResponse, validation: ValidationResult) -> NarrationOutcome:
    return NarrationOutcome(
        rationale=audit,
        validation=validation,
        status="ok",
        warnings=[],
    )


def _degraded_outcome(
    *,
    audit: TjcAuditResponse,
    validation: ValidationResult,
    warning: str,
) -> NarrationOutcome:
    stripped = _strip_citations(audit)
    return NarrationOutcome(
        rationale=stripped,
        validation=validation,
        status="degraded",
        warnings=[warning],
    )


__all__ = ["narrate_tjc", "TJC_SYSTEM_ROLE"]
