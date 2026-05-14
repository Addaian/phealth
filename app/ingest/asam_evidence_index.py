"""
ASAM-evidence index builder.

For each document, emits ``AsamEvidence`` rows -- one per (dimension, span) --
that anchor evidence for each of the six ASAM 4th-edition dimensions back to a
char-offset span in the source text. Driven by per-dimension keyword hooks plus
scale-result hooks (e.g. a CIWA-Ar total feeds Dimension 1).

This pre-computed index is what makes the Phase 3 ASAM endpoint a retrieval
problem rather than a full-document inference problem (PRD §1).

Implemented in M6.
"""

# Evidence-indexing logic is implemented in M6.
