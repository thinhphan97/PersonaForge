import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

from personaforge_observability.context import current_context

_STANDARD_FIELDS = set(logging.makeLogRecord({}).__dict__)
_SAFE_FIELDS = {
    "event",
    "environment",
    "service_version",
    "process_role",
    "request_id",
    "trace_id",
    "span_id",
    "trace_flags",
    "connection_id",
    "session_id",
    "user_id",
    "turn_id",
    "utterance_id",
    "command_id",
    "command_type",
    "worker_id",
    "child_id",
    "gpu_uuid",
    "gpu_id",
    "method",
    "path",
    "status_code",
    "grpc_status",
    "duration_ms",
    "attempt",
    "outcome",
    "reason",
    "input_chars",
    "output_chars",
    "history_count",
    "event_count",
    "active_turns",
    "queued_turns",
    "stream_message_id",
    "listen_address",
}


class JsonLogFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str, service_version: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment
        self._service_version = service_version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "service_version": self._service_version,
            "event": getattr(record, "event", "application.log"),
            "message": record.getMessage(),
            "logger": record.name,
        }
        payload.update(current_context())
        for key, value in record.__dict__.items():
            if key not in _STANDARD_FIELDS and key in _SAFE_FIELDS and value is not None:
                payload[key] = value
        if record.exc_info:
            exception_type = record.exc_info[0]
            payload["exception_type"] = (
                exception_type.__name__ if exception_type is not None else "Exception"
            )
            payload["exception_message"] = str(record.exc_info[1])
            payload["stack_trace"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))


class ConsoleLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = {**current_context()}
        fields.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key in _SAFE_FIELDS and value is not None
            }
        )
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        return f"{base} {suffix}".rstrip()


def configure_logging(service: str, process_role: str | None = None) -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(
            JsonLogFormatter(
                service=service,
                environment=os.getenv("LOG_ENVIRONMENT", "development"),
                service_version=os.getenv("LOG_SERVICE_VERSION", "0.1.0"),
            )
        )
    else:
        handler.setFormatter(
            ConsoleLogFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.captureWarnings(True)
    third_party_name = os.getenv("LOG_THIRD_PARTY_LEVEL", "WARNING").upper()
    third_party_level = getattr(logging, third_party_name, logging.WARNING)
    for logger_name in ("httpx", "httpcore"):
        logging.getLogger(logger_name).setLevel(third_party_level)
    # Uvicorn installs isolated handlers before importing the ASGI application.
    # Route those records through the same formatter so container stdout remains
    # one machine-readable stream.
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        framework_logger = logging.getLogger(logger_name)
        framework_logger.handlers.clear()
        framework_logger.propagate = True
    if process_role is not None:
        # LogRecordFactory makes the process role available on third-party logs too.
        previous_factory = logging.getLogRecordFactory()

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = previous_factory(*args, **kwargs)
            record.process_role = process_role
            return record

        logging.setLogRecordFactory(factory)
