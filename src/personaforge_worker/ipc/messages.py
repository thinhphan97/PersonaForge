import json
import time
from typing import Any

from personaforge.worker.v1 import child_control_pb2

SCHEMA_VERSION = 1
PROTOCOL_VERSION = "personaforge.worker.v1"


def command_message(
    command: dict[str, Any],
    *,
    worker_id: str,
    child_id: str,
    gpu_uuid: str,
    sequence: int,
) -> child_control_pb2.MasterToChild:
    command_id = required_string(command, "command_id")
    command_type = required_string(command, "type")
    serialized = json.dumps(command, separators=(",", ":"))
    message = child_control_pb2.MasterToChild(
        meta=message_meta(
            message_id=command_id,
            worker_id=worker_id,
            child_id=child_id,
            session_id=required_string(command, "session_id"),
            gpu_uuid=gpu_uuid,
            lease_token=optional_string(command, "lease_token", "legacy"),
            generation=optional_int(command, "generation", 0),
            sequence=sequence,
        )
    )
    if command_type == "avatar.session.start":
        message.start.command_json = serialized
    elif command_type == "avatar.state.set":
        message.change_state.command_json = serialized
    elif command_type == "avatar.speech.synthesize":
        message.speech.command_json = serialized
    elif command_type == "avatar.session.stop":
        message.stop.command_json = serialized
    else:
        raise ValueError(f"Unsupported render command: {command_type}")
    return message


def decode_command(message: child_control_pb2.MasterToChild) -> dict[str, Any]:
    payload_name = message.WhichOneof("payload")
    if payload_name is None:
        raise ValueError("Master command has no payload")
    payload = getattr(message, payload_name)
    raw = payload.command_json
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Master command JSON must be an object")
    return data


def message_meta(
    *,
    message_id: str,
    worker_id: str,
    child_id: str,
    session_id: str,
    gpu_uuid: str,
    lease_token: str,
    generation: int,
    sequence: int,
) -> child_control_pb2.MessageMeta:
    return child_control_pb2.MessageMeta(
        schema_version=SCHEMA_VERSION,
        message_id=message_id,
        worker_id=worker_id,
        child_id=child_id,
        session_id=session_id,
        gpu_uuid=gpu_uuid,
        lease_token=lease_token,
        generation=generation,
        sequence=sequence,
        sent_at_unix_ms=int(time.time() * 1_000),
    )


def validate_meta(
    meta: child_control_pb2.MessageMeta,
    *,
    worker_id: str,
    child_id: str,
    session_id: str,
    gpu_uuid: str,
    lease_token: str | None = None,
    generation: int | None = None,
) -> None:
    if meta.schema_version != SCHEMA_VERSION:
        raise ValueError(f"Unsupported child protocol schema: {meta.schema_version}")
    expected = {
        "worker_id": worker_id,
        "child_id": child_id,
        "session_id": session_id,
        "gpu_uuid": gpu_uuid,
    }
    for field, value in expected.items():
        if getattr(meta, field) != value:
            raise ValueError(f"Child message has invalid {field}")
    if lease_token is not None and meta.lease_token != lease_token:
        raise ValueError("Child message has invalid lease token")
    if generation is not None and meta.generation != generation:
        raise ValueError("Child message has invalid generation")


def required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def required_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def optional_int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def optional_float(data: dict[str, Any], key: str, default: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return float(value)


def optional_string(data: dict[str, Any], key: str, default: str) -> str:
    value = data.get(key, default)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid '{key}'")
    return value
