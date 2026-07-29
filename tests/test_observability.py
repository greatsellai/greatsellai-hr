from __future__ import annotations

import io
import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import observability


def test_request_id_is_generated_reused_and_replaced_when_invalid(client) -> None:
    generated = client.get("/health")
    generated_id = generated.headers[observability.REQUEST_ID_HEADER]
    assert generated.status_code == 200
    assert observability.validate_request_id(generated_id) == generated_id

    supplied_id = "abcdef0123456789abcdef0123456789"
    reused = client.get("/health", headers={observability.REQUEST_ID_HEADER: supplied_id})
    assert reused.headers[observability.REQUEST_ID_HEADER] == supplied_id

    invalid = client.get(
        "/health",
        headers={observability.REQUEST_ID_HEADER: "candidate-name-must-not-be-logged"},
    )
    invalid_id = invalid.headers[observability.REQUEST_ID_HEADER]
    assert invalid_id != "candidate-name-must-not-be-logged"
    assert observability.validate_request_id(invalid_id) == invalid_id
    assert observability.current_request_id() is None


def test_safe_json_formatter_excludes_messages_exceptions_and_unapproved_fields() -> None:
    formatter = observability.SafeJsonFormatter()
    record = logging.LogRecord(
        name="tests.observability",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="raw-message-secret-do-not-log",
        args=(),
        exc_info=(RuntimeError, RuntimeError("exception-secret-do-not-log"), None),
    )
    record.event = "api_request_completed"
    record.request_id = "1234567890abcdef1234567890abcdef"
    record.method = "POST"
    record.path = "/v1/resumes/{resume_id}"
    record.status_code = 500
    record.duration_ms = 18
    record.headers = {"authorization": "header-secret-do-not-log"}
    record.body = "body-secret-do-not-log"
    record.query_string = "query-secret-do-not-log"
    record.password = "password-secret-do-not-log"

    rendered = formatter.format(record)
    payload = json.loads(rendered)

    assert payload == {
        "duration_ms": 18,
        "event": "api_request_completed",
        "level": "ERROR",
        "method": "POST",
        "path": "/v1/resumes/{resume_id}",
        "request_id": "1234567890abcdef1234567890abcdef",
        "status_code": 500,
        "timestamp": payload["timestamp"],
    }
    for secret in (
        "raw-message-secret-do-not-log",
        "exception-secret-do-not-log",
        "header-secret-do-not-log",
        "body-secret-do-not-log",
        "query-secret-do-not-log",
        "password-secret-do-not-log",
    ):
        assert secret not in rendered


def test_log_event_discards_unknown_or_unsafe_fields(monkeypatch) -> None:
    stream = io.StringIO()
    logger = logging.Logger("tests.safe_observability")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(observability.SafeJsonFormatter())
    logger.addHandler(handler)
    monkeypatch.setattr(observability, "get_observability_logger", lambda: logger)

    observability.log_event(
        "api_request_completed",
        request_id="fedcba9876543210fedcba9876543210",
        status_code=204,
        body="resume-text-must-not-log",
        authorization="credential-must-not-log",
        headers={"x-api-key": "credential-must-not-log"},
        error_code="safe_error",
    )

    payload = json.loads(stream.getvalue())
    assert payload["request_id"] == "fedcba9876543210fedcba9876543210"
    assert payload["status_code"] == 204
    assert payload["error_code"] == "safe_error"
    assert "body" not in payload
    assert "authorization" not in payload
    assert "headers" not in payload
    assert "credential-must-not-log" not in stream.getvalue()
    assert "resume-text-must-not-log" not in stream.getvalue()


