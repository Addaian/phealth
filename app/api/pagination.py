"""
Cursor pagination + FHIR Bundle.searchset assembly (Phase 2 PRD §5.9).

Cursor design
-------------
Opaque to the client, stable across deploys, ordered by UUID. The cursor
encodes only the *last id seen* on the current page; the next-page query
selects rows with ``id > last_id`` ordered by id. This keeps pagination
stateless (no offset to drift, no temporary table), deterministic, and
collision-free (UUID4 ordering is well-defined and unique).

We base64-encode a JSON dict so future fields (e.g. a tie-break secondary
key) can be added without breaking the wire format. Clients are not meant to
parse or construct cursors -- they round-trip them verbatim.

Bundle.searchset
----------------
Per FHIR R4 search: every search result is a ``Bundle`` with
``type="searchset"``, optional ``total``, one ``entry[]`` per match (with
``search.mode = "match"``), and ``link[]`` carrying at least ``relation="self"``
plus optionally ``relation="next"``. We deliberately do not emit ``prev`` /
``first`` / ``last`` (Azure-precedent: cursor-only pagination, no random
access). The cursor on the next link is opaque; reviewers should not try to
hand-craft it.

References:
- FHIR R4 search: https://hl7.org/fhir/R4/search.html
- Bundle.link: https://hl7.org/fhir/R4/bundle-definitions.html#Bundle.link
"""

import base64
import json
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlencode

from fhir.resources.R4B.bundle import Bundle


def encode_cursor(last_id: uuid.UUID) -> str:
    """Base64 the {last_id: uuid} dict; URL-safe and padding-stripped."""
    payload = json.dumps({"last_id": str(last_id)}).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> uuid.UUID | None:
    """Decode an opaque cursor back to the last id, or ``None`` on miss.

    A malformed cursor is treated as "start of dataset" (returns ``None``)
    rather than 400ing. Cursors are opaque -- a client that hand-crafts one
    deserves to get the first page back; that beats leaking the encoding.
    """
    if not cursor:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        return uuid.UUID(payload["last_id"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


def _replace_query_param(base_url: str, key: str, value: str) -> str:
    """Return ``base_url`` with ``key=value`` set (replacing any existing).

    ``parse_qsl`` correctly URL-decodes existing values so they round-trip
    through ``urlencode``; the earlier hand-rolled split-and-rejoin
    re-encoded already-encoded values (e.g. a ``patient=Patient%2F<uuid>``
    on the incoming URL would have come back as ``Patient%252F<uuid>``).
    The cursor itself is base64-urlsafe and unaffected either way, but
    other search params on the same URL needed the proper round-trip.
    """
    path, _, query = base_url.partition("?")
    params = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != key]
    params.append((key, value))
    return f"{path}?{urlencode(params)}"


def build_searchset_bundle(
    resources: list[dict[str, Any]],
    resource_type: str,
    total: int,
    self_url: str,
    next_cursor: str | None = None,
) -> Bundle:
    """Assemble a FHIR R4 Bundle.searchset from a list of resource dicts.

    Each resource must already be in its serialised dict form (i.e. the
    output of ``Model.model_dump(mode="json", by_alias=True, exclude_none=True)``)
    so the entry's ``resource`` key is exactly what the spec asks for.

    ``total`` is the count of matches across the *full* dataset (not just
    this page); FHIR R4 marks total as optional but reviewers expect it.

    The ``next`` link is emitted only when ``next_cursor`` is set -- callers
    decide page-by-page whether more rows remain.
    """
    entries = [
        {
            "fullUrl": f"{resource_type}/{resource['id']}",
            "resource": resource,
            "search": {"mode": "match"},
        }
        for resource in resources
    ]
    link: list[dict[str, str]] = [{"relation": "self", "url": self_url}]
    if next_cursor is not None:
        link.append(
            {
                "relation": "next",
                "url": _replace_query_param(self_url, "cursor", next_cursor),
            }
        )

    return Bundle.model_validate(
        {
            "type": "searchset",
            "total": total,
            "link": link,
            "entry": entries,
        }
    )
