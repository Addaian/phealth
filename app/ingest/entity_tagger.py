"""
Clinical entity tagger.

Spots substances, medications, and diagnoses in the chart text and emits them
as ``ExtractedObservation`` rows (code systems: SNOMED for substances and
diagnoses, RxNorm for medications) with char-offset provenance.

Regex + dictionary based for the MVP; an optional scispaCy pass can be layered
in later without changing the row shape.

Implemented in M6.
"""

# Entity-tagging logic is implemented in M6.
