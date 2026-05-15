"""
Internal model -> FHIR R4 resource mappers.

Converts the relational rows into canonical FHIR resources using the
``fhir.resources`` library. We import from ``fhir.resources.R4B`` explicitly:
the installed library ships FHIR **R5** as its default, and the PRD targets
**R4**. R4B is the official maintenance release of R4 (R4 plus errata), so the
R4B models are the faithful "R4" choice here.

Each mapper builds the resource as a plain dict -- which reads exactly like
FHIR JSON -- and runs it through ``Model.model_validate``. That validates the
resource on construction *and* keeps the call sites free of deeply-nested
``Reference`` / ``CodeableConcept`` / ``HumanName`` / ... constructors. (It also
sidesteps the ``Encounter.class`` Python-keyword problem: the dict just uses
the ``"class"`` key.)

Three consumers, all using these mappers so the stored columns and the served
resources stay consistent:
  * the ingestion orchestrator (app.ingest.simplepractice_zip) calls these at
    write time to populate ``fhir_json`` / ``fhir_document_reference`` /
    ``fhir_clinical_impression`` -- the FHIR-shaped substrate;
  * the read API (app.api.fhir, app.api.patients) re-derives the same
    resources at request time for /fhir/* and /api/v1/* responses;
  * Phase 3 will consume them for cited reasoning.

Scaling note: ``to_fhir_*`` runs ``model_validate`` per row. For Marcus's
30-resource chart this is microseconds; at 10k resources/patient (real-world
scale) the pydantic validation cost dominates. Mitigation when needed is to
emit dicts directly and skip the per-row validation -- not a Phase 2 concern.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.capabilitystatement import CapabilityStatement
from fhir.resources.R4B.clinicalimpression import ClinicalImpression
from fhir.resources.R4B.documentreference import DocumentReference
from fhir.resources.R4B.encounter import Encounter as FhirEncounter
from fhir.resources.R4B.observation import Observation
from fhir.resources.R4B.patient import Patient as FhirPatient
from fhir.resources.R4B.provenance import Provenance
from fhir.resources.R4B.resource import Resource

from app.db.models import ClinicalDocument, Encounter, ExtractedObservation, Patient

# Code systems referenced by the resources.
_LOINC_SYSTEM = "http://loinc.org"
_V3_ACTCODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
_V3_DATAOPERATION = "http://terminology.hl7.org/CodeSystem/v3-DataOperation"
_SP_CLIENT_ID = "https://simplepractice.com/client-id"

# LOINC document-type bindings (PRD §6.2). Shared by:
#   * to_fhir_document_reference (here)            -- DocumentReference.type
#   * app/api/patients.py _loinc_for (/chart shape) -- intake.loinc_type / timeline[*].loinc_type
# Both surfaces emit the same {system, code, display} so a client comparing
# the FHIR DocumentReference to the canonical /chart sees consistent coding.
LOINC_DOCUMENT_TYPE: dict[str, tuple[str, str]] = {
    "bps_intake": ("11488-4", "Consultation Note"),
    "soap": ("11506-3", "Progress Note"),
    "dap": ("11506-3", "Progress Note"),
    "dsap": ("11506-3", "Progress Note"),
}

# Custom Provenance extension URL (Phase 2 PRD §5.4 / M10). Carries the
# character-offset span ``{start, end}`` for the source text underlying an
# Observation. Modelled on the FHIR R5 ``text-position-selector`` derived
# from the W3C Web Annotation spec -- back-ported here as an R4 extension
# so the same provenance shape works on both FHIR versions and we can drop
# it cleanly when fhir.resources upgrades to R5.
#
# Shape (complex extension):
#   {
#     "url": PH_OFFSET_EXT,
#     "extension": [
#       {"url": "start", "valueInteger": <char_start>},
#       {"url": "end",   "valueInteger": <char_end>}
#     ]
#   }
PH_OFFSET_EXT = "http://perspectiveshealth.ai/fhir/StructureDefinition/text-position-selector"

# The synthetic chart was authored in a Central Time SimplePractice account
# (the PDF appointment headers read "... CT"). Postgres columns are naive
# (single-clinic MVP); we attach the clinic timezone here at the FHIR
# serialization boundary so the emitted ISO offset is correct (`-05:00` in
# CDT, `-06:00` in CST) rather than mislabelling the time as UTC.
_CHART_TZ = ZoneInfo("America/Chicago")


def _fhir_datetime(value: datetime) -> str:
    """Render a datetime for a FHIR ``dateTime`` / ``instant`` field.

    Both FHIR types require a timezone offset. The synthetic chart's
    timestamps are stored naive (Postgres ``TIMESTAMP WITHOUT TIME ZONE`` --
    see app/db/models.py); a naive value is attached to the clinic timezone
    (``America/Chicago``) so the emitted ISO string carries the right offset
    rather than mislabelling the time as UTC.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=_CHART_TZ)
    return value.isoformat()


