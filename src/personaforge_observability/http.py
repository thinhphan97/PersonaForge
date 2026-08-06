import logging
import re
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

from personaforge_observability.context import bind_context, new_id, parse_traceparent

logger = logging.getLogger(__name__)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SESSION_PATH = re.compile(r"/sessions/([^/]+)")
_WORKER_PATH = re.compile(r"/workers/([^/]+)")


def install_http_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def observe_request(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied_request_id = request.headers.get("x-request-id", "")
        request_id = (
            supplied_request_id
            if _SAFE_REQUEST_ID.fullmatch(supplied_request_id)
            else new_id()
        )
        trace_id, span_id, trace_flags = parse_traceparent(
            request.headers.get("traceparent")
        )
        path = request.url.path
        session_match = _SESSION_PATH.search(path)
        worker_match = _WORKER_PATH.search(path)
        fields: dict[str, object] = {
            "request_id": request_id,
            "trace_id": trace_id,
            "span_id": span_id,
            "trace_flags": trace_flags,
        }
        if session_match is not None:
            fields["session_id"] = session_match.group(1)
        if worker_match is not None:
            fields["worker_id"] = worker_match.group(1)
        started = time.monotonic()
        with bind_context(**fields):
            try:
                response = await call_next(request)
            except Exception:
                logger.exception(
                    "HTTP request failed",
                    extra={
                        "event": "http.request.failed",
                        "method": request.method,
                        "path": path,
                        "duration_ms": round((time.monotonic() - started) * 1000, 2),
                        "outcome": "failure",
                    },
                )
                raise
            response.headers["X-Request-ID"] = request_id
            response.headers["traceparent"] = (
                f"00-{trace_id}-{span_id}-{trace_flags}"
            )
            level = logging.DEBUG if path.startswith("/health/") else logging.INFO
            logger.log(
                level,
                "HTTP request completed",
                extra={
                    "event": "http.request.completed",
                    "method": request.method,
                    "path": path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "outcome": "success" if response.status_code < 500 else "failure",
                },
            )
            return response
