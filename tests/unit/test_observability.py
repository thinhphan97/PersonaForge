import json
import logging

from personaforge_observability.context import (
    bind_context,
    current_context,
    grpc_metadata,
    http_headers,
    parse_traceparent,
)
from personaforge_observability.logging import JsonLogFormatter


def test_json_formatter_emits_stable_schema_and_bound_context() -> None:
    formatter = JsonLogFormatter("test-service", "test", "1.2.3")
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="Request completed",
        args=(),
        exc_info=None,
    )
    record.event = "test.request.completed"
    record.status_code = 200
    record.authorization = "Bearer must-not-be-logged"

    with bind_context(request_id="request-1", session_id="session-1"):
        payload = json.loads(formatter.format(record))

    assert payload["service"] == "test-service"
    assert payload["event"] == "test.request.completed"
    assert payload["request_id"] == "request-1"
    assert payload["session_id"] == "session-1"
    assert payload["status_code"] == 200
    assert "authorization" not in payload


def test_bind_context_is_nested_and_restored() -> None:
    assert current_context() == {}
    with bind_context(request_id="request-1"):
        with bind_context(session_id="session-1"):
            assert current_context() == {
                "request_id": "request-1",
                "session_id": "session-1",
            }
        assert current_context() == {"request_id": "request-1"}
    assert current_context() == {}


def test_trace_context_is_parsed_and_propagated_to_grpc() -> None:
    incoming_trace_id = "1" * 32
    trace_id, span_id, flags = parse_traceparent(
        f"00-{incoming_trace_id}-{'2' * 16}-01"
    )
    assert trace_id == incoming_trace_id
    assert len(span_id) == 16

    with bind_context(
        request_id="request-1",
        trace_id=trace_id,
        span_id=span_id,
        trace_flags=flags,
    ):
        metadata = dict(grpc_metadata())
        headers = http_headers({"Authorization": "Bearer secret"})

    assert metadata["x-request-id"] == "request-1"
    assert metadata["traceparent"] == f"00-{trace_id}-{span_id}-01"
    assert headers["X-Request-ID"] == "request-1"
    assert headers["traceparent"] == f"00-{trace_id}-{span_id}-01"
    assert headers["Authorization"] == "Bearer secret"


def test_invalid_traceparent_creates_a_new_trace() -> None:
    trace_id, span_id, flags = parse_traceparent("not-a-traceparent")
    assert len(trace_id) == 32
    assert len(span_id) == 16
    assert flags == "01"
