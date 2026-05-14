"""
Internal model <-> FHIR R4 resource mappers.

Converts the relational rows (Patient, Encounter, ClinicalDocument,
ExtractedObservation) into canonical FHIR R4 resources using the
``fhir.resources`` pydantic models, and assembles the transaction Bundle
served by GET /patients/{id}/fhir-bundle.

Implemented in M7. See documents/phase_1_implementation_plan.md §M7 and
documents/phase_1.md §Step 7.
"""

# Mappers and Bundle assembly are added here in M7.
