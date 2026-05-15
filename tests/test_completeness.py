"""
Tests for the trinary completeness classifier (Phase 2 PRD §5.5).

Covers both:
- The pure classifier (``app/api/completeness.py``) -- unit tests with
  hand-built section dicts, no FastAPI, no database.
- The /api/v1/patients/{id}/intake endpoint -- end-to-end check that Marcus's
  fully-populated BPS chart scores 1.0 across all 21 sections.
"""

import copy

import pytest

from app.api.completeness import (
    BPS_INTAKE_TEMPLATE,
    classify_all,
    classify_section,
    completeness_score,
    template_for,
)

# ---------------------------------------------------------------------------
# Pure-classifier unit tests
# ---------------------------------------------------------------------------


def test_template_for_bps_returns_21_sections():
    assert len(template_for("bps_intake")) == 21


def test_template_for_unknown_doc_type_is_empty():
    """An unknown document type has no template -- every section is not_assessed."""
    assert template_for("xxx") == frozenset()


def test_classify_section_found_when_text_non_empty():
    sections = {"presenting_problem": {"title": "Presenting Problem", "text": "anxiety"}}
    assert classify_section("presenting_problem", sections, BPS_INTAKE_TEMPLATE) == "found"


def test_classify_section_looked_but_missing_when_in_template_but_absent():
    """The template lists ``legal``; the chart didn't include it."""
    assert classify_section("legal", {}, BPS_INTAKE_TEMPLATE) == "looked_but_missing"


def test_classify_section_looked_but_missing_when_text_is_whitespace_only():
    """Whitespace-only text counts as empty -- the assessor "filled" it with nothing."""
    sections = {"legal": {"title": "Legal", "text": "   \n  "}}
    assert classify_section("legal", sections, BPS_INTAKE_TEMPLATE) == "looked_but_missing"


def test_classify_section_not_assessed_when_outside_template():
    """A section the template never asked for is ``not_assessed``."""
    sections = {"random_extra_section": {"title": "Extra", "text": "stuff"}}
    assert classify_section("random_extra_section", sections, BPS_INTAKE_TEMPLATE) == "not_assessed"


def test_classify_all_returns_status_for_every_template_section():
    """A chart with one filled section: 20 looked_but_missing + 1 found."""
    sections = {"presenting_problem": {"title": "Presenting Problem", "text": "x"}}
    statuses = classify_all(sections, "bps_intake")
    found = [k for k, v in statuses.items() if v == "found"]
    looked = [k for k, v in statuses.items() if v == "looked_but_missing"]
    assert found == ["presenting_problem"]
    assert len(looked) == 20


def test_completeness_score_perfect():
    """All 21 sections found → score = 1.0."""
    statuses = {key: "found" for key in BPS_INTAKE_TEMPLATE}
    assert completeness_score(statuses) == 1.0


def test_completeness_score_partial():
    """1 found + 1 looked_but_missing → 0.5."""
    statuses = {"a": "found", "b": "looked_but_missing"}
    assert completeness_score(statuses) == pytest.approx(0.5)


def test_completeness_score_excludes_not_assessed():
    """1 found + 1 looked_but_missing + 99 not_assessed → still 0.5."""
    statuses = {"a": "found", "b": "looked_but_missing"}
    for n in range(99):
        statuses[f"extra_{n}"] = "not_assessed"
    assert completeness_score(statuses) == pytest.approx(0.5)


def test_completeness_score_no_asked_sections_is_one():
    """Vacuous truth: a chart with no asked sections is 'perfect' (1.0)."""
    statuses = {"a": "not_assessed", "b": "not_assessed"}
    assert completeness_score(statuses) == 1.0


# ---------------------------------------------------------------------------
# Endpoint integration: /api/v1/patients/{id}/intake
# ---------------------------------------------------------------------------


def test_intake_endpoint_marcus_scores_perfect(api_client, ingested_patient):
    """Marcus's chart fills all 21 BPS sections -- score is 1.0, every status 'found'."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/intake")
    assert response.status_code == 200
    body = response.json()

    assert body["completeness_score"] == 1.0
    assert len(body["sections"]) == 21
    assert all(section["status"] == "found" for section in body["sections"].values())


def test_intake_endpoint_with_blanked_section_drops_score(api_client, ingested_patient, db_session):
    """Artificially blank one section -- it flips to looked_but_missing, score drops."""
    from sqlmodel import select

    from app.db.models import ClinicalDocument

    intake = db_session.exec(
        select(ClinicalDocument).where(
            ClinicalDocument.patient_id == ingested_patient.id,
            ClinicalDocument.document_type == "bps_intake",
        )
    ).one()

    # Mutate the JSONB column in place: blank the "legal" section. Mark the
    # column dirty so SQLAlchemy emits an UPDATE despite the JSONB being a
    # mutable Python dict.
    from sqlalchemy.orm.attributes import flag_modified

    mutated = copy.deepcopy(intake.sections)
    mutated["legal"]["text"] = ""
    intake.sections = mutated
    flag_modified(intake, "sections")
    db_session.flush()

    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/intake")
    assert response.status_code == 200
    body = response.json()

    assert body["sections"]["legal"]["status"] == "looked_but_missing"
    # 20 found / 21 asked = ~0.9524
    assert body["completeness_score"] == pytest.approx(20 / 21)
