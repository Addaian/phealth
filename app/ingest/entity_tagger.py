"""
Clinical entity tagging.

Spots three kinds of clinical entity in a document's ``raw_text`` and emits each
as an ExtractedObservation-shaped dict (the orchestrator attaches
``document_id``):

  * **Diagnoses** -- captured wherever the chart writes a diagnosis name
    immediately followed by its ICD-10 code, e.g. "Alcohol Use Disorder, severe,
    with withdrawal (F10.232; ...)". Because the chart carries the real ICD-10
    code inline, the tagger uses it directly: ``code_system`` "ICD-10".
  * **Substances** and **medications** -- dictionary-based. The chart does not
    carry SNOMED/RxNorm codes, so these are emitted with ``code_system``
    "internal" and a normalized term (``substance:alcohol``,
    ``medication:gabapentin``). One row per distinct entity per document,
    anchored to its first mention -- enough structured signal for the ASAM
    indexer without a row for every textual repeat of "alcohol".

Regex + dictionary based, per the implementation plan. An optional scispaCy NER
pass could be layered in later without changing the emitted row shape.
"""

import re

# A diagnosis name immediately followed by its ICD-10 code, as the chart writes
# them: "<Name> (F10.232; ...)" or "<Name> (ICD-10 F33.1; ...)". The name char
# class excludes ':' ';' '(' ')' so the capture cannot run backwards across a
# "Label:: " prefix or a preceding parenthetical.
_DIAGNOSIS = re.compile(
    r"(?P<name>[A-Z][A-Za-z0-9 ,/'-]{2,79}?)\s*\((?:ICD-10\s+)?(?P<code>F\d{2}\.\d+)"
)

# Substance dictionary: normalized name -> recogniser. Kept at the specific
# level the persona tracks (alcohol, alprazolam, cannabis, opioids).
SUBSTANCE_TERMS: dict[str, re.Pattern[str]] = {
    "alcohol": re.compile(r"\b(?:alcohol|ethanol|ETOH)\b", re.IGNORECASE),
    "alprazolam": re.compile(r"\b(?:alprazolam|xanax)\b", re.IGNORECASE),
    "cannabis": re.compile(r"\b(?:cannabis|cannabinoids?|marijuana)\b", re.IGNORECASE),
    "opioid": re.compile(r"\bopioids?\b", re.IGNORECASE),
}

# Medication dictionary: normalized name -> recogniser. Covers the medications
# named across the synthetic chart (current, taper, and prior-treatment).
MEDICATION_TERMS: dict[str, re.Pattern[str]] = {
    "lisinopril": re.compile(r"\blisinopril\b", re.IGNORECASE),
    "chlordiazepoxide": re.compile(r"\b(?:chlordiazepoxide|librium)\b", re.IGNORECASE),
    "gabapentin": re.compile(r"\bgabapentin\b", re.IGNORECASE),
    "thiamine": re.compile(r"\bthiamine\b", re.IGNORECASE),
    "naltrexone": re.compile(r"\bnaltrexone\b", re.IGNORECASE),
    "sertraline": re.compile(r"\bsertraline\b", re.IGNORECASE),
}


def tag_entities(raw_text: str) -> list[dict]:
    """Tag diagnoses, substances, and medications in one document's ``raw_text``.

    Returns ExtractedObservation-shaped dicts (without ``document_id``), sorted
    by position.
    """
    entities = (
        _tag_diagnoses(raw_text)
        + _tag_dictionary(raw_text, SUBSTANCE_TERMS, "substance")
        + _tag_dictionary(raw_text, MEDICATION_TERMS, "medication")
    )
    entities.sort(key=lambda entity: entity["char_start"])
    return entities


def _tag_diagnoses(raw_text: str) -> list[dict]:
    """Emit one row per inline-coded diagnosis mention.

    Every occurrence is kept (a diagnosis documented in both §17 Past Psych and
    §20 Diagnosis yields two rows) -- each has its own provenance span, which is
    exactly the signal a golden-thread / cross-document consistency check wants.
    """
    observations: list[dict] = []
    for match in _DIAGNOSIS.finditer(raw_text):
        observations.append(
            {
                "code_system": "ICD-10",
                "code": match.group("code"),
                "display": " ".join(match.group("name").split()),
                "value_quantity": None,
                "value_string": None,
                "char_start": match.start(),
                "char_end": match.end(),
                "extraction_method": "regex",
                "confidence": 1.0,  # anchored on a real ICD-10 code -- high precision
            }
        )
    return observations


def _tag_dictionary(raw_text: str, terms: dict[str, re.Pattern[str]], kind: str) -> list[dict]:
    """Emit one row per distinct dictionary term found, anchored to its first mention."""
    observations: list[dict] = []
    for normalized, pattern in terms.items():
        match = pattern.search(raw_text)
        if match is None:
            continue
        observations.append(
            {
                "code_system": "internal",
                "code": f"{kind}:{normalized}",
                "display": f"{normalized.capitalize()} ({kind})",
                "value_quantity": None,
                "value_string": match.group(0),
                "char_start": match.start(),
                "char_end": match.end(),
                "extraction_method": "regex",
                "confidence": 0.9,  # dictionary term match -- reliable, but less structured
            }
        )
    return observations
