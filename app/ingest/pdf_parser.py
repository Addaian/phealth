"""
PDF text extraction and SimplePractice header parsing.

Turns a SimplePractice chart-note PDF into two things:

  * ``raw_text`` -- the clean document text: the pdfplumber extraction with the
    repeating page-break artifacts and the trailing signature block removed.
    This is the canonical string every downstream extractor indexes into for
    char-offset provenance (see app/ingest/provenance.py).
  * :class:`DocumentMetadata` -- the structured header / signature fields:
    client name and DOB, the appointment date/time, the bracketed template
    title (the format dispatcher used by section_detector), the account
    "Provider", and the actual signing author plus their credential.

SimplePractice PDF quirks this module handles (all observed in the M3 export)
-----------------------------------------------------------------------------
* **Page-break artifacts.** Multi-page notes repeat a ``Client: / DOB:`` header
  at the top of pages 2+ and stamp a ``Created on ... Page N of M`` +
  ``(Un)locked by ...`` footer at the bottom of every page. Both are injected
  mid-content, so a section spanning a page break would otherwise be polluted.
  Stripped here so ``raw_text`` reads as one continuous document.
* **The "Provider:" header line is the account owner, not the author.** The
  clinician who actually wrote the note is in the signature block
  (``Signed by <name>`` followed by a credential line). The top ``Provider:``
  line is whoever owns the SimplePractice account.
* **Unsigned documents have no signature block.** The BPS intake, after being
  unlocked to correct appointment dates, exports without a ``Signed by`` block
  at all — so the author falls back to the ``Provider:`` header.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pdfplumber

# A page-break artifact: the footer SimplePractice stamps at the bottom of
# every page, plus the "Client: / DOB:" header it repeats at the top of the
# next page. The trailing header repeat is optional because the final page has
# a footer with nothing after it.
_PAGE_ARTIFACT = re.compile(
    r"Created on [^\n]+? Page \d+ of \d+\n"
    r"(?:Unlocked|Locked and Signed) by [^\n]+?(?:\n|$)"
    r"(?:Client: [^\n]+?\nDOB: [^\n]+?\n)?"
)

# The signature block of a *signed* progress note begins with a line that is
# exactly "Provider" (distinct from the header's "Provider: <name>").
_SIGNATURE_MARKER = "\nProvider\n"

# Header-line patterns.
_TEMPLATE_TITLE = re.compile(r"^\[[A-Za-z]+\][^\n]*", re.MULTILINE)
_DOB = re.compile(r"^DOB:\s*(\d{1,2}/\d{1,2}/\d{4})", re.MULTILINE)
_CLIENT = re.compile(r"^Client:\s*(.+)$", re.MULTILINE)
_PROVIDER = re.compile(r"^Provider:\s*(.+)$", re.MULTILINE)
_APPOINTMENT_DATE = re.compile(r"^Appointment:.*?\bon\s+([A-Za-z]+ \d{1,2}, \d{4})", re.MULTILINE)
_APPOINTMENT_TIME = re.compile(r"^(\d{1,2}:\d{2}\s*[ap]m)\b", re.MULTILINE | re.IGNORECASE)

# Signature block: "Signed by <name>" then the credential on the next line.
_SIGNED_BY = re.compile(r"^Signed by (.+)$\n(.+)$", re.MULTILINE)


@dataclass
class DocumentMetadata:
    """Structured fields parsed from a SimplePractice chart-note PDF."""

    client_name: str
    client_dob: date | None
    template_title: str  # e.g. "[BPS] Intake Assessment" — section_detector dispatches on this
    appointment_date: date | None
    authored_on: datetime | None  # appointment date + start time
    author_name: str  # the signing clinician, or the account Provider if unsigned
    author_role: str  # the credential line, or "" if the document is unsigned
    account_provider: str  # the SimplePractice account owner (the "Provider:" header line)
    is_signed: bool


def parse_pdf(pdf_path: Path) -> tuple[str, DocumentMetadata]:
    """Extract a SimplePractice PDF into ``(raw_text, metadata)``.

    ``raw_text`` is the cleaned, canonical document text; ``metadata`` carries
    the header and signature fields. Raises ValueError if the PDF yields no
    text layer (an image-only export would need an OCR path, which Phase 1
    deliberately does not build — see the playbook's staged decision points).
    """
    extracted = _extract_text(pdf_path)
    if not extracted.strip():
        raise ValueError(
            f"{pdf_path.name}: no extractable text layer "
            "(image-only PDF — OCR is out of scope for Phase 1)"
        )

    # 1. Remove the page-break artifacts so the body reads continuously.
    without_artifacts = _PAGE_ARTIFACT.sub("", extracted)

    # 2. Split off the trailing signature block (if the document is signed).
    if _SIGNATURE_MARKER in without_artifacts:
        raw_text, signature_block = without_artifacts.split(_SIGNATURE_MARKER, 1)
        author_name, author_role = _parse_signature(signature_block)
        is_signed = author_name is not None
    else:
        raw_text = without_artifacts
        author_name, author_role = None, None
        is_signed = False

    raw_text = raw_text.strip()
    header = _parse_header(raw_text)

    # Unsigned documents (e.g. the unlocked BPS intake) fall back to the
    # account Provider as the author — there is no signing clinician to record.
    if author_name is None:
        author_name = header["account_provider"]
        author_role = ""

    return raw_text, DocumentMetadata(
        client_name=header["client_name"],
        client_dob=header["client_dob"],
        template_title=header["template_title"],
        appointment_date=header["appointment_date"],
        authored_on=header["authored_on"],
        author_name=author_name,
        author_role=author_role or "",
        account_provider=header["account_provider"],
        is_signed=is_signed,
    )


def _extract_text(pdf_path: Path) -> str:
    """Concatenate every page's text layer, one page per line-break."""
    with pdfplumber.open(pdf_path) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _parse_header(text: str) -> dict:
    """Parse the SimplePractice header block from the top of the document."""
    client = _CLIENT.search(text)
    provider = _PROVIDER.search(text)
    title = _TEMPLATE_TITLE.search(text)
    appointment_date = _parse_appointment_date(text)
    authored_on = _parse_authored_on(text, appointment_date)

    return {
        "client_name": client.group(1).strip() if client else "",
        "client_dob": _parse_dob(text),
        "account_provider": provider.group(1).strip() if provider else "",
        "template_title": title.group(0).strip() if title else "",
        "appointment_date": appointment_date,
        "authored_on": authored_on,
    }


def _parse_dob(text: str) -> date | None:
    """Parse the ``DOB: MM/DD/YYYY`` header line."""
    match = _DOB.search(text)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%m/%d/%Y").date()


def _parse_appointment_date(text: str) -> date | None:
    """Parse the date out of ``Appointment: ... on <Month D, YYYY>``."""
    match = _APPOINTMENT_DATE.search(text)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%B %d, %Y").date()


def _parse_authored_on(text: str, appointment_date: date | None) -> datetime | None:
    """Combine the appointment date with the session start time.

    Falls back to midnight on the appointment date if the time line cannot be
    parsed, and to None if there is no appointment date at all.
    """
    if appointment_date is None:
        return None
    time_match = _APPOINTMENT_TIME.search(text)
    if not time_match:
        return datetime.combine(appointment_date, datetime.min.time())
    start_time = datetime.strptime(time_match.group(1).strip().lower(), "%I:%M %p").time()
    return datetime.combine(appointment_date, start_time)


def _parse_signature(signature_block: str) -> tuple[str | None, str | None]:
    """Parse ``(author_name, author_role)`` from a progress note's signature block.

    The block looks like::

        Signed by Dr. Aisha Patel, MD
        MD - Psychiatrist
        May 14, 2026 at 4:46 pm (CT)
        ...

    so the author name is the ``Signed by`` value and the role is the line
    immediately after it (the clinician's credential).
    """
    match = _SIGNED_BY.search(signature_block)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()
