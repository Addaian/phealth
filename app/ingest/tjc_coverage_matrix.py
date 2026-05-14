"""
TJC coverage-matrix builder.

Walks a patient's documents and emits one ``TjcCoverage`` row per Joint
Commission Element of Performance in the seed catalog, with a status of
``satisfied | gap | ambiguous`` and a pointer to the supporting (or
conspicuously absent) evidence span.

The five intentional gaps in the synthetic chart (G1-G5; see
``app/synthetic/persona.yaml`` §intentional_gaps) must each surface here as
status ``gap``.

Implemented in M6.
"""

# Coverage-matrix logic is implemented in M6.
