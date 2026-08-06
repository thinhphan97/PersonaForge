import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, Token
from uuid import uuid4

_CONTEXT: ContextVar[dict[str, object] | None] = ContextVar("log_context", default=None)
_TRACEPARENT = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


def new_id() -> str:
    return uuid4().hex


def current_context() -> dict[str, object]:
    return dict(_CONTEXT.get() or {})


@contextmanager
def bind_context(**fields: object) -> Iterator[None]:
    clean = {key: value for key, value in fields.items() if value is not None and value != ""}
    merged = {**(_CONTEXT.get() or {}), **clean}
    token: Token[dict[str, object] | None] = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def parse_traceparent(value: str | None) -> tuple[str, str, str]:
    if value is not None:
        match = _TRACEPARENT.fullmatch(value.strip().lower())
        if (
            match is not None
            and match.group("version") != "ff"
            and set(match.group("trace_id")) != {"0"}
            and set(match.group("span_id")) != {"0"}
        ):
            trace_id = match.group("trace_id")
            flags = match.group("flags")
            return trace_id, new_id()[:16], flags
    return new_id(), new_id()[:16], "01"


def grpc_metadata(
    existing: Sequence[tuple[str, str]] = (),
) -> tuple[tuple[str, str], ...]:
    context = current_context()
    metadata = list(existing)
    request_id = context.get("request_id")
    trace_id = context.get("trace_id")
    span_id = context.get("span_id")
    trace_flags = context.get("trace_flags", "01")
    if isinstance(request_id, str):
        metadata.append(("x-request-id", request_id))
    if isinstance(trace_id, str) and isinstance(span_id, str):
        metadata.append(
            ("traceparent", f"00-{trace_id}-{span_id}-{trace_flags}")
        )
    return tuple(metadata)


def http_headers(existing: Mapping[str, str] | None = None) -> dict[str, str]:
    headers = dict(existing or {})
    context = current_context()
    request_id = context.get("request_id")
    trace_id = context.get("trace_id")
    span_id = context.get("span_id")
    trace_flags = context.get("trace_flags", "01")
    if isinstance(request_id, str):
        headers["X-Request-ID"] = request_id
    if isinstance(trace_id, str) and isinstance(span_id, str):
        headers["traceparent"] = f"00-{trace_id}-{span_id}-{trace_flags}"
    return headers


def context_from_mapping(values: Mapping[str, object]) -> dict[str, object]:
    keys = (
        "request_id",
        "trace_id",
        "span_id",
        "trace_flags",
        "session_id",
        "turn_id",
        "utterance_id",
        "command_id",
        "worker_id",
        "child_id",
        "gpu_uuid",
    )
    return {key: values[key] for key in keys if values.get(key) not in (None, "")}
