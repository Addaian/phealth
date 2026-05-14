"""
Tests for the deterministic chart-text generator (app/synthetic/generate_chart_text.py).

The generator renders persona.yaml into the field-by-field text the clinician
pastes into SimplePractice. These tests guard its structural contract: every
BPS section present, every progress note in its format-specific shape, and —
most importantly — the verbatim scale strings reproduced exactly, since the M6
scale extractor's regexes (and their golden tests) are written against those
exact strings.

The render functions are pure (persona dict in, string out), so they are
imported and called directly — no file-system side effects in tests.
"""

from app.synthetic.generate_chart_text import render_bps_intake, render_progress_note


def test_bps_intake_has_all_21_sections(persona):
    """The rendered BPS intake contains all 21 numbered section banners."""
    text = render_bps_intake(persona)
    for n in range(1, 22):
        assert f"\n§{n}." in text, f"BPS section §{n} missing from generated output"


def test_bps_intake_embeds_verbatim_scales(persona):
    """Every scale's verbatim string is reproduced exactly in the BPS output.

    This is the contract the M6 regex scale extractor relies on: if the
    generator paraphrased a scale, extraction (and its golden tests) would
    break. The generator collapses YAML folded-block whitespace, so the
    expected string is collapsed the same way before comparison.
    """
    text = render_bps_intake(persona)
    for scale_key in ("phq9", "gad7", "audit_c", "dast10", "ciwa_ar", "cows", "c_ssrs"):
        verbatim = " ".join(persona["scales"][scale_key]["verbatim"].split())
        assert verbatim in text, f"{scale_key} verbatim string not found in BPS output"


def test_progress_notes_render_with_format_specific_sections(persona):
    """Each progress note renders with exactly the sections its format defines
    (SOAP / DAP / DSAP), labelled as the M6 section detector expects."""
    expected_sections = {
        "SOAP": ["Subjective", "Objective", "Assessment", "Plan"],
        "DAP": ["Data", "Assessment", "Plan"],
        "DSAP": ["Data", "Subjective", "Assessment", "Plan"],
    }
    for idx, note in enumerate(persona["progress_notes"]):
        text = render_progress_note(persona, idx)
        for section in expected_sections[note["format"]]:
            assert f"▶ {section}" in text, (
                f"note {idx} ({note['format']}) missing section '{section}'"
            )


def test_progress_note_gap_footers_are_author_only(persona):
    """Each progress note carries its intentional-gap footer flagged as
    author-only — it must never be pasted into SimplePractice."""
    for idx in range(len(persona["progress_notes"])):
        text = render_progress_note(persona, idx)
        assert "DO NOT paste this footer" in text
