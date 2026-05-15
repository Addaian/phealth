"""
Unified exception handlers for the dual API surface (Phase 2 PRD §5.8).

The same Python exception surfaces in two content types depending on which
namespace was hit:

- ``/api/v1/*`` errors  → RFC 7807 Problem Details (``application/problem+json``)
- ``/fhir/*``   errors  → FHIR R4 ``OperationOutcome`` (``application/fhir+json``)

A single handler per exception class dispatches on ``request.url.path`` so the
caller's namespace -- not the call site -- governs the response format. The
caller can therefore write ``raise HTTPException(404, "…")`` anywhere and the
correct envelope is applied automatically.

Coverage:
- ``StarletteHTTPException`` -- catches both FastAPI's ``HTTPException`` (the
  subclass our handlers raise) and Starlette's framework-internal 404s for
  unmounted paths. This is what lets ``GET /fhir/Patient/{id}`` return an
  ``OperationOutcome`` even before M8/M9 mount any ``/fhir`` route.
- ``RequestValidationError`` -- catches FastAPI's 422 path/query/body coercion
  errors (e.g. ``/api/v1/patients/not-a-uuid``).

Endpoints outside both prefixes (``/ingest``, ``/health``, ``/api/v1/capabilities``)
default to the RFC 7807 envelope: the JSON-API contract is the right baseline
for everything that is not strict FHIR.

References:
- RFC 7807 Problem Details for HTTP APIs: https://datatracker.ietf.org/doc/html/rfc7807
- FHIR R4 OperationOutcome: https://www.hl7.org/fhir/operationoutcome.html
- FHIR IssueType value set: https://www.hl7.org/fhir/valueset-issue-type.html
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

# HTTP status → FHIR IssueType code, per the IssueType value set linked above.
# Picked the closest semantic match for each status we expect to emit; falls
# back to "processing" for anything not enumerated.
_STATUS_TO_FHIR_ISSUE_CODE: dict[int, str] = {
    400: "invalid",
    401: "security",
    403: "forbidden",
    404: "not-found",
    405: "not-supported",
    409: "conflict",
    410: "deleted",
    415: "not-supported",
    422: "processing",
    500: "exception",
    503: "transient",
}

# HTTP status → RFC 7231 reason phrase, used as the RFC 7807 ``title`` field.
_STATUS_TO_TITLE: dict[int, str] = {
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    410: "Gone",
    415: "Unsupported Media Type",
    422: "Unprocessable Entity",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _is_fhir(request: Request) -> bool:
    """True if the request targets the strict-FHIR namespace.

    Matches the literal ``/fhir`` and anything under ``/fhir/*``.
    """
    path = request.url.path
    return path == "/fhir" or path.startswith("/fhir/")


def _coerce_detail(detail: Any) -> str:
    """Stringify an exception detail for the body's free-text field.

    FastAPI's ``HTTPException.detail`` is typed ``Any`` -- usually a string,
    occasionally a dict for richer error payloads. We coerce to a flat string
    for the ``detail`` (RFC 7807) and ``diagnostics`` (OperationOutcome)
    slots; structured payloads belong in ``errors`` / ``issue`` instead.
    """
    return detail if isinstance(detail, str) else str(detail)


def _problem_body(
    status_code: int,
    detail: Any,
    instance: str,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the RFC 7807 Problem Details body.

    ``type`` defaults to ``about:blank`` -- RFC 7807 §4.2 says clients SHOULD
    treat that as "no further information beyond the status code", which is
    the right default until we publish problem-type URIs of our own.

    ``errors`` is an RFC 7807 extension member (the RFC explicitly permits
    them); we use it to carry pydantic validation details when ``RequestValidationError``
    fires. RFC 9457 (the 7807 update) formalises this pattern.
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": _STATUS_TO_TITLE.get(status_code, "Error"),
        "status": status_code,
        "detail": _coerce_detail(detail),
        "instance": instance,
    }
    if errors:
        body["errors"] = errors
    return body


def _operation_outcome_body(
    status_code: int,
    detail: Any,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a FHIR R4 OperationOutcome body.

    Either a caller-built ``issues`` list (used for multi-issue validation
    failures) or a single default issue derived from the status code. Every
    issue carries ``severity: "error"`` -- we do not emit warnings or info
    from this layer.
    """
    if issues is None:
        issues = [
            {
                "severity": "error",
                "code": _STATUS_TO_FHIR_ISSUE_CODE.get(status_code, "processing"),
                "diagnostics": _coerce_detail(detail),
            }
        ]
    return {"resourceType": "OperationOutcome", "issue": issues}


def problem_response(
    request: Request,
    status_code: int,
    detail: Any,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """RFC 7807 ``application/problem+json`` JSON response."""
    body = _problem_body(status_code, detail, str(request.url.path), errors=errors)
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/problem+json",
    )


def operation_outcome_response(
    request: Request,
    status_code: int,
    detail: Any,
    issues: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """FHIR ``application/fhir+json`` OperationOutcome response."""
    body = _operation_outcome_body(status_code, detail, issues=issues)
    return JSONResponse(
        status_code=status_code,
        content=body,
        media_type="application/fhir+json",
    )


# ---------------------------------------------------------------------------
# Handlers registered with the FastAPI app
# ---------------------------------------------------------------------------


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Dispatch an HTTPException to the namespace's error envelope.

    Catches both ``fastapi.HTTPException`` (our raises) and Starlette's
    internal 404s for unmounted paths. The handler signature uses ``Exception``
    so it matches Starlette's ``ExceptionHandler`` Protocol; the cast is safe
    because FastAPI only dispatches subclasses of the registered class.
    """
    assert isinstance(exc, StarletteHTTPException)  # narrow for mypy
    if _is_fhir(request):
        return operation_outcome_response(request, exc.status_code, exc.detail)
    return problem_response(request, exc.status_code, exc.detail)


async def _validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Dispatch a ``RequestValidationError`` to the namespace's error envelope.

    Pydantic v2's ``errors()`` returns dicts with ``loc`` (tuple of str|int),
    ``msg``, ``type``, and sometimes ``ctx``. We project to a JSON-safe subset
    (``loc`` → list, drop ``ctx``) before embedding. For OperationOutcome we
    emit one ``issue`` per validation error and use ``expression`` to convey
    the failing path (a dotted ``loc``).
    """
    assert isinstance(exc, RequestValidationError)
    raw_errors = exc.errors()
    if _is_fhir(request):
        issues = [
            {
                "severity": "error",
                "code": "processing",
                "diagnostics": err.get("msg", "validation error"),
                "expression": [".".join(str(part) for part in err.get("loc", []))],
            }
            for err in raw_errors
        ]
        return operation_outcome_response(request, 422, "Request validation failed", issues=issues)

    safe_errors = [
        {
            "loc": list(err.get("loc", [])),
            "msg": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in raw_errors
    ]
    return problem_response(request, 422, "Request validation failed", errors=safe_errors)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the two unified handlers to the FastAPI app.

    Call once at app construction (see ``app/main.py``). Handlers registered
    here replace FastAPI's defaults that emit ``{"detail": ...}`` -- which is
    why the existing ``tests/test_security.py`` 401 assertions still pass
    against ``response.status_code`` but the body shape now carries the RFC
    7807 envelope.
    """
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
