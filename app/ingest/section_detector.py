"""
Document classification and section splitting.

Two jobs, both regex-first:

  * :func:`classify` -- maps a SimplePractice template title to a document type
    (``bps_intake | soap | dap | dsap``) by dispatching on the bracketed prefix
    the chart author put in every template name (``[BPS]``, ``[SOAP]``,
    ``[DAP]``, ``[DSAP]``). This is why the chart author was asked to prefix the
    templates — it makes classification a one-line deterministic lookup.
  * :func:`split_sections` -- splits ``raw_text`` into the format-specific
    sections, each carrying a char-offset span into ``raw_text``:
        bps_intake -> the numbered intake sections (1., 2., ... 21.)
        soap       -> subjective / objective / assessment / plan
        dap        -> data / assessment / plan
        dsap       -> data / subjective / assessment / plan

LLM fallback
------------
:func:`split_sections` is regex-first and deterministic. If the regex finds no
sections (a malformed or unexpected document), :func:`llm_fallback_sections`
asks an LLM to recover them. That path is bounded by ``MAX_LLM_CALLS_PER_INGEST``
(the orchestrator enforces the budget) and never fires for the well-formed
synthetic chart — the regex handles it entirely. It is the documented
graceful-degradation path, not the supported path.
"""

import json
import re

from app.core.config import get_settings
from app.ingest.provenance import Span

# Template-title prefix -> document type. The chart author prefixed every
# SimplePractice template with one of these brackets (see implementation plan M1).
DOC_TYPE_BY_PREFIX = {
    "[BPS]": "bps_intake",
    "[SOAP]": "soap",
    "[DAP]": "dap",
    "[DSAP]": "dsap",
}

# Section labels per progress-note format, in canonical order (playbook §F).
PROGRESS_NOTE_SECTIONS = {
    "soap": ["Subjective", "Objective", "Assessment", "Plan"],
    "dap": ["Data", "Assessment", "Plan"],
    "dsap": ["Data", "Subjective", "Assessment", "Plan"],
}

# A BPS numbered section header, e.g. "14. Physical Health" or
# "2. Signs and Symptoms (...) resulting in impairment(s):". The optional
# trailing colon is dropped from the captured title.
_NUMBERED_HEADER = re.compile(r"^(\d{1,2})\.\s+(.+?):?\s*$", re.MULTILINE)


def classify(template_title: str) -> str:
    """Map a SimplePractice template title to a document type.

    Returns ``"unknown"`` if no bracketed prefix matches — the caller's signal
    to fall back to LLM classification (never needed for the synthetic chart).
    """
    upper = template_title.upper()
    for prefix, doc_type in DOC_TYPE_BY_PREFIX.items():
        if upper.startswith(prefix):
            return doc_type
    return "unknown"


def split_sections(raw_text: str, doc_type: str) -> dict[str, dict]:
    """Split ``raw_text`` into format-specific sections with char-offset spans.

    Returns ``{section_key: {"title": str, "text": str, "char_span": [start, end]}}``.
    ``char_span`` points at the section's *content* (the value, trimmed of
    surrounding whitespace), not its header line.
    """
    if doc_type == "bps_intake":
        return _split_numbered_sections(raw_text)
    if doc_type in PROGRESS_NOTE_SECTIONS:
        return _split_labelled_sections(raw_text, PROGRESS_NOTE_SECTIONS[doc_type])
    raise ValueError(f"cannot split sections for unknown document type: {doc_type!r}")


def _split_numbered_sections(raw_text: str) -> dict[str, dict]:
    """Split a BPS intake on its ``N. Title`` numbered headers."""
    headers = list(_NUMBERED_HEADER.finditer(raw_text))
    sections: dict[str, dict] = {}
    for index, header in enumerate(headers):
        title = header.group(2).strip()
        next_start = headers[index + 1].start() if index + 1 < len(headers) else len(raw_text)
        span = _content_span(raw_text, header.end(), next_start)
        sections[_slug(title)] = {
            "title": title,
            "text": span.snippet(raw_text),
            "char_span": [span.char_start, span.char_end],
        }
    return sections


def _split_labelled_sections(raw_text: str, labels: list[str]) -> dict[str, dict]:
    """Split a progress note on its known section-label lines (Subjective, ...)."""
    # Locate each label as a line that is *exactly* the label — this avoids
    # matching the word where it appears inside a sentence.
    positions: list[tuple[str, int, int]] = []
    for label in labels:
        match = re.search(rf"^{re.escape(label)}\s*$", raw_text, re.MULTILINE)
        if match:
            positions.append((label, match.start(), match.end()))
    positions.sort(key=lambda found: found[1])

    sections: dict[str, dict] = {}
    for index, (label, _, label_end) in enumerate(positions):
        next_start = positions[index + 1][1] if index + 1 < len(positions) else len(raw_text)
        span = _content_span(raw_text, label_end, next_start)
        sections[label.lower()] = {
            "title": label,
            "text": span.snippet(raw_text),
            "char_span": [span.char_start, span.char_end],
        }
    return sections


def _content_span(raw_text: str, start: int, end: int) -> Span:
    """Return the Span of ``raw_text[start:end]`` trimmed of surrounding whitespace.

    Trimming keeps ``char_span`` pointing at real content, so the provenance
    round-trip (``raw_text[span]`` == stored text) holds exactly.
    """
    region = raw_text[start:end]
    leading = len(region) - len(region.lstrip())
    trailing = len(region) - len(region.rstrip())
    return Span(start + leading, end - trailing)


def _slug(title: str) -> str:
    """Turn a human section title into a stable snake_case key."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", title.lower())).strip("_")


def llm_fallback_sections(raw_text: str, doc_type: str) -> dict[str, dict]:
    """Recover section spans via an LLM when the regex splitter finds nothing.

    Used only on a regex miss; the orchestrator is responsible for the
    ``MAX_LLM_CALLS_PER_INGEST`` budget. Requires ``OPENAI_API_KEY`` — raises if
    it is unset, because the regex path is the supported path for the Phase 1
    synthetic chart and this fallback exists only for malformed real-world
    documents.

    The LLM is asked for section *text* (not offsets — models are unreliable at
    character arithmetic); each returned chunk is then located in ``raw_text``
    with ``str.find`` to recover a true provenance span.
    """
    settings = get_settings()
    if not settings.openai_api_key:
        raise RuntimeError(
            "section regex found no sections and OPENAI_API_KEY is unset — "
            "the LLM section-detection fallback is unavailable. "
            f"Inspect the {doc_type!r} document's format."
        )

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "You split a clinical document into its sections. Return a "
                    'JSON object {"sections": [{"key": str, "title": str, '
                    '"text": str}]} where each text is copied verbatim from the '
                    "document. Do not paraphrase."
                ),
            },
            {"role": "user", "content": f"Document type: {doc_type}\n\n{raw_text}"},
        ],
    )
    payload = json.loads(response.choices[0].message.content or "{}")

    sections: dict[str, dict] = {}
    for entry in payload.get("sections", []):
        text = entry["text"]
        start = raw_text.find(text)
        if start == -1:  # the model paraphrased — cannot anchor provenance, skip it
            continue
        sections[entry["key"]] = {
            "title": entry.get("title", entry["key"]),
            "text": text,
            "char_span": [start, start + len(text)],
        }
    return sections