def to_fhir_patient(patient: Patient) -> FhirPatient:
    """Map a Patient row to a FHIR R4 Patient resource."""
    return FhirPatient.model_validate(
        {
            "id": str(patient.id),
            "identifier": [{"system": _SP_CLIENT_ID, "value": patient.external_id}],
            "name": [{"family": patient.family_name, "given": [patient.given_name]}],
            "gender": patient.gender,
            "birthDate": patient.birth_date.isoformat(),
        }
    )


def to_fhir_encounter(encounter: Encounter) -> FhirEncounter:
    """Map an Encounter row to a FHIR R4 Encounter resource."""
    period = {"start": _fhir_datetime(encounter.period_start)}
    if encounter.period_end is not None:
        period["end"] = _fhir_datetime(encounter.period_end)
    return FhirEncounter.model_validate(
        {
            "id": str(encounter.id),
            "status": "finished",
            "class": {"system": _V3_ACTCODE, "code": encounter.class_code},
            "type": [{"text": encounter.type_code}],
            "subject": {"reference": f"Patient/{encounter.patient_id}"},
            "period": period,
        }
    )


def to_fhir_document_reference(document: ClinicalDocument) -> DocumentReference:
    """Map a ClinicalDocument row to a FHIR R4 DocumentReference resource.

    ``type`` carries a LOINC-coded ``CodeableConcept`` (PRD §6.2) plus the
    internal ``document_type`` slug as ``text`` -- the LOINC code is what a
    FHIR client searches on; the slug helps a human reading the resource.
    Both surfaces (/fhir/* and /api/v1/* /chart) emit the same LOINC binding.
    """
    loinc_code, loinc_display = LOINC_DOCUMENT_TYPE.get(
        document.document_type, ("11526-1", "Document")
    )
    return DocumentReference.model_validate(
        {
            "id": str(document.id),
            "status": "current",
            "type": {
                "coding": [{"system": _LOINC_SYSTEM, "code": loinc_code, "display": loinc_display}],
                "text": document.document_type,
            },
            "subject": {"reference": f"Patient/{document.patient_id}"},
            "date": _fhir_datetime(document.authored_on),
            "author": [{"display": document.author_name}],
            "content": [
                {"attachment": {"contentType": "text/plain", "title": document.document_type}}
            ],
            "context": {"encounter": [{"reference": f"Encounter/{document.encounter_id}"}]},
        }
    )


def to_fhir_clinical_impression(document: ClinicalDocument) -> ClinicalImpression:
    """Map a progress-note ClinicalDocument to a FHIR R4 ClinicalImpression.

    The summary is the note's Assessment section -- every SOAP / DAP / DSAP
    format has one. The BPS intake is not a progress note and is not passed here
    (the orchestrator only builds a ClinicalImpression for soap/dap/dsap).
    """
    assessment = document.sections.get("assessment", {}).get("text", "")
    return ClinicalImpression.model_validate(
        {
            "id": str(document.id),
            "status": "completed",
            "subject": {"reference": f"Patient/{document.patient_id}"},
            "encounter": {"reference": f"Encounter/{document.encounter_id}"},
            "date": _fhir_datetime(document.authored_on),
            "summary": assessment or document.document_type,
        }
    )


