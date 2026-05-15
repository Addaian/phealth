"""
Anthropic SDK smoke test — Phase 3 M0 acceptance gate.

Verifies four things end-to-end before any clinical code is written:

  1. The ``anthropic`` SDK is installed and importable.
  2. ``ANTHROPIC_API_KEY`` is present in the environment / ``.env``.
  3. The Claude API is reachable and the configured model
     (``PHEALTH_LLM_MODEL``, default ``claude-sonnet-4-6``) accepts requests.
  4. The Citations API — the single most important Phase 3 primitive
     (phase_3_PRD.md §5.6) — round-trips a tiny document and returns a
     ``citations`` array on the response content blocks.

This is intentionally a manual-run script: it makes a real billed API call,
so it is NOT wired into pytest. CI uses a mocked client (set up in M6).

Run:
    python scripts/smoke_anthropic.py

Exit codes:
    0 -- success; the printed response includes at least one citation block.
    1 -- a failure occurred; the message describes which of the four checks
         above broke.

Expected cost: <$0.001 per run (well under the per-assessment $0.10 ceiling
documented in phase_3_PRD.md §7).
"""

from __future__ import annotations

import sys

import anthropic

from app.core.config import get_settings

# A trivially-citable evidence span. We pick something that obviously answers
# the question, so a passing run cleanly demonstrates the Citations API is
# working — not just that some answer came back.
EVIDENCE_TEXT = (
    "Marcus J. Reyes (DOB 1991-04-11) is a 34-year-old male admitted for "
    "alcohol and benzodiazepine withdrawal management."
)

QUESTION = (
    "What two substances is this patient withdrawing from? "
    "Answer in one short sentence and cite the source."
)


def main() -> int:
    """Run the smoke test and return a process exit code."""

    settings = get_settings()

    if not settings.anthropic_api_key:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set. Copy .env.example to .env "
            "and fill in the key from https://console.anthropic.com/."
        )
        return 1

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    model = settings.phealth_llm_model

    print(f"→ Calling {model} with one Citations-API document block…")

    try:
        # Citations-API document block shape per phase_3.md §A4: the snippet
        # rides on ``source.data``; ``citations.enabled=True`` instructs Claude
        # to attach citation pointers to each response content block.
        response = client.messages.create(
            model=model,
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "text",
                                "media_type": "text/plain",
                                "data": EVIDENCE_TEXT,
                            },
                            "title": "smoke-evidence",
                            "citations": {"enabled": True},
                        },
                        {"type": "text", "text": QUESTION},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        print(f"ERROR: Claude API call failed: {exc.__class__.__name__}: {exc}")
        return 1

    # Pretty-print the response so the reviewer (and future-you) can eyeball
    # the citation shape without poking around with pdb.
    print("\n— Response content blocks —")
    citation_count = 0
    for block in response.content:
        block_type = getattr(block, "type", "?")
        text = getattr(block, "text", "")
        citations = getattr(block, "citations", None) or []
        citation_count += len(citations)
        print(f"  [{block_type}] {text!r}")
        for cite in citations:
            cited_text = getattr(cite, "cited_text", "?")
            doc_title = getattr(cite, "document_title", "?")
            print(f"      ↳ cites {doc_title!r}: {cited_text!r}")

    usage = response.usage
    print("\n— Usage —")
    print(f"  input_tokens:  {usage.input_tokens}")
    print(f"  output_tokens: {usage.output_tokens}")
    # Pricing context (May 2026 Sonnet 4.6): $3 / $15 per MTok in/out.
    # A few-hundred-token round trip should land well under a cent.
    cost = (usage.input_tokens * 3 + usage.output_tokens * 15) / 1_000_000
    print(f"  est. cost:     ${cost:.6f}")

    if citation_count == 0:
        print(
            "\nERROR: response had no citation blocks — Citations API may not "
            "be enabled on this model, or the SDK version is too old."
        )
        return 1

    print(f"\nOK: Citations API verified ({citation_count} citation(s) returned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
