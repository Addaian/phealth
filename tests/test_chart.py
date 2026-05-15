"""
Tests for the canonical ``GET /api/v1/patients/{id}/chart`` endpoint
(Phase 2 PRD §5.2 + §6.1) -- the Task 2 acceptance milestone.

The chart endpoint is the brief's required deliverable: one consolidated JSON
object with patient + intake + timeline (the brief's literal ask) plus the
extraction surplus (observations with provenance, scales, ASAM evidence
summary, TJC coverage summary, trinary completeness).

These tests assert two contracts:
  1. The response shape matches §6.1 -- meta, patient, intake, timeline,
     observations, asam_summary, tjc_coverage are all present and have the
     expected sub-fields.
  2. Provenance round-trips on every observation in the response:
     ``raw_text[char_start:char_end] == snippet`` (Success Metric M2, 100%).
"""

from datetime import date

from app.api.patients import EXTRACTION_VERSION


def test_chart_returns_canonical_shape(api_client, ingested_patient):
    """The /chart response has every top-level key from PRD §6.1."""
    response = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart")
    assert response.status_code == 200
    body = response.json()

    for key in (
        "meta",
        "patient",
        "intake",
        "timeline",
        "observations",
        "asam_summary",
        "tjc_coverage",
    ):
        assert key in body, f"missing top-level key: {key}"


def test_chart_meta_carries_completeness_and_extraction_version(api_client, ingested_patient):
    """meta.completeness_score ∈ [0, 1]; extraction_version is the stamped constant."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    meta = body["meta"]

    assert meta["extraction_version"] == EXTRACTION_VERSION
    assert 0.0 <= meta["completeness_score"] <= 1.0
    # Marcus's chart fills every BPS section -- score is exactly 1.0.
    assert meta["completeness_score"] == 1.0
    # audit_id / compliance_check_id are Phase 3 slots; nullable in Phase 2.
    assert meta["audit_id"] is None
    assert meta["compliance_check_id"] is None


def test_chart_patient_uses_nested_name_and_identifiers(api_client, ingested_patient):
    """PRD §6.1: patient.name is {given:[...], family:...} and identifiers carries SP."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    patient = body["patient"]

    assert patient["name"]["given"] == [ingested_patient.given_name]
    assert patient["name"]["family"] == ingested_patient.family_name
    sp_ids = [
        identifier
        for identifier in patient["identifiers"]
        if identifier["system"] == "simplepractice"
    ]
    assert len(sp_ids) == 1
    assert sp_ids[0]["value"] == ingested_patient.external_id


def test_chart_intake_uses_loinc_consultation_note(api_client, ingested_patient):
    """intake.loinc_type binds to LOINC 11488-4 Consultation Note (PRD §6.2)."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    intake = body["intake"]

    assert intake["loinc_type"]["system"] == "http://loinc.org"
    assert intake["loinc_type"]["code"] == "11488-4"
    assert intake["loinc_type"]["display"] == "Consultation Note"
    assert intake["document_type"] == "biopsychosocial"
    # All 21 BPS sections present, all "found" for Marcus.
    assert len(intake["sections"]) == 21
    assert all(section["status"] == "found" for section in intake["sections"].values())


def test_chart_intake_scales_carry_mandatory_provenance(api_client, ingested_patient):
    """Every scale must cite a span -- ScaleRead's provenance is required."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    scales = body["intake"]["scales"]

    assert len(scales) > 0
    for scale in scales:
        assert scale["provenance"] is not None
        prov = scale["provenance"]
        assert prov["char_end"] - prov["char_start"] == len(prov["snippet"])


