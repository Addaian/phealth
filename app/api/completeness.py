"""
Trinary completeness classification + score (Phase 2 PRD §5.5).

Three states for any section in a clinical document:

  * ``"found"``               -- present in the chart with non-empty text
  * ``"looked_but_missing"``  -- listed in the document type's template, but
                                 empty (or absent) in this chart
  * ``"not_assessed"``        -- not in the template at all (the assessor was
                                 never given a prompt for this section)

The distinction matters clinically. A missing PHQ-9 the assessor *was*
prompted to fill is a quality gap; a missing PHQ-9 in a document that never
had that section is irrelevant absence. Trinary status preserves that nuance
where binary present/absent would flatten it.

The BPS template is hard-coded in this module rather than seeded from YAML
because the 21-section list is a presentation-layer policy, not a data
contract. Phase 1's section_detector derives section keys empirically from
the chart's numbered headers; this module records the *expected* set the
SimplePractice [BPS] template corresponds to, so the API can tell the
difference between "the chart has 21 sections" and "the chart was asked for
21 sections and answered all of them."

The completeness score is::

    score = |found| / (|found| + |looked_but_missing|)

``not_assessed`` sections are excluded from both numerator and denominator --
the score is "the fraction of *asked* questions that were *answered*". A
chart with no asked sections scores 1.0 (vacuously perfect); handlers should
display this only when the denominator is meaningful.
"""

from app.api.schemas import SectionStatus

# The 21 BPS intake sections per the SimplePractice [BPS] template
# (see app/synthetic/chart_text/bps_intake.txt §1..§21). Slug keys match what
# section_detector._slug produces for the corresponding headers.
BPS_INTAKE_TEMPLATE: frozenset[str] = frozenset(
    {
        "presenting_problem",
        # The signs-and-symptoms header has a long parenthetical that the
        # slugger compresses; keep the actual produced slug, verified live
        # against the ingested chart.
        "signs_and_symptoms_dsm_v_tr_based_resulting_in_impairment_s",
        "history_of_presenting_problem",
        "current_family_and_significant_relationships",
        "childhood_adolescent_history",
        "social_relationships",
        "cultural_ethnic",
        "spiritual_religious",
        "legal",
        "education",
        "employment_vocational",
        "military",
        "leisure_recreational",
        "physical_health",
        "chemical_use_history",
        "counseling_prior_treatment_history",
        "past_psychiatric_history",
        "mental_status_exam",
        "risk_assessment",
        "diagnosis",
        "treatment_plan",
    }
)

# Progress-note templates: the section labels each format expects. The
# section_detector lowercases the labels before storing them, so the keys
# below match the stored slugs exactly.
PROGRESS_NOTE_TEMPLATES: dict[str, frozenset[str]] = {
    "soap": frozenset({"subjective", "objective", "assessment", "plan"}),
    "dap": frozenset({"data", "assessment", "plan"}),
    "dsap": frozenset({"data", "subjective", "assessment", "plan"}),
}

# All templates, keyed by document_type (matches ClinicalDocument.document_type).
TEMPLATES_BY_DOC_TYPE: dict[str, frozenset[str]] = {
    "bps_intake": BPS_INTAKE_TEMPLATE,
    **PROGRESS_NOTE_TEMPLATES,
}


def template_for(doc_type: str) -> frozenset[str]:
    """Return the expected section keys for ``doc_type``, or an empty set."""
    return TEMPLATES_BY_DOC_TYPE.get(doc_type, frozenset())


def _section_has_text(section: object) -> bool:
    """True if ``section`` has a non-empty ``text`` attribute / key.

    Tolerates either a dict (the storage form) or a Pydantic model
    (constructed-by-test form). Whitespace-only text is considered empty so a
    section the assessor "filled" with a blank space counts as missing.
    """
    if isinstance(section, dict):
        text = section.get("text")
    else:
        text = getattr(section, "text", None)
    return bool(text and str(text).strip())


def classify_section(
    section_key: str,
    sections: dict[str, object],
    template: frozenset[str],
) -> SectionStatus:
    """Return one section's trinary completeness status.

    ``"found"`` requires both: in the template AND present with non-empty text.
    ``"looked_but_missing"`` is in the template but absent/empty in the chart.
    ``"not_assessed"`` is not in the template at all.
    """
    if section_key not in template:
        return "not_assessed"
    section = sections.get(section_key)
    if section is None:
        return "looked_but_missing"
    return "found" if _section_has_text(section) else "looked_but_missing"


def classify_all(sections: dict[str, object], doc_type: str) -> dict[str, SectionStatus]:
    """Return trinary status for every expected key plus any chart extras.

    The returned dict's keys are the union of (template keys) and (sections
    actually present). Template keys never seen in the chart are
    "looked_but_missing"; chart keys not in the template are "not_assessed".
    """
    template = template_for(doc_type)
    statuses: dict[str, SectionStatus] = {
        key: classify_section(key, sections, template) for key in template
    }
    for key in sections:
        statuses.setdefault(key, "not_assessed")
    return statuses


def completeness_score(statuses: dict[str, SectionStatus]) -> float:
    """Return the completeness score from a status map.

    ``|found| / (|found| + |looked_but_missing|)``; "not_assessed" is
    excluded from both terms. Returns 1.0 when the denominator is zero (no
    section was asked, so vacuously perfect).
    """
    found = sum(1 for status in statuses.values() if status == "found")
    looked = sum(1 for status in statuses.values() if status == "looked_but_missing")
    denominator = found + looked
    if denominator == 0:
        return 1.0
    return found / denominator
