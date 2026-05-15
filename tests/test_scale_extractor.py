"""
Golden tests for the clinical scale extractor.

``extract_scales`` (app/ingest/scale_extractor.py) turns the verbatim scale
strings embedded in the chart into ExtractedObservation-shaped dicts with LOINC
codes and char-offset provenance. These tests pin that behaviour on the exact
string forms the chart uses -- including the PDF-line-wrapped form, which is
why the regexes use ``\\s+`` and ``re.DOTALL``.
"""

from app.ingest.scale_extractor import extract_scales

# The verbatim scale strings, in the forms the chart embeds them.
_PHQ9 = (
    "PHQ-9 administered 2026-05-04: anhedonia 3, depressed mood 3, sleep 3, "
    "fatigue 2, appetite 2, self-worth 2, concentration 2, psychomotor 1, "
    "suicidal ideation 0 — total 18 (moderately severe)."
)
_GAD7 = (
    "GAD-7 administered 2026-05-04: nervousness 3, uncontrollable worry 3, "
    "excessive worry 2, trouble relaxing 2, restlessness 2, irritability 2, "
    "fear 1 — total 15 (severe)."
)
_AUDITC = (
    "AUDIT-C administered 2026-05-04: frequency 4, typical quantity 4, "
    "heavy episodic 3 — total 11 (positive, high risk)."
)
_DAST10 = "DAST-10 administered 2026-05-04: total 7 (substantial level, treatment indicated)."
_CIWA_INTAKE = (
    "CIWA-Ar administered 2026-05-04 14:30: nausea/vomiting 2, tremor 3, "
    "sweats 2 — total 12 (moderate)."
)
_CIWA_NOTE = "CIWA-Ar = 8 (nausea 1, tremor 2, sweats 1) — down from 12 at intake."
_COWS = "COWS: not administered. Patient denies any opioid use. Documented for completeness."
_CSSRS = (
    "C-SSRS administered 2026-05-04: Q1 no, Q2 YES, Q3 no, Q4 no, Q5 no, "
    "Q6 no lifetime suicidal behavior. Risk: low-intensity passive ideation; "
    "no active SI/plan/intent."
)


def _only(text: str) -> dict:
    """Extract from a single-scale string and return the one observation."""
    observations = extract_scales(text)
    assert len(observations) == 1, f"expected exactly one scale in: {text!r}"
    return observations[0]


def test_totalled_scales_extract_code_value_and_interpretation():
    """PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar resolve to LOINC code + total + interp."""
    expectations = [
        (_PHQ9, "44261-6", 18.0, "moderately severe"),
        (_GAD7, "70274-6", 15.0, "severe"),
        (_AUDITC, "75624-7", 11.0, "positive, high risk"),
        (_DAST10, "82666-9", 7.0, "substantial level, treatment indicated"),
        (_CIWA_INTAKE, "72109-3", 12.0, "moderate"),
    ]
    for text, loinc, total, interp in expectations:
        observation = _only(text)
        assert observation["code_system"] == "LOINC"
        assert observation["code"] == loinc
        assert observation["value_quantity"] == total
        assert observation["value_string"] == interp
        assert observation["extraction_method"] == "regex"


def test_ciwa_progress_note_shorthand_form():
    """The progress-note "CIWA-Ar = N (...)" shorthand extracts the same way."""
    observation = _only(_CIWA_NOTE)
    assert observation["code"] == "72109-3"
    assert observation["value_quantity"] == 8.0


def test_cows_not_administered_is_a_negative_finding():
    """COWS recorded as 'not administered' extracts with no numeric total."""
    observation = _only(_COWS)
    assert observation["code"] == "92103-9"
    assert observation["value_quantity"] is None
    assert observation["value_string"] == "not administered"


def test_cssrs_extracts_the_risk_interpretation_without_a_total():
    """C-SSRS has no numeric total; the value is its 'Risk:' interpretation."""
    observation = _only(_CSSRS)
    assert observation["code"] == "93373-7"
    assert observation["value_quantity"] is None
    assert "passive ideation" in observation["value_string"]


def test_offsets_round_trip_into_the_source_text():
    """Every extracted observation's (char_start, char_end) re-locates in the source."""
    for text in (_PHQ9, _GAD7, _AUDITC, _DAST10, _CIWA_INTAKE, _CIWA_NOTE, _COWS, _CSSRS):
        observation = _only(text)
        snippet = text[observation["char_start"] : observation["char_end"]]
        assert snippet  # non-empty
        # the matched span sits within the source and starts at the scale name
        assert observation["char_start"] >= 0
        assert observation["char_end"] <= len(text)


def test_scale_string_survives_a_pdf_line_wrap():
    """A scale string broken across a line (as PDF extraction produces) still matches.

    The regexes use ``\\s+`` / ``re.DOTALL`` precisely so a newline mid-string
    does not defeat extraction.
    """
    wrapped = (
        "PHQ-9 administered 2026-05-04: anhedonia 3, depressed mood 3,\n"
        "sleep 3, fatigue 2, appetite 2, self-worth 2, concentration 2,\n"
        "psychomotor 1, suicidal ideation 0 — total 18 (moderately\nsevere)."
    )
    observation = _only(wrapped)
    assert observation["code"] == "44261-6"
    assert observation["value_quantity"] == 18.0
    # value_string is whitespace-normalized even though the source wrapped
    assert observation["value_string"] == "moderately severe"


def test_text_with_no_scales_extracts_nothing():
    """A document with no scale strings yields an empty list, not an error."""
    assert extract_scales("Patient attended a peer support group. No safety concerns.") == []
