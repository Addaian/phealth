"""
Internal-consistency tests for app/synthetic/persona.yaml.

persona.yaml is the single source of truth for the synthetic patient — every
downstream artifact (the SimplePractice chart, the generated chart text, the
M9 golden tests, the ASAM evidence index, the TJC coverage matrix) traces back
to it. These tests codify the M0 acceptance criteria so that an edit which
breaks the persona's internal consistency fails loudly rather than silently
corrupting everything built on top of it.
"""


def test_persona_parses_with_expected_sections(persona):
    """persona.yaml loads as a mapping containing every top-level section the
    downstream pipeline depends on."""
    expected = {
        "demographics",
        "encounter",
        "presenting_problem",
        "substance_use",
        "medical_history",
        "psychiatric_history",
        "family_social_history",
        "mental_status_exam",
        "scales",
        "asam_dimensions",
        "recommended_level_of_care",
        "treatment_plan",
        "progress_notes",
        "intentional_gaps",
    }
    assert expected.issubset(persona.keys())


def test_all_six_asam_dimensions_have_evidence(persona):
    """Every ASAM 4th-edition dimension (1..6) carries at least one evidence
    anchor — the property the M6 ASAM evidence indexer relies on."""
    dims = persona["asam_dimensions"]
    for n in range(1, 7):
        key = f"dim_{n}"
        assert key in dims, f"missing ASAM {key}"
        assert dims[key].get("evidence"), f"{key} has no evidence anchors"


def test_all_five_gaps_present_and_anchored(persona):
    """All five intentional compliance gaps (G1..G5) are present, each with an
    EP code, a description, and a chart location it is embedded in."""
    gaps = {g["id"]: g for g in persona["intentional_gaps"]}
    assert set(gaps) == {"G1", "G2", "G3", "G4", "G5"}
    for gid, gap in gaps.items():
        assert gap.get("ep_code"), f"{gid} missing ep_code"
        assert gap.get("gap"), f"{gid} missing gap description"
        assert gap.get("embedded_in"), f"{gid} missing embedded_in location"


def test_scale_item_scores_sum_to_totals(persona):
    """Item-level scores sum to the documented total for each scored scale.

    This is the invariant the M6 regex scale extractor and its golden tests
    depend on — a chart where PHQ-9 items don't sum to 18 is not extractable.
    """
    scales = persona["scales"]

    def _sum(items: dict) -> int:
        return sum(v for v in items.values() if isinstance(v, int | float))

    assert _sum(scales["phq9"]["items"]) == scales["phq9"]["total"] == 18
    assert _sum(scales["gad7"]["items"]) == scales["gad7"]["total"] == 15
    assert _sum(scales["audit_c"]["items"]) == scales["audit_c"]["total"] == 11
    assert _sum(scales["ciwa_ar"]["items"]) == scales["ciwa_ar"]["total_at_intake"] == 12


def test_all_seven_scales_present(persona):
    """All seven clinical scales the chart embeds are defined in the persona."""
    expected = {"phq9", "gad7", "audit_c", "dast10", "ciwa_ar", "cows", "c_ssrs"}
    assert expected.issubset(persona["scales"].keys())


def test_three_progress_notes_with_distinct_formats_and_dates(persona):
    """Exactly three progress notes, one each in SOAP / DAP / DSAP format, on
    three distinct dates (assessment brief, Task 1)."""
    notes = persona["progress_notes"]
    assert len(notes) == 3
    assert sorted(n["format"] for n in notes) == ["DAP", "DSAP", "SOAP"]
    dates = [n["date"] for n in notes]
    assert len(set(dates)) == 3, "progress notes must be on distinct dates"
