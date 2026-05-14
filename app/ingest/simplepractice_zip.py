"""
SimplePractice Data Export ingester -- top-level orchestrator.

Walks a SimplePractice "Complete" Data Export directory and drives the
per-document pipeline, persisting the FHIR-shaped substrate:

    vCard contacts            -> Patient
    each chart-note PDF:
        pdf_parser            -> raw_text + header/signature metadata
        section_detector      -> doc_type + sectioned JSONB (regex-first,
                                 LLM fallback bounded by MAX_LLM_CALLS_PER_INGEST)
        scale_extractor       -> ExtractedObservation rows (the 7 scales)
        entity_tagger         -> ExtractedObservation rows (substances/meds/dx)
        asam_evidence_index   -> AsamEvidence rows (6 dimensions)
        embeddings            -> ClinicalDocument.embedding (pgvector; skipped
                                 gracefully without an OpenAI key)
    whole document set        -> tjc_coverage_matrix -> TjcCoverage rows

Idempotency (PRD §7)
--------------------
Each document is keyed on ``sha256(raw_text)``. A re-ingest of an identical
document is a no-op -- the existing ClinicalDocument (and its observations and
evidence) is left untouched. The TJC coverage matrix is rebuilt from the
patient's full current document set every run, which is idempotent because the
EP catalog is fixed.

Export-format notes (discovered during the M3 export inspection)
----------------------------------------------------------------
* The export is a directory tree, not a single ZIP. The ``/ingest`` endpoint
  unzips an upload to a temp directory and hands the path here; the directory
  layout is then walked with rglob, so this function is agnostic to how it
  arrived.
* Demographics are vCard (.vcf), not CSV -- see vcard_parser.
* Billing PDFs live under "Billing Documents/" and are ignored; only PDFs under
  a "Medical Records/" path are chart notes.

The FHIR JSON written here (``fhir_json`` / ``fhir_document_reference``) is
minimal but valid R4 -- enough to satisfy the not-null columns and prove the
substrate is FHIR-shaped. M7 replaces these builders with ``fhir.resources``-
validated resources and adds the ClinicalImpression / Observation resources and
the transaction Bundle endpoint.
"""

import hashlib
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from app.db.models import (
    AsamEvidence,
    AuditEvent,
    ClinicalDocument,
    Encounter,
    ExtractedObservation,
    Patient,
    TjcCoverage,
)
from app.ingest.asam_evidence_index import build_asam_evidence
from app.ingest.embeddings import embed_text
from app.ingest.entity_tagger import tag_entities
from app.ingest.pdf_parser import DocumentMetadata, parse_pdf
from app.ingest.scale_extractor import extract_scales
from app.ingest.section_detector import classify, llm_fallback_sections, split_sections
from app.ingest.tjc_coverage_matrix import build_tjc_coverage
from app.ingest.vcard_parser import ContactCard, parse_vcard

logger = logging.getLogger(__name__)

# Per document type: the local Encounter (type_code, class_code). class_code is
# the FHIR v3-ActCode; the synthetic patient is in medically monitored inpatient
# withdrawal management, so every encounter is inpatient (IMP).
_ENCOUNTER_KIND: dict[str, tuple[str, str]] = {
    "bps_intake": ("intake", "IMP"),
    "soap": ("progress", "IMP"),
    "dap": ("progress", "IMP"),
    "dsap": ("loc-reassessment", "IMP"),
}


def ingest_export(export_root: Path, session: Session, actor: str = "ingest-api") -> dict:
    """Ingest a SimplePractice export directory. Returns a summary dict.

    Does not commit -- the caller owns the transaction boundary (the endpoint's
    background task commits; tests roll back).
    """
    patient = _ingest_patient(export_root, session, actor)

    bundles: list[dict] = []
    ingested = 0
    skipped = 0
    for pdf_path in _medical_record_pdfs(export_root):
        bundle, was_new = _ingest_document(pdf_path, session, patient, actor)
        bundles.append(bundle)
        ingested += int(was_new)
        skipped += int(not was_new)

    admission_date = _admission_date(bundles)
    tjc_rows = _rebuild_tjc_coverage(session, patient, bundles, admission_date)

    _audit(
        session,
        actor,
        action="ingest",
        resource_type="Patient",
        resource_id=patient.id,
        payload={
            "export_root": str(export_root),
            "documents_ingested": ingested,
            "documents_skipped": skipped,
            "tjc_rows": tjc_rows,
        },
    )
    summary = {
        "patient_id": str(patient.id),
        "documents_ingested": ingested,
        "documents_skipped": skipped,
        "documents_total": len(bundles),
        "tjc_coverage_rows": tjc_rows,
    }
    logger.info("ingest complete: %s", summary)
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# Export walking
# ─────────────────────────────────────────────────────────────────────────────
def _medical_record_pdfs(export_root: Path) -> list[Path]:
    """Find chart-note PDFs: every ``*.pdf`` under a ``Medical Records`` path.

    Sorted by filename, which (SimplePractice names files ``... <date> ...``)
    happens to put them in chronological order -- a convenience, not relied on;
    timeline ordering is done from the parsed appointment date.
    """
    return sorted(path for path in export_root.rglob("*.pdf") if "Medical Records" in path.parts)