def test_log_exception_event_omits_exception_message_from_safe_handler(monkeypatch) -> None:
    stream = io.StringIO()
    logger = logging.Logger("tests.safe_exception_observability")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(observability.SafeJsonFormatter())
    logger.addHandler(handler)
    monkeypatch.setattr(observability, "get_observability_logger", lambda: logger)

    observability.log_exception_event(
        "ai_extraction_worker_failed",
        error_code="ai_extraction_worker_error",
        exception=RuntimeError("candidate-payload-must-not-log@example.test"),
        job_id="job-safe-123",
        workspace_id="workspace-safe-456",
        prompt="resume-text-must-not-log",
    )

    rendered = stream.getvalue()
    payload = json.loads(rendered)
    assert payload["event"] == "ai_extraction_worker_failed"
    assert payload["error_code"] == "ai_extraction_worker_error"
    assert payload["error_type"] == "RuntimeError"
    assert payload["job_id"] == "job-safe-123"
    assert payload["workspace_id"] == "workspace-safe-456"
    assert "candidate-payload-must-not-log@example.test" not in rendered
    assert "resume-text-must-not-log" not in rendered


def test_legacy_app_logger_isolated_from_raw_message_and_traceback(monkeypatch) -> None:
    stream = io.StringIO()
    app_logger = logging.getLogger("app")
    legacy_logger = logging.getLogger("app.tests.legacy_raw_log")
    original_handlers = list(app_logger.handlers)
    original_level = app_logger.level
    original_propagate = app_logger.propagate
    original_legacy_handlers = list(legacy_logger.handlers)
    original_legacy_level = legacy_logger.level
    original_legacy_propagate = legacy_logger.propagate
    for handler in tuple(app_logger.handlers):
        app_logger.removeHandler(handler)
    for handler in tuple(legacy_logger.handlers):
        legacy_logger.removeHandler(handler)
    monkeypatch.setattr(observability.sys, "stdout", stream)

    try:
        observability.configure_legacy_app_logging()
        legacy_logger.error(
            "raw-provider-response candidate-payload-must-not-log@example.test",
            exc_info=(
                RuntimeError,
                RuntimeError("traceback-secret-must-not-log"),
                None,
            ),
        )
        rendered = stream.getvalue()
        payload = json.loads(rendered)

        assert payload["event"] == "invalid_event"
        assert payload["level"] == "ERROR"
        assert app_logger.propagate is False
        assert "raw-provider-response" not in rendered
        assert "candidate-payload-must-not-log@example.test" not in rendered
        assert "traceback-secret-must-not-log" not in rendered
    finally:
        for handler in tuple(app_logger.handlers):
            app_logger.removeHandler(handler)
        for handler in original_handlers:
            app_logger.addHandler(handler)
        app_logger.setLevel(original_level)
        app_logger.propagate = original_propagate
        for handler in tuple(legacy_logger.handlers):
            legacy_logger.removeHandler(handler)
        for handler in original_legacy_handlers:
            legacy_logger.addHandler(handler)
        legacy_logger.setLevel(original_legacy_level)
        legacy_logger.propagate = original_legacy_propagate


def test_unhandled_exception_returns_safe_diagnostic_response_and_event(monkeypatch) -> None:
    events: list[tuple[str, int, dict[str, object]]] = []

    def capture_event(event: str, *, level: int = logging.INFO, **fields: object) -> None:
        events.append((event, level, fields))

    monkeypatch.setattr(observability, "log_event", capture_event)

    app = FastAPI()
    app.add_middleware(observability.RequestCorrelationMiddleware)

    @app.get("/test/explode")
    async def explode() -> None:
        raise RuntimeError("provider response with secret should not reach a client or log event")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/test/explode",
            headers={observability.REQUEST_ID_HEADER: "0123456789abcdef0123456789abcdef"},
        )

    assert response.status_code == 500
    assert response.headers[observability.REQUEST_ID_HEADER] == "0123456789abcdef0123456789abcdef"
    assert response.json() == {
        "detail": "internal_server_error",
        "request_id": "0123456789abcdef0123456789abcdef",
    }

    error_events = [event for event in events if event[0] == "api_unhandled_exception"]
    assert error_events == [
        (
            "api_unhandled_exception",
            logging.ERROR,
            {
                "request_id": "0123456789abcdef0123456789abcdef",
                "method": "GET",
                "path": "/test/explode",
                "error_code": "unhandled_exception",
                "error_type": "RuntimeError",
            },
        )
    ]
    assert "provider response with secret" not in repr(events)

    completion_events = [event for event in events if event[0] == "api_request_completed"]
    assert len(completion_events) == 1
    assert completion_events[0][2]["status_code"] == 500
