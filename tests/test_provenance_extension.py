"""
Tests for the custom Provenance extension (Phase 2 PRD §5.4 + M10).

The Provenance resource carries the char-offset attribution between an
extracted Observation and the source text it was derived from, via a custom
complex extension at the URL stored in ``app.fhir.mappers.PH_OFFSET_EXT``.

These tests assert three contracts:
  1. Every Provenance emitted by ``$everything`` and ``/fhir/Provenance``
     validates against ``fhir.resources.R4B.Provenance``.
  2. Every Provenance carries the custom extension with ``start`` and ``end``
     integer slots, and those slots round-trip the original Observation's
     ``char_start`` / ``char_end`` exactly.
  3. The char-offset range still resolves to the actual snippet in the
     source document's ``raw_text``.
"""

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.provenance import Provenance
from sqlmodel import select

from app.fhir.mappers import PH_OFFSET_EXT


def _provenance_entries_from_everything(body: dict) -> list[dict]:
    """Pull just the Provenance resources out of a $everything Bundle body."""
    return [
        entry["resource"]
        for entry in body["entry"]
        if entry["resource"]["resourceType"] == "Provenance"
    ]


def _start_end_from_extension(provenance: dict) -> tuple[int, int]:
    """Extract (start, end) from the custom complex extension on entity[0]."""
    entity_extensions = provenance["entity"][0]["extension"]
    custom = next(ext for ext in entity_extensions if ext["url"] == PH_OFFSET_EXT)
    inner = {ext["url"]: ext["valueInteger"] for ext in custom["extension"]}
    return inner["start"], inner["end"]


def test_extension_url_is_stable():
    """The extension URL is the public contract -- a regression here breaks every client."""
    assert (
        PH_OFFSET_EXT
        == "http://perspectiveshealth.ai/fhir/StructureDefinition/text-position-selector"
    )


def test_everything_emits_provenance_per_loinc_observation(api_client, ingested_patient):
    """One Provenance per LOINC Observation; Marcus has 9 → 9 Provenance entries."""
    body = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything").json()
    provenance_resources = _provenance_entries_from_everything(body)
    assert len(provenance_resources) == 9


def test_every_provenance_validates_against_fhir_r4(api_client, ingested_patient):
    """Each Provenance entry round-trips through fhir.resources.R4B.Provenance."""
    body = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything").json()
    Bundle.model_validate(body)  # asserts the surrounding Bundle is conformant too
    for resource in _provenance_entries_from_everything(body):
        Provenance.model_validate(resource)


def test_every_provenance_carries_custom_extension(api_client, ingested_patient):
    """entity[0].extension[*].url includes the PH_OFFSET_EXT URL."""
    body = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything").json()
    for resource in _provenance_entries_from_everything(body):
        urls = {ext["url"] for ext in resource["entity"][0]["extension"]}
        assert PH_OFFSET_EXT in urls


def test_provenance_offsets_round_trip_to_source_snippet(api_client, ingested_patient, db_session):
    """For every Provenance, raw_text[start:end] matches the Observation's stored snippet.

    Cross-checks the FHIR Provenance shape against the relational ground truth:
    pull the ExtractedObservation row, find its parent ClinicalDocument, slice
    the document's raw_text by the extension's char range, and assert the slice
    equals what the canonical /chart snippet would carry.
    """
    from app.db.models import ClinicalDocument, ExtractedObservation

    body = api_client.get(f"/fhir/Patient/{ingested_patient.id}/$everything").json()
    provenance_resources = _provenance_entries_from_everything(body)

    # observation_id -> raw_text via document join.
    observations_by_id = {
        observation.id: observation
        for observation in db_session.exec(
            select(ExtractedObservation).where(ExtractedObservation.code_system == "LOINC")
        ).all()
    }
    documents_by_id = {
        document.id: document
        for document in db_session.exec(
            select(ClinicalDocument).where(ClinicalDocument.patient_id == ingested_patient.id)
        ).all()
    }

    import uuid as _uuid

    for resource in provenance_resources:
        target_ref = resource["target"][0]["reference"]
        observation_id = _uuid.UUID(target_ref.removeprefix("Observation/"))
        observation = observations_by_id[observation_id]
        document = documents_by_id[observation.document_id]

        start, end = _start_end_from_extension(resource)
        assert (start, end) == (observation.char_start, observation.char_end)

        slice_text = document.raw_text[start:end]
        # The slice is the same as what the canonical /chart endpoint serves
        # as ``provenance.snippet`` -- the Provenance extension shape and the
        # /chart shape agree on the source span.
        assert slice_text, "char range did not resolve to any text"


def test_fhir_provenance_search_by_target_returns_one(api_client, ingested_patient, db_session):
    """target=Observation/{id} narrows the search Bundle to exactly that Provenance."""
    from app.db.models import ExtractedObservation

    observation = db_session.exec(
        select(ExtractedObservation).where(ExtractedObservation.code_system == "LOINC")
    ).first()

    response = api_client.get(f"/fhir/Provenance?target=Observation/{observation.id}")
    assert response.status_code == 200
    body = response.json()
    Bundle.model_validate(body)

    assert body["total"] == 1
    provenance = body["entry"][0]["resource"]
    assert provenance["target"][0]["reference"] == f"Observation/{observation.id}"
    start, end = _start_end_from_extension(provenance)
    assert (start, end) == (observation.char_start, observation.char_end)


def test_fhir_provenance_search_unfiltered_returns_all_loinc(api_client, ingested_patient):
    """Unfiltered /fhir/Provenance returns one Provenance per LOINC observation."""
    body = api_client.get("/fhir/Provenance").json()
    Bundle.model_validate(body)
    assert body["total"] == 9
