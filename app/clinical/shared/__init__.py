"""
Shared helpers used by both the ASAM and TJC reasoning paths.

  * ``evidence_retrieval`` -- builds Anthropic Citations-API document
                               blocks from any citation-shaped input (M7).
  * ``evidence_hash``      -- xxhash64 cache-key over the contributing
                               row set (M10).
  * ``response_builder``   -- assembles the canonical API response shapes
                               (M8 / M9).
"""
