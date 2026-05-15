"""phase 3 clinical assessments — cache + observability tables

Revision ID: phase3_001_clinical_assessments
Revises: phase3_000_drop_embedding
Create Date: 2026-05-15 02:47:32.387807

Creates the three tables documented in documents/phase_3_PRD.md §6.3:

  * ``asam_assessment``     -- cache for POST /api/v1/patients/{id}/asam-loc.
  * ``tjc_audit_result``    -- cache for POST /api/v1/patients/{id}/tjc-audit.
  * ``llm_invocation``      -- per-call Claude cost/latency/reliability log.

The two cache tables both carry a UNIQUE constraint on
``(patient_id, evidence_hash, model_version)`` — this is the cache key
(phase_3_PRD.md §5.7). A duplicate insert raises ``IntegrityError`` and the
API code is expected to fall back to a SELECT.

Schema baseline starts from autogenerate against the SQLModel definitions
in ``app/db/models.py``; the autogen-flagged ``clinical_document.fts`` drop
was a false positive (the FTS column is a Postgres GENERATED column added
by the initial migration in raw SQL and intentionally absent from the
SQLModel schema) and has been removed by hand.
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "phase3_001_clinical_assessments"
down_revision: str | None = "phase3_000_drop_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the three Phase 3 tables, their indexes, and unique constraints."""

    # AsamAssessment — cache row for one ASAM Level-of-Care assessment.
    op.create_table(
        "asam_assessment",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("recommended_level", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("modifiers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dimensions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rules_fired", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("confidence", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("rationale_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("rationale_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "patient_id",
            "evidence_hash",
            "model_version",
            name="uq_asam_assessment_cache_key",
        ),
    )
    op.create_index(
        op.f("ix_asam_assessment_patient_id"),
        "asam_assessment",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_asam_assessment_evidence_hash"),
        "asam_assessment",
        ["evidence_hash"],
        unique=False,
    )

    # TjcAuditResult — cache row for one TJC compliance audit.
    op.create_table(
        "tjc_audit_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("findings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rationale_status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("rationale_warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model_version", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("computed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patient.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "patient_id",
            "evidence_hash",
            "model_version",
            name="uq_tjc_audit_cache_key",
        ),
    )
    op.create_index(
        op.f("ix_tjc_audit_result_patient_id"),
        "tjc_audit_result",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_tjc_audit_result_evidence_hash"),
        "tjc_audit_result",
        ["evidence_hash"],
        unique=False,
    )

    # LlmInvocation — append-only Claude call log. No FK to patient: this
    # table outlives the patient row in a "patient row deleted, audit retained"
    # scenario. The patient_id column stays for query convenience.
    op.create_table(
        "llm_invocation",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=False),
        sa.Column("model", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("response_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("citation_validation_passed", sa.Boolean(), nullable=False),
        sa.Column("error_class", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_llm_invocation_patient_id"),
        "llm_invocation",
        ["patient_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the three Phase 3 tables in reverse order."""

    op.drop_index(op.f("ix_llm_invocation_patient_id"), table_name="llm_invocation")
    op.drop_table("llm_invocation")
    op.drop_index(op.f("ix_tjc_audit_result_evidence_hash"), table_name="tjc_audit_result")
    op.drop_index(op.f("ix_tjc_audit_result_patient_id"), table_name="tjc_audit_result")
    op.drop_table("tjc_audit_result")
    op.drop_index(op.f("ix_asam_assessment_evidence_hash"), table_name="asam_assessment")
    op.drop_index(op.f("ix_asam_assessment_patient_id"), table_name="asam_assessment")
    op.drop_table("asam_assessment")
