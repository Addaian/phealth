"""
Golden tests for the document classifier and section splitter.

``classify`` and ``split_sections`` are the regex-first heart of the ingestion
pipeline (app/ingest/section_detector.py). These tests pin their behaviour on
small, hand-built golden inputs -- controlled and fast, independent of the real
export -- and assert the char-offset spans round-trip exactly.
"""

from app.ingest.section_detector import classify, split_sections

# Small golden documents -- the structural shape of each format, not the full chart.
_BPS = """[BPS] Intake Assessment
1. Presenting Problem:
Patient reports worsening anxiety.
2. Substance Use History
Daily alcohol use for four years.
3. Risk Assessment
C-SSRS administered: Q2 positive."""

_SOAP = """[SOAP] Progress Note
Subjective
Patient feels less anxious today.
Objective
BP 128/82, affect bright.
Assessment
Anxiety improving on current plan.
Plan
Continue weekly therapy."""

_DAP = """[DAP] Progress Note
Data
Attended group session, participated actively.
Assessment
Engagement improving.
Plan
Continue daily groups."""

_DSAP = """[DSAP] Progress Note
Data
UDS negative; vitals stable.
Subjective
"I feel ready to step down."
Assessment
Clinically ready for a lower level of care.
Plan
Coordinate transfer to PHP."""


def test_classify_dispatches_on_the_bracketed_prefix():
    """Each bracketed template prefix maps to its document type."""
    assert classify("[BPS] Intake Assessment") == "bps_intake"
    assert classify("[SOAP] Progress Note") == "soap"
    assert classify("[DAP] Progress Note") == "dap"
    assert classify("[DSAP] Progress Note") == "dsap"


def test_classify_returns_unknown_without_a_known_prefix():
    """A title with no recognised bracket prefix classifies as 'unknown'."""
    assert classify("Standard Progress Note") == "unknown"
    assert classify("") == "unknown"


def test_split_bps_into_numbered_sections():
    """A BPS intake splits on its ``N. Title`` numbered headers."""
    sections = split_sections(_BPS, "bps_intake")
    assert list(sections) == ["presenting_problem", "substance_use_history", "risk_assessment"]
    assert sections["presenting_problem"]["title"] == "Presenting Problem"
    assert sections["substance_use_history"]["text"] == "Daily alcohol use for four years."


def test_split_soap_into_its_four_sections():
    """A SOAP note splits into subjective / objective / assessment / plan."""
    sections = split_sections(_SOAP, "soap")
    assert list(sections) == ["subjective", "objective", "assessment", "plan"]
    assert sections["objective"]["text"] == "BP 128/82, affect bright."
    assert sections["plan"]["text"] == "Continue weekly therapy."


def test_split_dap_and_dsap_use_their_format_specific_sections():
    """DAP has data/assessment/plan; DSAP adds subjective between data and assessment."""
    assert list(split_sections(_DAP, "dap")) == ["data", "assessment", "plan"]
    assert list(split_sections(_DSAP, "dsap")) == ["data", "subjective", "assessment", "plan"]


def test_section_char_spans_round_trip_exactly():
    """Every section's char_span re-locates its text verbatim in the source.

    This is the provenance guarantee at the section level -- the same property
    the extractors rely on (PRD §5.5).
    """
    for raw_text, doc_type in [
        (_BPS, "bps_intake"),
        (_SOAP, "soap"),
        (_DAP, "dap"),
        (_DSAP, "dsap"),
    ]:
        for section in split_sections(raw_text, doc_type).values():
            start, end = section["char_span"]
            assert raw_text[start:end] == section["text"]
