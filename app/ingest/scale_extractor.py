"""
Clinical scale extractor.

Pattern-matched extractors for the seven scales embedded verbatim in the
synthetic chart -- PHQ-9, GAD-7, AUDIT-C, DAST-10, CIWA-Ar, COWS, C-SSRS --
each emitted as an ``ExtractedObservation`` row with a LOINC code where
available and ``(char_start, char_end)`` provenance back into the source
``raw_text``.

The verbatim scale strings the regexes target are defined in
``app/synthetic/persona.yaml`` (the same source the chart text was generated
from), which keeps the golden tests in tests/test_scale_extractor.py stable.

Implemented in M6.
"""

# Per-scale extractors are implemented in M6.
