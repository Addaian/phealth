"""
Application configuration.

Loads settings from environment variables (and a local ``.env`` file) using
pydantic-settings. The app fails fast on startup if a required setting is
missing — see :class:`Settings` for which are required.

This module is the single source of truth for configuration; no other module
should read ``os.environ`` directly.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, populated from the environment / ``.env``.

    Required (startup raises ``ValidationError`` if absent):
        database_url   -- SQLAlchemy URL for the Postgres instance.
        ingest_api_key -- shared secret guarding the POST /ingest/* endpoints.

    Optional:
        anthropic_api_key -- API key for the Claude SDK. Powers both the
                          section-detector LLM fallback (Phase 1) and the
                          Phase 3 narration layer on /asam-loc and /tjc-audit.
                          Absent is fine for the regex-only happy path on a
                          well-formed chart; Phase 3 endpoints return 503 +
                          a rule-engine-only body when missing
                          (phase_3_PRD.md §5.9).
        phealth_llm_model -- Claude model id; defaults to Sonnet 4.6. Swap to
                          ``claude-opus-4-7`` for narration-quality fallback
                          (phase_3_PRD.md §5.6). Logged on every
                          AsamAssessment / TjcAuditResult row so a model swap
                          creates a new cache namespace (§5.7).
        max_llm_calls_per_ingest -- hard cap on LLM calls per ingest job, so a
                          malformed document cannot run up an unbounded bill.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    ingest_api_key: str
    max_llm_calls_per_ingest: int = 5
    # Claude is the single LLM provider for this project — used by the section
    # detector's regex-miss fallback (Phase 1) and the cited-rationale
    # narration on /asam-loc + /tjc-audit (Phase 3). Plain ``str | None``
    # (not SecretStr) for consistency with the rest of the settings; never
    # log the value — the LlmInvocation table records tokens and latency,
    # never the key itself.
    anthropic_api_key: str | None = None
    phealth_llm_model: str = "claude-sonnet-4-6"


@lru_cache
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton.

    Wrapped in ``lru_cache`` so the ``.env`` file is read once per process.
    FastAPI dependencies and module-level code should call this rather than
    constructing ``Settings()`` directly.
    """
    return Settings()
