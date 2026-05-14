# Changelog

2026-05-13 — Reviewed Phase 1 PRD; resolved Q1–Q3, defined DSAP, aligned §5.1 gap list with §6.3, added submission-contract section, flipped status Draft → Approved.
2026-05-13 — M0: wrote app/synthetic/persona.yaml (single source of truth); added generate_chart_text.py + chart_text/ paste-into-SP outputs.
2026-05-14 — M1: built SimplePractice [BPS]/[SOAP]/[DAP]/[DSAP] templates (21-section BPS). M2: authored Marcus Reyes chart, exported to data/synthetic_export/.
2026-05-14 — M4: scaffolded FastAPI app, docker-compose (Postgres 16 + pgvector), Alembic, app/ skeleton, /health endpoint — docker+alembic+pytest acceptance green, ruff clean.
2026-05-14 — M5: defined 7-table SQLModel schema (FHIR-shaped, char-offset provenance, pgvector embedding, generated tsvector FTS); first Alembic migration + DB-backed schema tests, all green.
2026-05-14 — Added persona-consistency and chart-text-generator tests ahead of M6; suite now 13 tests (gracefully skips DB-backed tests when Postgres is down).
2026-05-14 — Simplify pass on M0–M5 code: renamed single-letter variables, extracted _render_substance helper, added missing docstring + EMBEDDING_DIM cross-ref comment. Output byte-identical, all 13 tests green.
2026-05-14 — M6: built the full ingestion pipeline — pdf_parser, vcard_parser, section_detector, scale_extractor, entity_tagger, asam_evidence_index, tjc_coverage_matrix, embeddings, provenance, orchestrator + POST /ingest endpoint; ASAM + TJC seed catalogs. All M6 acceptance criteria pass against the real export (4 docs, 7 scales, 6 ASAM dims, 5/5 intentional gaps detected); idempotent re-ingest; HTTP endpoint verified through the docker stack; 15 tests green.
2026-05-14 — M6 sanity check: added explicit `bps is None` guards in tjc_coverage_matrix rules (mypy now clean across all 12 ingest modules); ruff + 15 tests still green.
