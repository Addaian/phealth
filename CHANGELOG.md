# Changelog

2026-05-13 — Reviewed Phase 1 PRD; resolved Q1–Q3, defined DSAP, aligned §5.1 gap list with §6.3, added submission-contract section, flipped status Draft → Approved.
2026-05-13 — M0: wrote app/synthetic/persona.yaml (single source of truth); added generate_chart_text.py + chart_text/ paste-into-SP outputs.
2026-05-14 — M1: built SimplePractice [BPS]/[SOAP]/[DAP]/[DSAP] templates (21-section BPS). M2: authored Marcus Reyes chart, exported to data/synthetic_export/.
2026-05-14 — M4: scaffolded FastAPI app, docker-compose (Postgres 16 + pgvector), Alembic, app/ skeleton, /health endpoint — docker+alembic+pytest acceptance green, ruff clean.
2026-05-14 — M5: defined 7-table SQLModel schema (FHIR-shaped, char-offset provenance, pgvector embedding, generated tsvector FTS); first Alembic migration + DB-backed schema tests, all green.
