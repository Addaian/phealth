-- Enable pgvector on first database boot.
-- The pgvector/pgvector image ships the extension binary; this script makes
-- it active in the `phealth` database so M5's Alembic migration can create
-- vector columns without a manual `CREATE EXTENSION` step.
CREATE EXTENSION IF NOT EXISTS vector;