def _ingest_patient(export_root: Path, session: Session, actor: str) -> Patient:
    """Resolve the patient from the vCards and get-or-create the Patient row.

    The patient is the contact whose name matches the ``Medical Records/<name>/``
    folder; other contacts in the export (e.g. an emergency contact) are not
    patients and are skipped.
    """
    medical_pdfs = _medical_record_pdfs(export_root)
    if not medical_pdfs:
        raise ValueError(f"no medical-record PDFs found under {export_root}")
    client_folder_name = medical_pdfs[0].parent.name

    cards = [parse_vcard(vcf) for vcf in export_root.rglob("*.vcf")]
    patient_card = next(
        (card for card in cards if card.full_name == client_folder_name),
        None,
    )
    if patient_card is None:
        raise ValueError(
            f"no vCard matches the medical-records client folder {client_folder_name!r}"
        )
    return _get_or_create_patient(session, patient_card, actor)


def _get_or_create_patient(session: Session, card: ContactCard, actor: str) -> Patient:
    """Return the existing Patient (by SimplePractice external id) or create one."""
    existing = session.exec(select(Patient).where(Patient.external_id == card.external_id)).first()
    if existing is not None:
        return existing

    patient = Patient(
        external_id=card.external_id,
        given_name=card.given_name,
        family_name=card.family_name,
        birth_date=card.birth_date,
        # The SimplePractice vCard carries no gender; "unknown" is a valid FHIR
        # administrative-gender value (see vcard_parser's module docstring).
        gender="unknown",
        fhir_json={},  # populated just below, once the row has an id
    )
    session.add(patient)
    session.flush()
    patient.fhir_json = _fhir_patient(patient)
    _audit(session, actor, "ingest", "Patient", patient.id, {"external_id": card.external_id})
    return patient


# ─────────────────────────────────────────────────────────────────────────────
# Per-document ingestion
# ─────────────────────────────────────────────────────────────────────────────
def _ingest_document(
    pdf_path: Path, session: Session, patient: Patient, actor: str
) -> tuple[dict, bool]:
    """Ingest one chart-note PDF. Returns ``(bundle, was_new)``.

    ``bundle`` is the in-memory representation the TJC coverage matrix consumes
    (see tjc_coverage_matrix's contract); it is built whether the document is
    newly persisted or was already present (idempotent re-ingest).
    """
    raw_text, metadata = parse_pdf(pdf_path)
    content_hash = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    doc_type = classify(metadata.template_title)
    sections = _detect_sections(raw_text, doc_type)
    scale_observations = extract_scales(raw_text)
    entity_observations = tag_entities(raw_text)

    existing = session.exec(
        select(ClinicalDocument).where(ClinicalDocument.content_hash == content_hash)
    ).first()
    if existing is not None:
        logger.info("skipping %s -- already ingested (content hash match)", pdf_path.name)
        document_id = existing.id
        was_new = False
    else:
        document_id = _persist_document(
            session,
            patient,
            pdf_path,
            raw_text,
            content_hash,
            doc_type,
            metadata,
            sections,
            scale_observations,
            entity_observations,
            actor,
        )
        was_new = True

    bundle = {
        "doc_type": doc_type,
        "document_id": document_id,
        "raw_text": raw_text,
        "authored_on": metadata.authored_on,
        "author_name": metadata.author_name,
        "author_role": metadata.author_role,
        "sections": sections,
        "scale_observations": scale_observations,
        "entity_observations": entity_observations,
    }
    return bundle, was_new


def _detect_sections(raw_text: str, doc_type: str) -> dict[str, dict]:
    """Split into sections, regex-first, falling back to the LLM on a regex miss."""
    sections = split_sections(raw_text, doc_type)
    if sections:
        return sections
    logger.warning("regex section split found nothing for %s -- trying LLM fallback", doc_type)
    return llm_fallback_sections(raw_text, doc_type)


