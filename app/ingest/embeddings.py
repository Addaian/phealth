"""
Document embeddings.

Computes a vector embedding for a document's text and hands it to the
orchestrator, which writes it to the pgvector ``ClinicalDocument.embedding``
column for semantic retrieval in Phase 3.

The model is read from the ``EMBEDDING_MODEL`` setting (default
``text-embedding-3-small``) so an open-source embedder can be substituted in a
deployment that cannot egress to OpenAI (PRD §7).

Graceful degradation
--------------------
If ``OPENAI_API_KEY`` is unset, embedding is skipped: :func:`embed_text` returns
``None``, the orchestrator leaves ``ClinicalDocument.embedding`` NULL, and the
pipeline continues. The Phase 1 acceptance criteria do not require embeddings --
they are Phase 3 retrieval infrastructure -- so ingestion must run end-to-end
without a key.

The synthetic chart's documents are a few thousand tokens each, well within the
embedding model's input limit, so no chunking/truncation is needed here.
"""

import logging

from app.core.config import get_settings
from app.db.models import EMBEDDING_DIM

logger = logging.getLogger(__name__)


def embed_text(text: str) -> list[float] | None:
    """Return an embedding vector for ``text``, or ``None`` if unavailable.

    Returns ``None`` (with a warning) when ``OPENAI_API_KEY`` is unset or when
    the configured model returns an unexpected dimension -- in both cases the
    caller leaves the embedding column NULL and ingestion proceeds.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        logger.warning(
            "OPENAI_API_KEY unset -- skipping embedding (ClinicalDocument.embedding "
            "will be NULL). Phase 3 semantic retrieval needs this; Phase 1 ingestion does not."
        )
        return None

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.embeddings.create(model=settings.embedding_model, input=text)
    vector = response.data[0].embedding

    if len(vector) != EMBEDDING_DIM:
        logger.warning(
            "embedding model %s returned dimension %d, expected %d -- skipping embedding",
            settings.embedding_model,
            len(vector),
            EMBEDDING_DIM,
        )
        return None
    return vector
