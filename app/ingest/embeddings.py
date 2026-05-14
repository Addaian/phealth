"""
Section embeddings.

Computes an embedding per document section and writes it to a pgvector column,
for semantic retrieval in Phase 3. The model is read from the
``EMBEDDING_MODEL`` setting (default ``text-embedding-3-small``) so an
open-source embedder can be substituted without code change (PRD §7).

Implemented in M6.
"""

# Embedding logic is implemented in M6.
