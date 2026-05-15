"""
Compute the ``evidence_hash`` cache key for an assessment.

phase_3_PRD.md §5.7 defines this as ``xxhash64`` of the canonical
contributing-row PK set:

  * **ASAM**: every AsamEvidence row for the patient's documents, plus
    every ExtractedObservation row for the same documents (the scale
    extractions the rule engine consumes).
  * **TJC**: every TjcCoverage row for the patient, plus every
    ExtractedObservation + ClinicalDocument row for the same patient
    (TJC predicates read all three).

The hash is the cache key for ``(patient_id, evidence_hash,
model_version)``. A re-ingest changes one of the contributing rows ->
the hash changes -> the cache misses -> the assessment recomputes.

Same patient, same data, same model -> same hash -> cached body byte-stable.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import xxhash
from sqlmodel import Session, select

from app.db.models import (
    AsamEvidence,
    ClinicalDocument,
    ExtractedObservation,
    TjcCoverage,
)

Kind = Literal["asam", "tjc"]


def compute_evidence_hash(patient_id: UUID, db: Session, kind: Kind) -> str:
    """Return the canonical xxhash64 hex over the contributing rows.

    Returns an empty string when the patient has no contributing rows
    (the caller treats that as "no evidence" and returns 422).
    """
    # Patient's documents are the join key for both reasoning kinds.
    doc_ids = sorted(
        db.exec(select(ClinicalDocument.id).where(ClinicalDocument.patient_id == patient_id))
    )
    if not doc_ids:
        return ""

    pk_tokens: list[str] = []

    # ExtractedObservation rows are common to both kinds (rule engine
    # in ASAM, raw-row predicates in TJC).
    obs_pks = sorted(
        db.exec(
            select(ExtractedObservation.id).where(
                ExtractedObservation.document_id.in_(doc_ids)  # type: ignore[attr-defined]
            )
        )
    )
    pk_tokens.extend(f"obs:{pk}" for pk in obs_pks)

    if kind == "asam":
        # AsamEvidence has a composite PK (document_id, dimension, char_start)
        # so we serialize all three coordinates.
        evidence_rows = db.exec(
            select(AsamEvidence).where(
                AsamEvidence.document_id.in_(doc_ids)  # type: ignore[attr-defined]
            )
        )
        evidence_tokens = sorted(
            f"asam:{row.document_id}:{row.dimension}:{row.char_start}" for row in evidence_rows
        )
        pk_tokens.extend(evidence_tokens)
    else:
        # TJC: include the TjcCoverage rows (composite PK patient_id +
        # ep_code) and the document PKs themselves (the audit predicates
        # read ClinicalDocument metadata for several EPs).
        coverage_rows = db.exec(select(TjcCoverage).where(TjcCoverage.patient_id == patient_id))
        coverage_tokens = sorted(f"tjc:{row.ep_code}:{row.status}" for row in coverage_rows)
        pk_tokens.extend(coverage_tokens)
        pk_tokens.extend(f"doc:{pk}" for pk in doc_ids)

    if not pk_tokens:
        return ""

    # Join with a delimiter the row tokens can't contain so the canonical
    # serialization is unambiguous, then hex-digest.
    canonical = "\n".join(pk_tokens).encode("utf-8")
    return xxhash.xxh64(canonical).hexdigest()


__all__ = ["compute_evidence_hash", "Kind"]
