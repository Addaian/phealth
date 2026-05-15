"""
End-to-end deterministic-core test: chart -> ratings -> decision.

After M2 (rubric) + M3 (risk_ratings) + M4 (level_decision) the entire
path from the ingested SimplePractice chart to a recommended Level-of-Care
is **pure Python** -- zero Claude calls. This test asserts the demo-
defining outcome at the boundary of the deterministic core.

If this test fails, the bug is upstream of any LLM concerns -- one of
M2/M3/M4 produces the wrong output. Per the implementation plan, fix the
deterministic core first; don't paper over a wrong recommendation with
LLM narration.
"""

from app.clinical.asam.level_decision import decide
from app.clinical.asam.risk_ratings import compute_risk_ratings


def test_marcus_full_chart_resolves_to_level_3_7(ingested_patient, db_session):
    """The full deterministic stack on Marcus's live ingested chart:

        chart rows -> compute_risk_ratings -> decide -> Level 3.7

    Non-COE, non-BIO. This is the recommendation the Phase 3 endpoint
    will surface in the demo, *before* Claude writes the rationale.
    """
    ratings = compute_risk_ratings(ingested_patient.id, db_session)
    decision = decide(ratings)

    assert decision.level == "3.7", (
        f"Marcus should resolve to Level 3.7 but got {decision.level!r}; "
        f"ratings were {ratings}; rules fired: {decision.rules_fired}"
    )
    assert decision.co_occurring_enhanced is False
    assert decision.biomedical_enhanced is False
    assert decision.modifiers == []
    # The cascade should fire at least one rule.
    assert len(decision.rules_fired) >= 1
