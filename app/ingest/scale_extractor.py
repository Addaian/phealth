"""
Clinical scale extraction.

Regex extractors for the seven validated scales embedded verbatim in the
synthetic chart -- PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS, C-SSRS. Each
match becomes an ExtractedObservation-shaped dict carrying:
  * a LOINC code (``code_system`` "LOINC"),
  * ``value_quantity`` (the total score) and/or ``value_string`` (the
    interpretation),
  * ``(char_start, char_end)`` provenance into ``raw_text``,
  * ``extraction_method`` "regex", ``confidence`` 1.0.

The regexes are written against the *actual* pdfplumber output: PDF line wraps
turn into arbitrary whitespace mid-sentence, hence ``re.DOTALL`` and ``.*?``
rather than literal spaces. The verbatim scale strings they target are defined
in app/synthetic/persona.yaml -- the same source the chart text was generated
from -- which is what keeps the golden tests in tests/test_scale_extractor.py
stable.

A scale can legitimately appear more than once across the chart: CIWA-Ar is
recorded at intake (12), in the day-2 SOAP note (8), and the day-8 DSAP note
(2). :func:`extract_scales` runs per-document, so each occurrence is captured
against the document it actually appears in.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Scale:
    """A scale's identity plus the regex pattern(s) that recognise it.

    Each pattern exposes an ``interp`` named group (the bracketed or "Risk:"
    interpretation) and, for scored scales, a ``total`` named group. The full
    regex match span is the provenance span.
    """

    name: str
    loinc: str
    display: str
    patterns: tuple[re.Pattern[str], ...]


def _administered_total(name: str) -> re.Pattern[str]:
    """Pattern for the "<NAME> administered <date>: ... total <N> (<interp>)" form.

    Used by PHQ-9, GAD-7, AUDIT-C, DAST-10, and the intake form of CIWA-Ar. The
    ``.*?`` is non-greedy so it stops at the scale's *own* total, not a later
    scale's; it is also zero-or-more, since DAST-10 has no item list between the
    date and the total.
    """
    return re.compile(
        rf"{re.escape(name)}\s+administered\b.*?total\s+(?P<total>\d+)\s*\((?P<interp>[^)]+)\)",
        re.DOTALL,
    )


# The scale catalog. LOINC codes per documents/phase_1.md §H and persona.yaml.
SCALES: tuple[Scale, ...] = (
    Scale("PHQ-9", "44261-6", "PHQ-9 total score", (_administered_total("PHQ-9"),)),
    Scale("GAD-7", "70274-6", "GAD-7 total score", (_administered_total("GAD-7"),)),
    Scale("AUDIT-C", "75624-7", "AUDIT-C total score", (_administered_total("AUDIT-C"),)),
    Scale("DAST-10", "82666-9", "DAST-10 total score", (_administered_total("DAST-10"),)),
    Scale(
        "CIWA-Ar",
        "72109-3",
        "CIWA-Ar total score",
        (
            # Intake form: "CIWA-Ar administered <date> <time>: ... total 12 (moderate)".
            _administered_total("CIWA-Ar"),
            # Progress-note shorthand: "CIWA-Ar = 8 (nausea 1, ...)". Requires an
            # opening paren after the number, so the bare "(CIWA-Ar = 12)" prose
            # mentions in the BPS narrative are correctly ignored.
            re.compile(r"CIWA-Ar\s*=\s*(?P<total>\d+)\s*\((?P<interp>[^)]+)\)"),
        ),
    ),
    Scale(
        "COWS",
        "92103-9",
        "COWS (Clinical Opiate Withdrawal Scale)",
        # Recorded as a deliberate negative finding -- "COWS: not administered ...".
        (re.compile(r"COWS:\s*(?P<interp>not administered)\b.*?completeness\.", re.DOTALL),),
    ),
    Scale(
        "C-SSRS",
        "93373-7",
        "C-SSRS (Columbia Suicide Severity Rating Scale)",
        # No numeric total; the meaningful value is the "Risk:" interpretation.
        # Requires "C-SSRS administered" so the DSAP note's "Re-administer C-SSRS"
        # planning line is not mistaken for an actual result (that absence is gap G3).
        (re.compile(r"C-SSRS\s+administered\b.*?Risk:\s*(?P<interp>.*?intent\.)", re.DOTALL),),
    ),
)


def extract_scales(raw_text: str) -> list[dict]:
    """Find every scale occurrence in one document's ``raw_text``.

    Returns ExtractedObservation-shaped dicts (without ``document_id`` -- the
    orchestrator attaches that), sorted by position. A scale absent from this
    document simply contributes nothing.
    """
    observations: list[dict] = []
    seen_spans: set[tuple[int, int]] = set()
    for scale in SCALES:
        for pattern in scale.patterns:
            for match in pattern.finditer(raw_text):
                span = (match.start(), match.end())
                if span in seen_spans:  # a second pattern matched the same text
                    continue
                seen_spans.add(span)
                observations.append(_build_observation(scale, match))
    observations.sort(key=lambda observation: observation["char_start"])
    return observations


def _build_observation(scale: Scale, match: re.Match[str]) -> dict:
    """Turn a regex match into an ExtractedObservation-shaped dict.

    ``value_string`` is whitespace-normalized (PDF line wraps inside the
    interpretation become single spaces) since it is a derived display value;
    ``char_start``/``char_end`` still point at the raw, un-normalized span, so
    the provenance round-trip is unaffected.
    """
    groups = match.groupdict()
    total = groups.get("total")
    interpretation = " ".join((groups.get("interp") or "").split())
    return {
        "code_system": "LOINC",
        "code": scale.loinc,
        "display": scale.display,
        "value_quantity": float(total) if total is not None else None,
        "value_string": interpretation or None,
        "char_start": match.start(),
        "char_end": match.end(),
        "extraction_method": "regex",
        "confidence": 1.0,
    }