def test_chart_timeline_progress_notes_ordered_oldest_first(api_client, ingested_patient):
    """3 progress notes (SOAP/DAP/DSAP), each bound to LOINC Progress Note, oldest first."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    timeline = body["timeline"]

    assert len(timeline) == 3
    formats = [note["format"] for note in timeline]
    assert formats == ["SOAP", "DAP", "DSAP"]
    for note in timeline:
        assert note["type"] == "progress_note"
        assert note["loinc_type"]["code"] == "11506-3"
        assert note["loinc_type"]["display"] == "Progress Note"
    # Sorted by date ascending.
    dates = [note["date"] for note in timeline]
    assert dates == sorted(dates)


def test_chart_observations_provenance_round_trips(api_client, ingested_patient, db_session):
    """Success Metric M2 — raw_text[char_start:char_end] == snippet for every observation.

    This is the headline provenance contract: the offsets we ship resolve back
    to the exact source span in the document's raw_text. Verified against the
    actual stored documents, not just the API's own self-consistency.
    """
    from sqlmodel import select

    from app.db.models import ClinicalDocument

    raw_text_by_document = {
        doc.id: doc.raw_text
        for doc in db_session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == ingested_patient.id)
        ).all()
    }

    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    observations = body["observations"]

    assert len(observations) > 0
    for observation in observations:
        prov = observation["provenance"]
        raw_text = raw_text_by_document[__uuid(prov["document_id"])]
        assert raw_text[prov["char_start"] : prov["char_end"]] == prov["snippet"], (
            f"provenance round-trip failed for observation {observation['id']}: "
            f"code={observation['code']['code']!r} ({observation['code']['display']!r})"
        )


def test_chart_observations_have_loinc_codes_only(api_client, ingested_patient):
    """The canonical /chart observations list is LOINC-only (PRD §6.1 example)."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    for observation in body["observations"]:
        assert observation["code"]["system"] == "http://loinc.org"


def test_chart_asam_summary_has_six_dimension_counts(api_client, ingested_patient):
    """asam_summary.dim_1..dim_6 are non-negative ints; sum > 0 for Marcus."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    summary = body["asam_summary"]

    for n in range(1, 7):
        assert isinstance(summary[f"dim_{n}"], int)
        assert summary[f"dim_{n}"] >= 0
    # Marcus's chart has evidence across multiple dimensions.
    assert sum(summary[f"dim_{n}"] for n in range(1, 7)) > 0


def test_chart_tjc_coverage_summary_lists_5_gap_eps(api_client, ingested_patient):
    """The 5 intentional Joint Commission gaps (G1–G5) show up in gap_eps."""
    body = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    summary = body["tjc_coverage"]

    assert summary["total_eps"] == 8
    expected_gaps = {"CTS.03.01.09", "CTS.03.01.03", "NPSG.15.01.01", "R3-25", "RC.01.02.01"}
    assert set(summary["gap_eps"]) == expected_gaps
    assert summary["covered_eps"] == summary["total_eps"] - len(expected_gaps)
    # sanity: there are 0 ambiguous + 5 gap + 3 satisfied = 8. covered counts satisfied only.


def test_chart_returns_404_for_unknown_patient(api_client):
    """An unknown patient id returns the RFC 7807 404, not a partial chart."""
    import uuid

    response = api_client.get(f"/api/v1/patients/{uuid.uuid4()}/chart")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")


def test_chart_meta_generated_at_is_stable_across_requests(api_client, ingested_patient):
    """meta.generated_at is the chart's latest authored_on (deterministic).

    Stable across calls so the ETag/If-None-Match contract (PRD §5.7) holds:
    a wall-clock value would mutate every response and force every conditional
    request to miss. The source-document timestamp is also the more honest
    answer to "when was this chart generated?".
    """
    first = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    second = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()
    assert first["meta"]["generated_at"] == second["meta"]["generated_at"]

    # And the date portion still parses as a valid ISO 8601 date.
    iso_date = first["meta"]["generated_at"][:10]
    assert date.fromisoformat(iso_date)


def test_chart_supports_if_none_match_304(api_client, ingested_patient):
    """PRD §5.7: a repeat GET with If-None-Match: <etag> returns 304.

    Regression guard for the wall-clock generated_at bug: if any field in the
    /chart body becomes request-time-dependent, this test fails.
    """
    first = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart")
    assert first.status_code == 200
    etag = first.headers["etag"]

    second = api_client.get(
        f"/api/v1/patients/{ingested_patient.id}/chart",
        headers={"If-None-Match": etag},
    )
    assert second.status_code == 304
    assert second.content == b""


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def __uuid(value: str):
    """uuid.UUID with the import inline to keep the imports list short above."""
    import uuid

    return uuid.UUID(value)
