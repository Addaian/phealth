# CLAUDE.md — Project Conventions for Claude Code

This file captures durable, project-specific instructions. Follow them on every task in this repo unless the user explicitly overrides them in a given turn.

---

## Code style

### Comments
- **Write code with comments.** This repo is intended to be readable by someone who has never seen it before.
- Every module should have a short top-of-file docstring describing what it does and how it fits into the pipeline.
- Every non-trivial function should have a docstring explaining purpose, inputs, outputs, and any non-obvious behavior.
- Inline comments are encouraged where they clarify *why* a piece of logic exists (clinical rule references, regex intent, FHIR mapping rationale). Avoid restating *what* the code obviously does.
- Reference external sources (ASAM dimensions, TJC EP codes, LOINC codes) by name in comments so a reader can look them up.

> Note: this overrides Claude Code's general "default to no comments" guidance — this project explicitly wants commented, teachable code.

### Formatting and naming
- Format code correctly before committing. Python: `ruff format` + `ruff check --fix`. Run these as part of the workflow, not as an afterthought.
- Use clear, descriptive names. Prefer `extracted_observation` over `obs`, `clinical_document` over `doc`, `char_start` over `cs`.
- Module names: `snake_case`. Class names: `PascalCase`. Constants: `UPPER_SNAKE_CASE`.
- Function names should read like verbs (`extract_scales`, `build_asam_evidence_index`). Variable names should read like nouns.
- Avoid one-letter variables except in tight loop indices.
- Keep functions focused. If a function does more than one thing, split it.

---

## Scope and architecture

- **This project is an MVP.** Optimize for clarity, correctness, and a working end-to-end demo over feature completeness.
- **Build it so it could scale.** Don't paint into corners: prefer designs that would survive a second patient, a second EMR, or a second note format without a rewrite. Examples already baked in: unified `ClinicalDocument` table with format-specific JSONB sections; FHIR-shaped `*_json` columns on every relational row; pre-computed evidence tables consumed by Phase 3 endpoints.
- Don't over-engineer. If a hypothetical future requirement isn't on the PRD, don't build for it. "Could scale" means "doesn't block scaling," not "ships at scale today."

---

## Phase gates

- The PRD (`documents/phase_1_PRD.md`) and implementation plan (`documents/phase_1_implementation_plan.md`) define the phases.
- **When a phase finishes, sanity-check the code before declaring it done.** Sanity check means at minimum:
  1. `ruff check` and `ruff format --check` clean.
  2. `pytest -q` green.
  3. `docker compose up` succeeds and `/health` responds.
  4. Manual hit of the new endpoints with `curl` or `/docs` to confirm shape.
  5. Re-read the phase's PRD acceptance criteria and confirm each one is met.
- If any sanity check fails, fix it before marking the phase complete.

---

## Changelog

- Maintain a `CHANGELOG.md` at the repo root.
- **At the end of every session, append a one-line entry** describing what changed in that session.
- Format: `YYYY-MM-DD — <one-line summary>`. Multiple lines per day are fine; keep each line basic and concrete.
- Examples:
  - `2026-05-11 — Drafted Phase 1 PRD and implementation plan in /documents.`
  - `2026-05-12 — Scaffolded FastAPI app, docker-compose with Postgres+pgvector, initial Alembic migration.`
  - `2026-05-13 — Implemented scale_extractor for PHQ-9, GAD-7, AUDIT-C; golden tests passing.`
- If the changelog doesn't exist yet, create it at the start of the next session that produces a change.

> If you want this automated rather than convention-based, it can be wired as a Claude Code Stop hook in `.claude/settings.json` — say the word and I'll set it up.

---

## Reference documents
- `documents/Technical task 5-8.pdf` — the original assessment brief.
- `documents/phase_1.md` — research playbook (background, citations, rationale).
- `documents/phase_1_PRD.md` — Phase 1 requirements, success metrics, non-goals.
- `documents/phase_1_implementation_plan.md` — milestones M0–M10, file-level scope, risks.
