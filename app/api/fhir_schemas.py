"""
FHIR-aligned Pydantic schemas for the /fhir/* read API.

Distinct from ``app/api/schemas.py`` (the snake_case ergonomic models). These
serialise to camelCase by alias so the wire shape matches FHIR R4 verbatim
without manual field-by-field translation.

The actual FHIR resource models (Patient, Observation, Bundle, ...) come from
``fhir.resources.R4B`` and are built by ``app.fhir.mappers``; this module is
reserved for *thin* response envelopes around those resources -- e.g. a
search-result Bundle wrapper with our cursor pagination links. M8 fills it
out as the search endpoints land; M5 just stakes out the module and the
shared ``model_config`` so the FHIR-isms have a single home.

References:
- pydantic alias_generator: https://docs.pydantic.dev/latest/concepts/alias/
- FHIR R4B spec: https://hl7.org/fhir/R4B/
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class FhirBaseModel(BaseModel):
    """Pydantic base for any custom envelope on the /fhir/* surface.

    ``alias_generator=to_camel`` makes snake_case Python attributes serialise
    to camelCase on the wire; ``populate_by_name=True`` lets test code still
    construct the model with snake_case keyword args; ``serialize_by_alias=True``
    ensures ``model_dump(mode="json")`` emits the camelCase form by default.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
    )
