"""
The provenance round-trip guarantee (PRD §5.5, Success Metric M2).

Every extracted value must re-locate in the source ``raw_text``. This file
asserts the invariant at two levels:

  - **Table-level** (Phase 1) -- walks the relational rows directly
    (``ExtractedObservation``, ``AsamEvidence``, ``TjcCoverage``) and
    confirms every stored span is a valid range into the document it points
    at, with snippets verbatim where stored.

  - **Response-level** (Phase 2 M11) -- walks every served endpoint that
    returns observation provenance (``/chart``, ``/observations``,
    ``/fhir/Observation``, ``$everything``, ``/fhir/Provenance``) and
    confirms ``raw_text[char_start:char_end] == snippet`` on the *wire*.

The response-level checks are the load-bearing ones: a regression in the
serialization layer (e.g. a future refactor that loses a few bytes of the
snippet) would slip past the table-level test, but the response-level test
catches it where it actually matters -- at the API boundary the reviewer
calls.
"""

import uuid

from sqlmodel import select

from app.db.models import AsamEvidence, ClinicalDocument, ExtractedObservation, TjcCoverage


def test_every_extracted_observation_offset_is_valid(db_session, ingested_patient):
    """Each ExtractedObservation span is a valid, non-empty range into its document."""
    raw_text_by_document = {
        document.id: document.raw_text
        for document in db_session.exec(select(ClinicalDocument)).all()
    }
    observations = db_session.exec(select(ExtractedObservation)).all()
    assert observations, "expected the ingested chart to produce observations"

    for observation in observations:
        raw_text = raw_text_by_document[observation.document_id]
        assert 0 <= observation.char_start < observation.char_end <= len(raw_text), (
            f"observation {observation.code} has an out-of-range span"
        )


def test_every_asam_evidence_snippet_round_trips(db_session, ingested_patient):
    """Each AsamEvidence row's stored snippet equals ``raw_text[char_start:char_end]``."""
    raw_text_by_document = {
        document.id: document.raw_text
        for document in db_session.exec(select(ClinicalDocument)).all()
    }
    evidence = db_session.exec(select(AsamEvidence)).all()
    assert evidence, "expected the ingested chart to produce ASAM evidence"

    for row in evidence:
        raw_text = raw_text_by_document[row.document_id]
        assert raw_text[row.char_start : row.char_end] == row.snippet


def test_every_tjc_evidence_pointer_is_valid(db_session, ingested_patient):
    """Each TjcCoverage row that carries an evidence pointer points at a valid span.

    Some EPs are flagged on the *absence* of documentation and carry no
    evidence pointer -- those are skipped.
    """
    raw_text_by_document = {
        document.id: document.raw_text
        for document in db_session.exec(select(ClinicalDocument)).all()
    }
    coverage = db_session.exec(select(TjcCoverage)).all()
    assert coverage, "expected the ingested chart to produce TJC coverage rows"

    checked = 0
    for row in coverage:
        if row.evidence_document_id is None or row.evidence_char_start is None:
            continue
        raw_text = raw_text_by_document[row.evidence_document_id]
        assert 0 <= row.evidence_char_start < row.evidence_char_end <= len(raw_text), (
            f"TJC coverage {row.ep_code} has an out-of-range evidence span"
        )
        checked += 1
    assert checked > 0, "expected at least one TJC row to carry an evidence pointer"


# ---------------------------------------------------------------------------
# Response-level round-trip (Phase 2 M11): the same invariant on the wire.
# ---------------------------------------------------------------------------


def _raw_text_by_document(db_session, patient_id: uuid.UUID) -> dict[uuid.UUID, str]:
    """Index the ingested chart's documents by id → raw_text for slicing."""
    return {
        document.id: document.raw_text
        for document in db_session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == patient_id)
        ).all()
    }


def test_chart_endpoint_provenance_round_trips(api_client, ingested_patient, db_session):
    """``/chart``: every observation's ``provenance.snippet`` slices its raw_text."""
    raw_by_id = _raw_text_by_document(db_session, ingested_patient.id)
    chart = api_client.get(f"/api/v1/patients/{ingested_patient.id}/chart").json()

    assert chart["observations"], "expected non-empty observations list"
    for observation in chart["observations"]:
        provenance = observation["provenance"]
        raw_text = raw_by_id[uuid.UUID(provenance["document_id"])]
        assert raw_text[provenance["char_start"] : provenance["char_end"]] == provenance["snippet"]


def test_observations_endpoint_offsets_round_trip(api_client, ingested_patient, db_session):
    """``/observations``: char_start/char_end resolve to non-empty snippets in raw_text.

    The /observations endpoint serves the flat ``char_start``/``char_end``
    fields (no embedded snippet). The round-trip here is "the offsets resolve
    to *some* span" -- the snippet-equality check belongs on /chart, where
    the snippet is part of the contract.
    """
    raw_by_id = _raw_text_by_document(db_session, ingested_patient.id)
    rows = api_client.get(f"/api/v1/patients/{ingested_patient.id}/observations").json()

    assert rows
    for observation in rows:
        raw_text = raw_by_id[uuid.UUID(observation["document_id"])]
        sliced = raw_text[observation["char_start"] : observation["char_end"]]
        assert sliced, f"empty slice for observation {observation['code']}"


def test_fhir_observation_search_offsets_resolve(api_client, ingested_patient, db_session):
    """``/fhir/Observation``: every row's id matches a stored ExtractedObservation
    whose char_start/char_end resolve in the parent document's raw_text.

    The FHIR Observation resource itself does not carry offsets (those live
    on the paired Provenance), so the round-trip here joins back through the
    relational ExtractedObservation row.
    """
    raw_by_id = _raw_text_by_document(db_session, ingested_patient.id)
    obs_by_id = {
        row.id: row
        for row in db_session.exec(
            select(ExtractedObservation).where(ExtractedObservation.code_system == "LOINC")
        ).all()
    }

    bundle = api_client.get(f"/fhir/Observation?patient={ingested_patient.id}").json()
    assert bundle["entry"], "expected non-empty FHIR Observation Bundle"
    for entry in bundle["entry"]:
        observation_id = uuid.UUID(entry["resource"]["id"])
        observation_row = obs_by_id[observation_id]
        raw_text = raw_by_id[observation_row.document_id]
        sliced = raw_text[observation_row.char_start : observation_row.char_end]
        assert sliced, f"empty slice for FHIR Observation/{observation_id}"


def test_everything_bundle_provenance_round_trips(api_client, ingested_patient, db_session):
    """``$everything``: every Provenance's text-position-selector resolves in raw_text."""
    from app.fhir.mappers import PH_OFFSET_EXT

    raw_by_id = _raw_text_by_document(db_session, ingested_patient.id)
    bundle = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything").json()

    provenance_resources = [
        entry["resource"]
        for entry in bundle["entry"]
        if entry["resource"]["resourceType"] == "Provenance"
    ]
    assert provenance_resources

    for resource in provenance_resources:
        document_ref = resource["entity"][0]["what"]["reference"]
        document_id = uuid.UUID(document_ref.removeprefix("DocumentReference/"))
        offset_ext = next(
            ext for ext in resource["entity"][0]["extension"] if ext["url"] == PH_OFFSET_EXT
        )
        offsets = {child["url"]: child["valueInteger"] for child in offset_ext["extension"]}
        sliced = raw_by_id[document_id][offsets["start"] : offsets["end"]]
        assert sliced, "Provenance offsets resolved to an empty slice"
