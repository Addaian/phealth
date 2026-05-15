"""drop clinical_document.embedding column

Revision ID: phase3_000_drop_embedding
Revises: a5ac5e9fdd48
Create Date: 2026-05-15 03:00:00.000000

The legacy pgvector embedding column on clinical_document was Phase 1
infrastructure for semantic retrieval that Phase 3 ended up not using —
phase_3_PRD.md §3 (Non-Goals) explicitly states "no embeddings used at
request time; retrieval is by structured query, not vector similarity, for
this MVP".

This migration also drops the project's lone OpenAI dependency, since the
embedding column was OpenAI's only role. Going forward Claude is the single
LLM provider (section-detector fallback + Phase 3 narration).

We intentionally leave the ``vector`` Postgres extension installed — it is
harmless when unused and avoids forcing every developer's dev DB to be
recreated. The initial migration's ``CREATE EXTENSION IF NOT EXISTS vector``
remains idempotent.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "phase3_000_drop_embedding"
down_revision: str | None = "a5ac5e9fdd48"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the embedding column. Idempotent: IF EXISTS guards a re-run."""
    # ``IF EXISTS`` because some dev DBs may already have been hand-cleaned.
    op.execute("ALTER TABLE clinical_document DROP COLUMN IF EXISTS embedding")


def downgrade() -> None:
    """Re-add the embedding column for symmetry with the initial migration.

    Imports ``pgvector.sqlalchemy.Vector`` lazily because the runtime dependency
    is no longer in ``pyproject.toml`` — a downgrade is a rare developer
    workflow and the dev pulls the package back in manually if needed.
    """
    from pgvector.sqlalchemy import Vector  # type: ignore[import-untyped]

    op.add_column(
        "clinical_document",
        sa.Column("embedding", Vector(1536), nullable=True),
    )
