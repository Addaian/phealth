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
        openai_api_key -- only needed when the ingest pipeline falls back to
                          an LLM for section detection. Absent is fine for the
                          regex-only happy path.
        embedding_model -- embedding model id; swap for an open model in a
                          deployment that cannot egress to OpenAI (PRD §7).
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
    openai_api_key: str | None = None
    embedding_model: str = "text-embedding-3-small"
    max_llm_calls_per_ingest: int = 5


@lru_cache
def get_settings() -> Settings:
    """Return the cached :class:`Settings` singleton.

    Wrapped in ``lru_cache`` so the ``.env`` file is read once per process.
    FastAPI dependencies and module-level code should call this rather than
    constructing ``Settings()`` directly.
    """
    return Settings()
