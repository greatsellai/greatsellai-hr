"""Safe request correlation and structured operational logging.

The application handles candidate and mailbox data, so operational logging must
be deliberately narrow.  This module emits only an allowlisted event schema:
it never serializes request bodies, headers, query strings, exception text, or
arbitrary ``logging`` messages.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


REQUEST_ID_HEADER = "X-Request-ID"

# Request IDs are service-issued UUID hex strings. Validation protects the
# structured logger and response construction, but must never be treated as
# proof that a client-supplied header is safe: arbitrary PII can be hex
# encoded. The public middleware therefore always creates a new value.
_REQUEST_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_EVENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_METHOD_PATTERN = re.compile(r"^[A-Z]{1,16}$")
_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9_./{}:-]{0,255}$")

# The formatter reads only these fields from a LogRecord.  Keeping this list
# small is the safety boundary: adding a field requires an explicit validator
# below and a review of whether it could contain applicant or credential data.
SAFE_LOG_FIELDS = frozenset(
    {
        "request_id",
        "method",
        "path",
        "status_code",
        "duration_ms",
        "workspace_id",
        "user_id",
        "job_id",
        "ai_run_id",
        "error_code",
        "error_type",
        "attempt",
        "provider_request_id",
    }
)

_request_id_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "resume_v3_request_id",
    default=None,
)
_OBSERVABILITY_LOGGER_NAME = "resume_screening.observability"
_OBSERVABILITY_HANDLER_MARKER = "_resume_v3_safe_json_handler"
_LEGACY_APP_LOGGER_NAME = "app"
_LEGACY_APP_HANDLER_MARKER = "_resume_v3_safe_legacy_app_handler"


def validate_request_id(value: object) -> str | None:
    """Return a service-ID-shaped value, or ``None`` when it is invalid."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not _REQUEST_ID_PATTERN.fullmatch(normalized):
        return None
    return normalized


def new_request_id() -> str:
    """Generate a compact opaque identifier suitable for logs and headers."""

    return uuid4().hex


def current_request_id() -> str | None:
    """Return the request ID associated with the current async context."""

    return _request_id_context.get()


def route_template_from_scope(scope: Scope) -> str:
    """Return a route template, never the raw path or its query string."""

    route = scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and _PATH_PATTERN.fullmatch(route_path):
        return route_path
    return "/unknown"


def _safe_event_name(value: object) -> str:
    if isinstance(value, str) and _EVENT_PATTERN.fullmatch(value):
        return value
    return "invalid_event"


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        return None
    return value


