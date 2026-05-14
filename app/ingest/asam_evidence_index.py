"""
ASAM-evidence index builder.

For one document, emits ``AsamEvidence``-shaped dicts -- one per (dimension,
span) -- anchoring evidence for the six ASAM 4th-edition dimensions back to a
char-offset span in ``raw_text``. Two sources of evidence:

  * **Keyword hooks** -- each dimension in ``data/seed/asam_dimensions.yaml``
    carries a keyword list; the first occurrence of each keyword anchors an
    evidence span (a context window around the keyword).
  * **Scale-result hooks** -- an extracted scale is high-precision evidence for
    a specific dimension (CIWA-Ar/COWS -> withdrawal, PHQ-9/GAD-7/C-SSRS ->
    psychiatric, AUDIT-C/DAST-10 -> substance-use risk). These reuse the
    scale's own extracted span.

This pre-computed index is what turns the Phase 3 ASAM endpoint into a SELECT
over evidence rows rather than a full-document inference (PRD §1).
"""

import re

from app.ingest.seed import load_seed

# Characters of context kept on each side of a matched keyword, so the stored
# ``snippet`` reads as a sentence fragment rather than a bare word.
_CONTEXT_PAD = 80

# LOINC code -> the ASAM dimension the scale is primary evidence for.
SCALE_DIMENSION: dict[str, int] = {
    "72109-3": 1,  # CIWA-Ar  -> Dim 1, alcohol/benzo withdrawal
    "92103-9": 1,  # COWS     -> Dim 1, opioid withdrawal
    "44261-6": 3,  # PHQ-9    -> Dim 3, depression
    "70274-6": 3,  # GAD-7    -> Dim 3, anxiety
    "93373-7": 3,  # C-SSRS   -> Dim 3, suicide risk
    "75624-7": 4,  # AUDIT-C  -> Dim 4, alcohol-use risk
    "82666-9": 4,  # DAST-10  -> Dim 4, drug-use risk
}


def build_asam_evidence(raw_text: str, scale_observations: list[dict]) -> list[dict]:
    """Build the ASAM-evidence index for one document.

    ``scale_observations`` is the output of scale_extractor for the same
    document. Returns ``AsamEvidence``-shaped dicts (without ``document_id`` --
    the orchestrator attaches that), de-duplicated on ``(dimension, char_start)``
    to respect the table's composite primary key.
    """
    evidence: list[dict] = []
    seen: set[tuple[int, int]] = set()  # (dimension, char_start)

    def add(dimension: int, char_start: int, char_end: int, subdimension: str) -> None:
        if (dimension, char_start) in seen:
            return
        seen.add((dimension, char_start))
        evidence.append(
            {
                "dimension": dimension,
                "char_start": char_start,
                "char_end": char_end,
                "subdimension": subdimension,
                "snippet": raw_text[char_start:char_end],
            }
        )

    # Keyword hooks: first occurrence of each dimension keyword, with context.
    for entry in load_seed("asam_dimensions.yaml")["dimensions"]:
        dimension = entry["number"]
        for keyword in entry["keywords"]:
            match = re.search(rf"\b{re.escape(keyword)}\b", raw_text, re.IGNORECASE)
            if match is None:
                continue
            start = max(0, match.start() - _CONTEXT_PAD)
            end = min(len(raw_text), match.end() + _CONTEXT_PAD)
            add(dimension, start, end, f"keyword:{keyword}")

    # Scale-result hooks: reuse the scale's own extracted span (high precision).
    for observation in scale_observations:
        dimension = SCALE_DIMENSION.get(observation["code"])
        if dimension is None:
            continue
        add(
            dimension,
            observation["char_start"],
            observation["char_end"],
            f"scale:{observation['display']}",
        )

    evidence.sort(key=lambda row: (row["dimension"], row["char_start"]))
    return evidence