def to_fhir_observation(
    observation: ExtractedObservation, patient_id: str, encounter_id: str
) -> Observation:
    """Map a LOINC-coded ExtractedObservation to a FHIR R4 Observation resource.

    ``patient_id`` / ``encounter_id`` are passed in because ExtractedObservation
    only carries ``document_id``; the orchestrator and the Bundle builder both
    know the document's patient and encounter.
    """
    resource: dict = {
        "id": str(observation.id),
        "status": "final",
        "code": {
            "coding": [
                {
                    "system": _LOINC_SYSTEM,
                    "code": observation.code,
                    "display": observation.display,
                }
            ]
        },
        "subject": {"reference": f"Patient/{patient_id}"},
        "encounter": {"reference": f"Encounter/{encounter_id}"},
    }
    if observation.value_quantity is not None:
        resource["valueQuantity"] = {"value": observation.value_quantity}
    elif observation.value_string is not None:
        resource["valueString"] = observation.value_string
    return Observation.model_validate(resource)


def to_fhir_provenance(observation: ExtractedObservation, document: ClinicalDocument) -> Provenance:
    """Build a FHIR R4 Provenance resource for a LOINC-coded Observation.

    The Provenance carries char-offset attribution back into the source
    document's ``raw_text``. ``target`` points at the Observation;
    ``entity.what`` points at the DocumentReference that supplied the text;
    ``entity.extension`` carries the custom ``text-position-selector``
    extension (see PH_OFFSET_EXT) with the ``{start, end}`` integer slots.

    Provenance.id is derived from the Observation id (``prov-{obs_id}``) so
    a client walking $everything sees a stable, dereferenceable id.

    The ``activity`` coding ``DERIVE`` from v3-DataOperation marks this as
    a derived value (Observation generated from prose text), the canonical
    Provenance.activity binding for extraction pipelines.
    """
    return Provenance.model_validate(
        {
            "id": f"prov-{observation.id}",
            "target": [{"reference": f"Observation/{observation.id}"}],
            "recorded": _fhir_datetime(document.authored_on),
            "activity": {
                "coding": [
                    {
                        "system": _V3_DATAOPERATION,
                        "code": "DERIVE",
                        "display": "derive",
                    }
                ]
            },
            "agent": [
                {
                    "type": {
                        "coding": [
                            {
                                "system": (
                                    "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
                                ),
                                "code": "performer",
                            }
                        ]
                    },
                    "who": {"display": "perspectives-health-extractor"},
                }
            ],
            "entity": [
                {
                    "role": "source",
                    "what": {"reference": f"DocumentReference/{document.id}"},
                    # Custom complex extension carrying char-offset attribution.
                    # Inner extensions use valueInteger so the offsets survive
                    # the FHIR R4 type system.
                    "extension": [
                        {
                            "url": PH_OFFSET_EXT,
                            "extension": [
                                {"url": "start", "valueInteger": observation.char_start},
                                {"url": "end", "valueInteger": observation.char_end},
                            ],
                        }
                    ],
                }
            ],
        }
    )


