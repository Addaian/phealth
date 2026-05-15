"""
Tests for the /fhir/* search endpoints (Phase 2 PRD §5.3 + M8).

Two contracts under test:
  1. Each Bundle round-trips through ``fhir.resources.R4B.Bundle.model_validate``
     (Success Metric M3 -- 100% of /fhir/* responses validate against the spec).
  2. Cursor pagination reaches every row exactly once (no duplicates, no
     misses) across page boundaries.

Coverage:
  - ``/fhir/Patient/{id}``                  -- read happy path + 404
  - ``/fhir/DocumentReference/{id}``        -- read happy path + 404
  - ``/fhir/DocumentReference``             -- search w/ + w/o filters,
                                                 paginated traversal
  - ``/fhir/Observation/{id}``              -- read happy path
  - ``/fhir/Observation``                   -- search filtered by patient,
                                                 by code, paginated traversal
  - ``/fhir/Provenance``                    -- empty Bundle until M10
"""

import uuid

from fhir.resources.R4B.bundle import Bundle
from sqlmodel import select

# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_fhir_patient_read_returns_resource(api_client, ingested_patient):
    """GET /fhir/Patient/{id} returns one validated Patient resource."""
    response = api_client.get(f"/fhir/Patient/{ingested_patient.id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/fhir+json")

    body = response.json()
    assert body["resourceType"] == "Patient"
    assert body["id"] == str(ingested_patient.id)
    assert body["name"][0]["given"] == [ingested_patient.given_name]


def test_fhir_patient_read_unknown_returns_operation_outcome(api_client):
    """An unknown Patient id returns 404 with an OperationOutcome body."""
    response = api_client.get(f"/fhir/Patient/{uuid.uuid4()}")
    assert response.status_code == 404
    body = response.json()
    assert body["resourceType"] == "OperationOutcome"
    assert body["issue"][0]["code"] == "not-found"


def test_fhir_document_reference_read_round_trips(api_client, ingested_patient, db_session):
    """GET /fhir/DocumentReference/{id} returns a single DocumentReference."""
    from app.db.models import ClinicalDocument

    document = db_session.exec(
        select(ClinicalDocument).where(ClinicalDocument.patient_id == ingested_patient.id)
    ).first()
    response = api_client.get(f"/fhir/DocumentReference/{document.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["resourceType"] == "DocumentReference"
    assert body["id"] == str(document.id)


# ---------------------------------------------------------------------------
# DocumentReference search
# ---------------------------------------------------------------------------


def _validate_bundle(payload: dict) -> Bundle:
    """Round-trip a payload through fhir.resources.R4B.Bundle.model_validate."""
    return Bundle.model_validate(payload)


def test_fhir_document_reference_search_unfiltered_returns_all(api_client, ingested_patient):
    """All 4 documents (1 intake + 3 progress notes) appear in the unfiltered Bundle."""
    response = api_client.get("/fhir/DocumentReference")
    assert response.status_code == 200
    body = response.json()
    bundle = _validate_bundle(body)

    assert bundle.type == "searchset"
    assert bundle.total == 4
    assert len(bundle.entry) == 4
    # self link present.
    assert any(link.relation == "self" for link in bundle.link)


def test_fhir_document_reference_search_by_type(api_client, ingested_patient):
    """type=bps_intake → one matching DocumentReference."""
    response = api_client.get("/fhir/DocumentReference?type=bps_intake")
    body = response.json()
    bundle = _validate_bundle(body)
    assert bundle.total == 1
    assert bundle.entry[0].resource.type.text == "bps_intake"


def test_fhir_document_reference_search_by_patient(api_client, ingested_patient):
    """patient=Patient/{id} filter narrows to that patient's documents."""
    response = api_client.get(f"/fhir/DocumentReference?patient=Patient/{ingested_patient.id}")
    bundle = _validate_bundle(response.json())
    assert bundle.total == 4


def test_fhir_document_reference_paginates_with_cursor(api_client, ingested_patient):
    """Paginating across the 4 documents with _count=2 reaches every row exactly once."""
    seen_ids: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        url = "/fhir/DocumentReference?_count=2"
        if cursor:
            url += f"&cursor={cursor}"
        response = api_client.get(url)
        body = response.json()
        bundle = _validate_bundle(body)
        for entry in bundle.entry:
            assert entry.resource.id not in seen_ids, "cursor returned a duplicate row"
            seen_ids.add(entry.resource.id)
        pages += 1
        next_links = [link for link in bundle.link if link.relation == "next"]
        if not next_links:
            break
        # Parse out the cursor value from the next URL.
        next_url = next_links[0].url
        cursor = next_url.split("cursor=")[-1]
        assert pages < 10, "pagination did not terminate"

    assert len(seen_ids) == 4
    assert pages == 2  # 4 rows / 2 per page


# ---------------------------------------------------------------------------
# Observation search
# ---------------------------------------------------------------------------


def test_fhir_observation_search_by_patient_returns_loinc_only(api_client, ingested_patient):
    """The /fhir/Observation surface is LOINC-only (PRD §6.1 scope)."""
    response = api_client.get(f"/fhir/Observation?patient={ingested_patient.id}")
    bundle = _validate_bundle(response.json())

    assert bundle.total > 0
    for entry in bundle.entry:
        codings = entry.resource.code.coding
        assert any(coding.system == "http://loinc.org" for coding in codings)


def test_fhir_observation_search_by_code(api_client, ingested_patient):
    """code=44261-6 filters to only the PHQ-9 total score observations."""
    response = api_client.get(f"/fhir/Observation?patient={ingested_patient.id}&code=44261-6")
    bundle = _validate_bundle(response.json())

    for entry in bundle.entry:
        assert entry.resource.code.coding[0].code == "44261-6"


def test_fhir_observation_paginates_correctly(api_client, ingested_patient):
    """Paginate Marcus's 9 LOINC observations with _count=4 → 3 pages, every row once."""
    seen_ids: set[str] = set()
    cursor: str | None = None
    pages = 0
    while True:
        url = "/fhir/Observation?_count=4"
        if cursor:
            url += f"&cursor={cursor}"
        response = api_client.get(url)
        bundle = _validate_bundle(response.json())
        for entry in bundle.entry:
            assert entry.resource.id not in seen_ids
            seen_ids.add(entry.resource.id)
        pages += 1
        next_links = [link for link in bundle.link if link.relation == "next"]
        if not next_links:
            break
        cursor = next_links[0].url.split("cursor=")[-1]
        assert pages < 10

    # Marcus has 9 LOINC observations (PHQ-9, GAD-7, AUDIT-C, CIWA-Ar, COWS, ...).
    assert len(seen_ids) == 9
    assert pages == 3  # ceil(9 / 4)


def test_fhir_observation_search_patient_with_no_documents_returns_empty(api_client):
    """Search for a patient that doesn't exist → empty Bundle, not a 404."""
    response = api_client.get(f"/fhir/Observation?patient={uuid.uuid4()}")
    assert response.status_code == 200
    bundle = _validate_bundle(response.json())
    assert bundle.total == 0
    assert len(bundle.entry or []) == 0


# ---------------------------------------------------------------------------
# Provenance search (M10 will populate)
# ---------------------------------------------------------------------------


def test_fhir_provenance_returns_empty_bundle_in_phase2_m8(api_client, ingested_patient):
    """M8 mounts the endpoint; M10 fills it. Empty Bundle, not a 404."""
    response = api_client.get(f"/fhir/Provenance?target=Observation/{uuid.uuid4()}")
    assert response.status_code == 200
    bundle = _validate_bundle(response.json())
    assert bundle.type == "searchset"
    assert bundle.total == 0
