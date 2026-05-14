"""
vCard contact parsing.

SimplePractice's Data Export ships client demographics as vCard (``.vcf``)
files under ``Contacts/`` -- *not* CSV, as the original research playbook
assumed. This was a genuine discovery during the M3 export inspection and is a
small example of the "you can't just wrap the API" reality: the export format
is whatever the vendor chose, and the ingester adapts.

The vCards SimplePractice produces are minimal -- name, address, and
*sometimes* a birth date or phone. Notably absent: gender, race/ethnicity,
language, insurance. The Patient row therefore gets what the vCard provides and
leaves the rest unknown; the richer demographics in the persona never make it
into the export. ``Patient.gender`` falls back to the FHIR-valid value
``"unknown"``.

The format is line-oriented (``KEY[;PARAMS]:VALUE``), so it is parsed directly
rather than pulling in a vCard dependency.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# The SimplePractice client id is the trailing number in the vCard filename,
# e.g. "Marcus Reyes - 106915126.vcf".
_EXTERNAL_ID = re.compile(r"-\s*(\d+)\.vcf$", re.IGNORECASE)


@dataclass
class ContactCard:
    """A parsed vCard. Fields absent from the vCard are left as None."""

    external_id: str  # SimplePractice client id, from the filename
    given_name: str
    family_name: str
    full_name: str
    birth_date: date | None
    address: str | None
    phone: str | None


def parse_vcard(vcf_path: Path) -> ContactCard:
    """Parse a single ``.vcf`` file into a :class:`ContactCard`."""
    fields = _read_vcard_fields(vcf_path)

    # N is "family;given;additional;prefix;suffix"; FN is the display name.
    name_parts = fields.get("N", "").split(";")
    family_name = name_parts[0].strip() if len(name_parts) > 0 else ""
    given_name = name_parts[1].strip() if len(name_parts) > 1 else ""

    external_id_match = _EXTERNAL_ID.search(vcf_path.name)

    return ContactCard(
        external_id=external_id_match.group(1) if external_id_match else vcf_path.stem,
        given_name=given_name,
        family_name=family_name,
        full_name=fields.get("FN", "").strip(),
        birth_date=_parse_bday(fields.get("BDAY")),
        address=_parse_address(fields.get("ADR")),
        phone=fields.get("TEL"),
    )


def _read_vcard_fields(vcf_path: Path) -> dict[str, str]:
    """Read a vCard into a ``{KEY: VALUE}`` dict.

    The key is taken before any parameters (``TEL;TYPE=mobile`` -> ``TEL``); the
    last occurrence of a repeated key wins, which is fine for these minimal cards.
    """
    fields: dict[str, str] = {}
    for line in vcf_path.read_text(encoding="utf-8").splitlines():
        if ":" not in line:
            continue
        raw_key, value = line.split(":", 1)
        key = raw_key.split(";", 1)[0].strip().upper()
        if key in ("BEGIN", "END", "VERSION"):
            continue
        fields[key] = value.strip()
    return fields


def _parse_bday(value: str | None) -> date | None:
    """Parse a vCard ``BDAY`` value (SimplePractice emits the ``YYYYMMDD`` form)."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%d").date()
    except ValueError:
        return None


def _parse_address(value: str | None) -> str | None:
    """Flatten a vCard ``ADR`` value into a single human-readable line.

    ADR is ``po-box;extended;street;city;region;postal;country``; vCard escapes
    commas as ``\\,`` which we unescape.
    """
    if not value:
        return None
    parts = [part.replace("\\,", ",").strip() for part in value.split(";")]
    # Keep street, city, region, postal (indices 2..5); drop the empty fields.
    meaningful = [part for part in parts[2:6] if part]
    return ", ".join(meaningful) if meaningful else None