def build_patient_bundle(
    patient: Patient,
    encounters: list[Encounter],
    documents: list[ClinicalDocument],
    observations: list[ExtractedObservation],
) -> Bundle:
    """Assemble a FHIR R4 transaction Bundle of all of a patient's resources.

    Contains the Patient, every Encounter, a DocumentReference per chart
    document, a ClinicalImpression per progress note (not the BPS intake),
    an Observation per LOINC-coded ExtractedObservation passed in, and a
    Provenance per LOINC-coded Observation carrying the custom char-offset
    extension (M10). Each entry's ``fullUrl`` is ``ResourceType/id`` --
    unique across the Bundle because the id is a UUID and the type prefix
    disambiguates the two resources that reuse a document's id
    (DocumentReference and ClinicalImpression).
    """
    encounter_by_document = {document.id: document.encounter_id for document in documents}
    document_by_id = {document.id: document for document in documents}
    entries: list[dict] = []

    def add(resource: Resource, resource_type: str, resource_id: str) -> None:
        entries.append(
            {
                "fullUrl": f"{resource_type}/{resource_id}",
                "resource": resource.model_dump(mode="json", by_alias=True, exclude_none=True),
                "request": {"method": "POST", "url": resource_type},
            }
        )

    add(to_fhir_patient(patient), "Patient", str(patient.id))
    for encounter in encounters:
        add(to_fhir_encounter(encounter), "Encounter", str(encounter.id))
    for document in documents:
        add(to_fhir_document_reference(document), "DocumentReference", str(document.id))
        if document.document_type != "bps_intake":
            add(to_fhir_clinical_impression(document), "ClinicalImpression", str(document.id))
    for observation in observations:
        add(
            to_fhir_observation(
                observation,
                str(patient.id),
                str(encounter_by_document[observation.document_id]),
            ),
            "Observation",
            str(observation.id),
        )
    # One Provenance per LOINC-coded Observation, carrying the custom
    # text-position-selector extension. Appended last so any client walking
    # the Bundle sees the targets before their attribution.
    for observation in observations:
        if observation.code_system != "LOINC":
            continue
        provenance = to_fhir_provenance(observation, document_by_id[observation.document_id])
        add(provenance, "Provenance", f"prov-{observation.id}")

    return Bundle.model_validate({"type": "transaction", "entry": entries})


# ---------------------------------------------------------------------------
# $everything (M9) -- Patient compartment as a searchset Bundle
# ---------------------------------------------------------------------------


def build_everything_bundle(
    patient: Patient,
    encounters: list[Encounter],
    documents: list[ClinicalDocument],
    observations: list[ExtractedObservation],
) -> Bundle:
    """Assemble a Synthea-shaped Patient/$everything Bundle (FHIR R4 operation).

    The ``$everything`` operation returns "all known information related to a
    given Patient" as a searchset Bundle. We order resources by reference
    chain so any client walking the Bundle sees parents before children:
    Patient -> Encounters -> DocumentReferences -> ClinicalImpressions ->
    Observations -> Provenance.

    M10: one Provenance entry per LOINC-coded Observation, appended last,
    carrying the custom ``text-position-selector`` extension that anchors
    the observation back to a char-offset span in the source document.

    Each entry's ``search.mode = "match"`` per FHIR R4 searchset semantics.

    References:
    - $everything operation: https://hl7.org/fhir/R4/patient-operation-everything.html
    """
    encounter_by_document = {document.id: document.encounter_id for document in documents}
    document_by_id = {document.id: document for document in documents}
    entries: list[dict] = []

    def add(resource: Resource, resource_type: str, resource_id: str) -> None:
        entries.append(
            {
                "fullUrl": f"{resource_type}/{resource_id}",
                "resource": resource.model_dump(mode="json", by_alias=True, exclude_none=True),
                "search": {"mode": "match"},
            }
        )

    add(to_fhir_patient(patient), "Patient", str(patient.id))
    for encounter in encounters:
        add(to_fhir_encounter(encounter), "Encounter", str(encounter.id))
    for document in documents:
        add(to_fhir_document_reference(document), "DocumentReference", str(document.id))
    # ClinicalImpression rows come from progress notes only; emitted after
    # all DocumentReferences so a sequential reader has the parent before
    # the impression that summarises it.
    for document in documents:
        if document.document_type != "bps_intake":
            add(to_fhir_clinical_impression(document), "ClinicalImpression", str(document.id))
    for observation in observations:
        add(
            to_fhir_observation(
                observation,
                str(patient.id),
                str(encounter_by_document[observation.document_id]),
            ),
            "Observation",
            str(observation.id),
        )
    # One Provenance per LOINC-coded Observation; non-LOINC observations
    # are not surfaced as FHIR Observations and so get no Provenance here.
    for observation in observations:
        if observation.code_system != "LOINC":
            continue
        provenance = to_fhir_provenance(observation, document_by_id[observation.document_id])
        add(provenance, "Provenance", f"prov-{observation.id}")

    return Bundle.model_validate({"type": "searchset", "total": len(entries), "entry": entries})