def _safe_http_method(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    method = value.upper()
    if not _METHOD_PATTERN.fullmatch(method):
        return None
    return method


def _safe_route_path(value: object) -> str | None:
    if not isinstance(value, str) or not _PATH_PATTERN.fullmatch(value):
        return None
    return value


def _safe_status_code(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 100 <= value <= 599:
        return value
    return None


def _safe_duration_ms(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    duration = int(value)
    if 0 <= duration <= 86_400_000:
        return duration
    return None


def _safe_attempt(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if 0 <= value <= 10_000:
        return value
    return None


def _safe_field(field_name: str, value: object) -> object | None:
    if field_name == "request_id":
        return validate_request_id(value)
    if field_name == "method":
        return _safe_http_method(value)
    if field_name == "path":
        return _safe_route_path(value)
    if field_name == "status_code":
        return _safe_status_code(value)
    if field_name == "duration_ms":
        return _safe_duration_ms(value)
    if field_name == "attempt":
        return _safe_attempt(value)
    if field_name in {
        "workspace_id",
        "user_id",
        "job_id",
        "ai_run_id",
        "error_code",
        "error_type",
        "provider_request_id",
    }:
        return _safe_identifier(value)
    return None


class SafeJsonFormatter(logging.Formatter):
    """Serialize only safe, validated observability fields as JSON.

    Deliberately do not call ``record.getMessage()`` or ``formatException``:
    either may contain request payloads, provider errors, credentials, or
    candidate data.  Events must be emitted through :func:`log_event`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "event": _safe_event_name(getattr(record, "event", None)),
        }

        for field_name in SAFE_LOG_FIELDS:
            value = _safe_field(field_name, getattr(record, field_name, None))
            if value is not None:
                payload[field_name] = value

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def get_observability_logger() -> logging.Logger:
    return logging.getLogger(_OBSERVABILITY_LOGGER_NAME)


def configure_observability_logging() -> logging.Logger:
    """Install one isolated stdout JSON handler for safe operational events.

    The explicit observability logger carries new structured events.  The
    legacy ``app`` namespace is isolated separately below so an overlooked
    ``logger.exception(...)`` cannot leak a raw provider error, candidate
    value, or traceback through a root server logger.
    """

    logger = get_observability_logger()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, _OBSERVABILITY_HANDLER_MARKER, False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(SafeJsonFormatter())
        setattr(handler, _OBSERVABILITY_HANDLER_MARKER, True)
        logger.addHandler(handler)
    configure_legacy_app_logging()
    return logger


def configure_legacy_app_logging() -> logging.Logger:
    """Contain legacy application logger records behind the safe formatter.

    ``app.*`` records historically flowed to the process/root logger.  A
    normal ``logger.exception`` formats both its message and traceback, either
    of which can contain provider responses or candidate data.  This one
    namespace-only boundary leaves Uvicorn, Caddy, and other server loggers
    untouched while ensuring any residual application record is rendered from
    the allowlisted :class:`SafeJsonFormatter` fields alone.
    """

    logger = logging.getLogger(_LEGACY_APP_LOGGER_NAME)
    logger.setLevel(logging.WARNING)
    logger.propagate = False

    # A handler directly attached to the ``app`` namespace would otherwise
    # receive raw ``LogRecord`` text before it reaches this formatter.  Remove
    # only handlers on this application namespace; root/Uvicorn/Caddy loggers
    # are deliberately outside this privacy boundary.
    for handler in tuple(logger.handlers):
        if not getattr(handler, _LEGACY_APP_HANDLER_MARKER, False):
            logger.removeHandler(handler)

    if not any(
        getattr(handler, _LEGACY_APP_HANDLER_MARKER, False)
        for handler in logger.handlers
    ):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(SafeJsonFormatter())
        setattr(handler, _LEGACY_APP_HANDLER_MARKER, True)
        logger.addHandler(handler)
    return logger


def log_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
    """Emit a safe observability event using the strict allowlist.

    Unknown fields are discarded before the ``LogRecord`` is created.  This is
    intentionally not a general-purpose logger: do not use it for free-form
    diagnostics or user-controlled values.
    """

    safe_extra: dict[str, object] = {"event": _safe_event_name(event)}
    for field_name, value in fields.items():
        if field_name not in SAFE_LOG_FIELDS:
            continue
        normalized = _safe_field(field_name, value)
        if normalized is not None:
            safe_extra[field_name] = normalized

    if "request_id" not in safe_extra:
        context_request_id = validate_request_id(current_request_id())
        if context_request_id is not None:
            safe_extra["request_id"] = context_request_id

    get_observability_logger().log(level, safe_extra["event"], extra=safe_extra)


def log_exception_event(
    event: str,
    *,
    error_code: str,
    exception: BaseException,
    level: int = logging.ERROR,
    **fields: object,
) -> None:
    """Record a caught exception without serializing its message or traceback.

    Application and worker callers should use a reviewed, fixed ``error_code``
    and may retain only the exception class name for diagnosis.  The exception
    object is never passed to :mod:`logging`, avoiding the implicit
    ``str(exception)`` and traceback formatting performed by ``logger.exception``.
    """

    safe_fields = dict(fields)
    safe_fields["error_code"] = error_code
    safe_fields["error_type"] = type(exception).__name__
    log_event(event, level=level, **safe_fields)


def safe_internal_error_response(request_id: str) -> JSONResponse:
    """Build a non-sensitive response for an unhandled application failure."""

    return JSONResponse(
        status_code=500,
        content={"detail": "internal_server_error", "request_id": request_id},
        headers={REQUEST_ID_HEADER: request_id},
    )


class RequestCorrelationMiddleware:
    """Attach a server-issued opaque request ID to each response and event."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Never reuse the public X-Request-ID header. A format check cannot
        # distinguish a genuine trace ID from a hex-encoded candidate name or
        # credential, and this ID may be stored in an audit record.
        request_id = new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id_context.set(request_id)
        started_at = time.perf_counter()
        response_status: int | None = None
        response_started = False

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, response_status
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message.get("status")
                if isinstance(status_code, int):
                    response_status = status_code
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            # This path covers failures FastAPI has not converted into a
            # response.  Do not serialize the exception text or traceback.
            log_event(
                "api_unhandled_exception",
                level=logging.ERROR,
                request_id=request_id,
                method=scope.get("method"),
                path=route_template_from_scope(scope),
                error_code="unhandled_exception",
                error_type=type(exc).__name__,
            )
            if response_started:
                # A streaming response may already be on the wire.  A second
                # response would corrupt it, so keep the safe event and let
                # the server close the stream.
                raise
            await safe_internal_error_response(request_id)(scope, receive, send_with_request_id)
        finally:
            elapsed_ms = round((time.perf_counter() - started_at) * 1000)
            log_event(
                "api_request_completed",
                level=logging.INFO,
                request_id=request_id,
                method=scope.get("method"),
                path=route_template_from_scope(scope),
                status_code=response_status if response_status is not None else 500,
                duration_ms=elapsed_ms,
            )
            _request_id_context.reset(token)
