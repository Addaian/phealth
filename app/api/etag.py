"""
ETag generation and conditional-request handling (Phase 2 PRD §5.7).

Every 200 OK on a GET that returns JSON carries a weak ``ETag`` header derived
from the response body via ``xxhash.xxh64`` -- a non-cryptographic hash chosen
for speed; cache validation is a hint, not a security boundary. A repeat
request that sends ``If-None-Match: <etag>`` returns ``304 Not Modified`` with
no body, so a polling reviewer (or an HTTP cache) avoids re-downloading the
same chart on every refresh.

We hash the *rendered* response bytes rather than the pre-serialised payload:
the bytes are deterministic for our handlers (pydantic ``model_dump`` preserves
field declaration order; Python dict insertion order is stable since 3.7), and
the bytes are what the client actually receives -- so a byte-level digest is
the truest "did the response change?" signal.

The weak-ETag prefix ``W/`` (RFC 7232 §2.3) signals that two ETag values may
not be byte-for-byte identical even if they represent the same semantic
resource. For this MVP every digest is byte-derived so strong ETags would also
be correct, but weak is the safer default for a JSON API where framework
upgrades might change incidental whitespace or key ordering.

Streaming media types (NDJSON ``$export``, octet-stream) are skipped: hashing
would force the whole stream into memory, defeating the point of streaming.
For those endpoints, the lack of an ``ETag`` header is the explicit contract.

References:
- RFC 7232 Conditional Requests (ETag, If-None-Match):
  https://datatracker.ietf.org/doc/html/rfc7232
- xxhash docs: https://cyan4973.github.io/xxHash/
"""

from collections.abc import Awaitable, Callable

import xxhash
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Media types we will not buffer to ETag: streaming or binary payloads where
# whole-body hashing is too expensive or semantically wrong.
_STREAMING_MEDIA_TYPES: tuple[str, ...] = (
    "application/fhir+ndjson",
    "application/octet-stream",
)


def make_etag(body: bytes) -> str:
    """Build a weak-ETag value (with surrounding quotes) for ``body``.

    Uses ``xxh64`` truncated to 16 hex chars; with ~1B distinct payloads the
    birthday-paradox collision probability is ~3e-3 -- well within tolerance
    for cache validation.
    """
    digest = xxhash.xxh64(body).hexdigest()
    return f'W/"{digest}"'


class ETagMiddleware(BaseHTTPMiddleware):
    """Compute an ``ETag`` for every JSON GET 200 and honour ``If-None-Match``.

    Buffers the response body to hash it, so this middleware is incompatible
    with streaming responses -- those are opted out via ``_STREAMING_MEDIA_TYPES``.

    Sequencing: install this *after* the routers and the exception handlers so
    the middleware sees the final rendered body, including the bodies produced
    by ``app/api/errors.py`` (those carry a status >= 400 and are skipped, but
    the byte path is the same).
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)

        # Only ETag idempotent JSON reads. 4xx/5xx already carry RFC 7807 /
        # OperationOutcome bodies -- caching those would mask a fix.
        if request.method != "GET" or response.status_code != 200:
            return response
        content_type = response.headers.get("content-type", "")
        if any(media in content_type for media in _STREAMING_MEDIA_TYPES):
            return response

        # Buffer the streaming body so we can hash it. Under
        # ``BaseHTTPMiddleware`` Starlette always wraps the handler's response
        # as ``_StreamingResponse`` (a private subclass that shares the
        # ``body_iterator`` interface with the public ``StreamingResponse``
        # but does NOT inherit from it -- a plain ``isinstance`` check fails).
        # The duck-type defends against a future Starlette refactor that
        # changes that wrapping; today the ``None`` branch is unreachable.
        body_iterator = getattr(response, "body_iterator", None)
        if body_iterator is None:
            return response  # defensive; not exercised under current Starlette
        chunks: list[bytes] = []
        async for chunk in body_iterator:
            if isinstance(chunk, bytes):
                chunks.append(chunk)
            elif isinstance(chunk, str):
                chunks.append(chunk.encode())
            else:
                chunks.append(bytes(chunk))
        body = b"".join(chunks)

        etag = make_etag(body)

        # Cache hit: respond 304 with the ETag and *no body*. RFC 7232 §4.1
        # requires the same validators that a 200 would have carried; we echo
        # the ETag only -- Last-Modified is not produced by this layer.
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})

        # Cache miss: rebuild the response with the same body, status, and
        # media_type, plus the new ETag header. content-length must be
        # recomputed because the streaming form had no fixed length.
        headers = dict(response.headers)
        headers["ETag"] = etag
        headers["content-length"] = str(len(body))
        return Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