def _persist_document(
    session: Session,
    patient: Patient,
    pdf_path: Path,
    raw_text: str,
    content_hash: str,
    doc_type: str,
    metadata: DocumentMetadata,
    sections: dict[str, dict],
    scale_observations: list[dict],
    entity_observations: list[dict],
    actor: str,
) -> uuid.UUID:
    """Persist the Encounter, ClinicalDocument, observations, and ASAM evidence."""
    type_code, class_code = _ENCOUNTER_KIND.get(doc_type, ("progress", "IMP"))
    authored_on = metadata.authored_on or datetime.now()

    encounter = Encounter(
        patient_id=patient.id,
        period_start=authored_on,
        class_code=class_code,
        type_code=type_code,
        fhir_json={},
    )
    session.add(encounter)
    session.flush()
    encounter.fhir_json = _fhir_encounter(encounter)

    document = ClinicalDocument(
        patient_id=patient.id,
        encounter_id=encounter.id,
        document_type=doc_type,
        external_id=_external_id_from_filename(pdf_path),
        authored_on=authored_on,
        author_name=metadata.author_name,
        author_role=metadata.author_role,
        raw_text=raw_text,
        content_hash=content_hash,
        sections=sections,
        fhir_document_reference={},
        embedding=embed_text(raw_text),
    )
    session.add(document)
    session.flush()
    document.fhir_document_reference = _fhir_document_reference(document)

    for observation in (*scale_observations, *entity_observations):
        session.add(ExtractedObservation(document_id=document.id, **observation))

    for evidence in build_asam_evidence(raw_text, scale_observations):
        session.add(AsamEvidence(document_id=document.id, **evidence))

    _audit(
        session,
        actor,
        "ingest",
        "ClinicalDocument",
        document.id,
        {
            "document_type": doc_type,
            "source_file": pdf_path.name,
            "scale_observations": len(scale_observations),
            "entity_observations": len(entity_observations),
        },
    )
    return document.id


# ─────────────────────────────────────────────────────────────────────────────
# TJC coverage
# ─────────────────────────────────────────────────────────────────────────────
def _rebuild_tjc_coverage(
    session: Session, patient: Patient, bundles: list[dict], admission_date: datetime
) -> int:
    """Delete and rebuild this patient's TjcCoverage rows from the full document set.

    Delete-and-rebuild is idempotent here: the EP catalog is fixed, so the same
    document set always yields the same set of (patient_id, ep_code) rows.
    """
    for stale in session.exec(
        select(TjcCoverage).where(TjcCoverage.patient_id == patient.id)
    ).all():
        session.delete(stale)
    session.flush()

    coverage_rows = build_tjc_coverage(bundles, admission_date)
    for row in coverage_rows:
        session.add(TjcCoverage(patient_id=patient.id, **row))
    return len(coverage_rows)


def _admission_date(bundles: list[dict]) -> datetime:
    """The admission datetime: the BPS intake's authored-on, or the earliest document."""
    intake = next((b for b in bundles if b["doc_type"] == "bps_intake"), None)
    if intake is not None and intake["authored_on"] is not None:
        return intake["authored_on"]
    return min(b["authored_on"] for b in bundles if b["authored_on"] is not None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _external_id_from_filename(pdf_path: Path) -> str | None:
    """The SimplePractice document id is the trailing number in the PDF filename."""
    digits = [part for part in pdf_path.stem.split() if part.isdigit()]
    return digits[-1] if digits else None


def _audit(
    session: Session,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID,
    payload: dict,
) -> None:
    """Append an AuditEvent -- every state-changing operation is event-sourced (PRD §7)."""
    session.add(
        AuditEvent(
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
        )
    )


# --- Minimal FHIR R4 JSON builders (M7 replaces with fhir.resources-validated) ---
def _fhir_patient(patient: Patient) -> dict:
    return {
        "resourceType": "Patient",
        "id": str(patient.id),
        "identifier": [
            {"system": "https://simplepractice.com/client-id", "value": patient.external_id}
        ],
        "name": [{"family": patient.family_name, "given": [patient.given_name]}],
        "gender": patient.gender,
        "birthDate": patient.birth_date.isoformat(),
    }


def _fhir_encounter(encounter: Encounter) -> dict:
    return {
        "resourceType": "Encounter",
        "id": str(encounter.id),
        "status": "finished",
        "class": {"code": encounter.class_code},
        "type": [{"text": encounter.type_code}],
        "subject": {"reference": f"Patient/{encounter.patient_id}"},
        "period": {"start": encounter.period_start.isoformat()},
    }


def _fhir_document_reference(document: ClinicalDocument) -> dict:
    return {
        "resourceType": "DocumentReference",
        "id": str(document.id),
        "status": "current",
        "type": {"text": document.document_type},
        "subject": {"reference": f"Patient/{document.patient_id}"},
        "date": document.authored_on.isoformat(),
        "author": [{"display": document.author_name}],
        "context": {"encounter": [{"reference": f"Encounter/{document.encounter_id}"}]},
    }


def _main() -> None:
    """CLI entry point: ingest a SimplePractice export directory directly.

    Usage:  python -m app.ingest.simplepractice_zip <export_dir>
    Useful for the demo and local runs without going through the HTTP endpoint.
    """
    if len(sys.argv) != 2:
        print("usage: python -m app.ingest.simplepractice_zip <export_dir>", file=sys.stderr)
        raise SystemExit(2)

    from app.db.session import engine

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with Session(engine) as session:
        summary = ingest_export(Path(sys.argv[1]), session)
        session.commit()
    print(summary)


if __name__ == "__main__":
    _main()
