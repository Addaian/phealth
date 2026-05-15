"""
Shared XML-tagged prompt fragments for the narration prompts.

Anthropic's prompt-engineering docs (referenced in playbook §C3):
  1. XML tags help Claude parse structured prompts.
  2. Documents first, question last.
  3. Pre-fill the assistant turn to constrain format.
  4. Explicit refusal clause for un-evidenced claims.

These fragments are used by both the ASAM narration (M8) and the TJC
narration (M9). Keeping them here avoids two copies of slightly-different
boilerplate and gives one place to tune the refusal language.

ASAM IP boundary (phase_3_PRD.md §3, playbook §F)
-------------------------------------------------
None of these fragments quote the ASAM Score Sheet or the CAMBHC. The
language is project-internal paraphrase. The fragments may be combined
with engine output (rule strings, ratings) in the final prompt -- those
engine outputs are *our* derived clinical decisions and are explicitly
allowed by the IP-boundary rule.
"""

from __future__ import annotations

# ────────────────────────────────────────────────────────────────────────
# System-role boilerplate.
# ────────────────────────────────────────────────────────────────────────


ASAM_SYSTEM_ROLE = """\
You are a clinical reviewer narrating an ASAM Criteria 4th Edition
Level-of-Care recommendation that has ALREADY been determined by a
deterministic rule engine applying Chapter 10 Dimensional Admission
Criteria. Your sole job is to write concise, professionally-toned
rationale text that:

  1. Restates the engine's per-subdimension risk ratings.
  2. Cites ONLY evidence snippets provided in <evidence> blocks.
  3. Connects ratings to the recommended level via the rules in
     <rules_fired>.

You are NOT making the recommendation -- the rule engine did. You are
documenting its reasoning."""


# ────────────────────────────────────────────────────────────────────────
# Refusal / anti-hallucination clause.
# ────────────────────────────────────────────────────────────────────────


REFUSAL_CLAUSE = """\
DO NOT introduce new clinical findings. DO NOT speculate about
diagnoses absent from the evidence. If a dimension or subdimension
lacks evidence, write exactly:
  "Documentation does not establish findings for this subdimension."

Every citation you include MUST be a verbatim text fragment from the
evidence blocks; do not paraphrase a span. Use the document_id, char
offsets, and snippet exactly as they appear under each <evidence>
block's metadata."""


# ────────────────────────────────────────────────────────────────────────
# Output-format clause used with tool-forced calls.
# ────────────────────────────────────────────────────────────────────────


TOOL_OUTPUT_CLAUSE = """\
Respond by calling the submit_structured_response tool with the
narrative fields filled in. Include citations on each subdimension
rationale that draws on evidence (omit citations for subdimensions
with no evidence). Do not emit any text outside the tool call."""


# ────────────────────────────────────────────────────────────────────────
# Helpers to wrap text in XML tags.
# ────────────────────────────────────────────────────────────────────────


def xml(tag: str, body: str) -> str:
    """Wrap ``body`` in ``<tag>...</tag>`` -- the project's standard
    prompt-section delimiter. Keeps the prompt-building code readable
    by collapsing the f-string boilerplate.
    """
    return f"<{tag}>\n{body}\n</{tag}>"


__all__ = [
    "ASAM_SYSTEM_ROLE",
    "REFUSAL_CLAUSE",
    "TOOL_OUTPUT_CLAUSE",
    "xml",
]
