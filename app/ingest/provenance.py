"""
Char-offset provenance bookkeeping.

Helper for mapping character offsets through the text-cleaning steps the
pipeline applies (whitespace normalization, de-hyphenation, etc.), so that
every extracted value can be re-located in the *original* ``raw_text``.

The round-trip guarantee this enables -- ``raw_text[char_start:char_end]``
matches the recorded snippet for every extracted row -- is asserted by
tests/test_provenance.py (PRD §5.5).

Implemented in M6.
"""

# Offset-mapping helpers are implemented in M6.