# ---------------------------------------------------------------------------
# CapabilityStatement (M9)
# ---------------------------------------------------------------------------


# Pinned to import time so the served CapabilityStatement is byte-stable
# across requests. A fresh datetime.now(UTC) per request would (a) defeat
# the ETag/304 contract on the one auth-free endpoint and (b) revalidate
# an ~80-line static structure on every hit. ``date`` semantically means
# "when the server's capabilities were defined", which is at module load
# for a hand-built statement.
_CAPABILITY_STATEMENT_DATE = datetime.now(UTC).isoformat()


def build_capability_statement() -> CapabilityStatement:
    """Hand-build the FHIR R4 CapabilityStatement for /fhir/metadata.

    Declares the served resources (Patient, DocumentReference, Observation,
    Provenance), the supported interactions (read, search-type), the search
    parameters per resource, and the Patient/$everything + $export
    operations. Reviewers hit ``/fhir/metadata`` to learn what the server
    offers without reading the source.

    Kept hand-built rather than auto-generated: the auto-generators we tried
    (fastapi-fhir, etc.) produce statements that misclassify FastAPI's path
    operations. Hand-built keeps the statement honest and short.

    The result is functionally constant for a given build -- callers can
    treat it as such. We do not @lru_cache here because the pydantic model
    is mutable; cache stability comes from ``_CAPABILITY_STATEMENT_DATE``
    being module-scoped, so successive calls produce equal JSON.

    References:
    - CapabilityStatement: https://hl7.org/fhir/R4/capabilitystatement.html
    """
    return CapabilityStatement.model_validate(
        {
            "status": "active",
            "date": _CAPABILITY_STATEMENT_DATE,
            "kind": "instance",
            "software": {"name": "phealth", "version": "0.1.0"},
            "implementation": {
                "description": (
                    "Perspectives Health clinical ingestion substrate -- "
                    "Phase 2 dual-surface API (snake_case + strict FHIR R4)."
                ),
                "url": "/fhir",
            },
            "fhirVersion": "4.0.1",
            "format": ["json"],
            "rest": [
                {
                    "mode": "server",
                    "resource": [
                        {
                            "type": "Patient",
                            "interaction": [{"code": "read"}],
                            "operation": [
                                {
                                    "name": "everything",
                                    "definition": (
                                        "http://hl7.org/fhir/OperationDefinition/Patient-everything"
                                    ),
                                },
                                {
                                    "name": "export",
                                    "definition": (
                                        # FHIR Bulk Data IG patient-level export.
                                        # MVP ships the synchronous NDJSON variant;
                                        # production would add kick-off + status.
                                        "http://hl7.org/fhir/uv/bulkdata/OperationDefinition/patient-export"
                                    ),
                                },
                            ],
                        },
                        {
                            "type": "DocumentReference",
                            "interaction": [{"code": "read"}, {"code": "search-type"}],
                            "searchParam": [
                                {"name": "patient", "type": "reference"},
                                {"name": "type", "type": "token"},
                                {"name": "_count", "type": "number"},
                            ],
                        },
                        {
                            "type": "Observation",
                            "interaction": [{"code": "read"}, {"code": "search-type"}],
                            "searchParam": [
                                {"name": "patient", "type": "reference"},
                                {"name": "code", "type": "token"},
                                {"name": "_count", "type": "number"},
                            ],
                        },
                        {
                            "type": "Provenance",
                            "interaction": [{"code": "search-type"}],
                            "searchParam": [{"name": "target", "type": "reference"}],
                        },
                    ],
                }
            ],
        }
    )
