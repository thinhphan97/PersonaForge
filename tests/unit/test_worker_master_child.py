import asyncio
from typing import Any

import grpc
import pytest

from personaforge.worker.v1 import child_control_pb2, child_control_pb2_grpc
from personaforge_chat.domain.models import AvatarState
from personaforge_worker.child import runtime as child_runtime_module
from personaforge_worker.child.runtime import CompositeChildRuntime
from personaforge_worker.ipc.messages import (
    PROTOCOL_VERSION,
    command_message,
    message_meta,
)
from personaforge_worker.master.child_server import (
    ChildConnection,
    ChildControlServer,
    ChildControlService,
)
from personaforge_worker.settings import WorkerSettings


class FakeLiveKitRenderSession:
    instances: list["FakeLiveKitRenderSession"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.states: list[tuple[AvatarState, int]] = []
        self.speech: list[dict[str, Any]] = []
        self._stopped = asyncio.Event()
        self.instances.append(self)

    async def run(self) -> None:
        await self._stopped.wait()

    async def wait_ready(self, timeout_seconds: float = 15) -> None:
        del timeout_seconds

    def set_state(self, state: AvatarState, state_version: int) -> None:
        self.states.append((state, state_version))

    async def speak(self, **kwargs: Any) -> None:
        self.speech.append(kwargs)
        await asyncio.sleep(0)

    async def stop(self) -> None:
        self._stopped.set()


@pytest.mark.asyncio
async def test_master_routes_each_child_to_its_own_grpc_stream(tmp_path: Any) -> None:
    socket_path = tmp_path / "routing.sock"

    async def ignore_event(child: ChildConnection, event: Any) -> None:
        del child, event

    service = ChildControlService(ignore_event)
    server = ChildControlServer(socket_path, service)
    child_a = _connection("a")
    child_b = _connection("b")
    await service.register(child_a)
    await service.register(child_b)
    await server.start()
    channel_a = grpc.aio.insecure_channel(f"unix:{socket_path}")
    channel_b = grpc.aio.insecure_channel(f"unix:{socket_path}")
    events_a: asyncio.Queue[Any] = asyncio.Queue()
    events_b: asyncio.Queue[Any] = asyncio.Queue()
    call_a = child_control_pb2_grpc.CompositeChildControlStub(channel_a).Connect(
        _hello_stream(child_a, events_a)
    )
    call_b = child_control_pb2_grpc.CompositeChildControlStub(channel_b).Connect(
        _hello_stream(child_b, events_b)
    )
    try:
        await asyncio.wait_for(child_a.connected.wait(), timeout=3)
        await asyncio.wait_for(child_b.connected.wait(), timeout=3)
        await child_a.outbound.put(
            command_message(
                _start_command_for("a"),
                worker_id=child_a.worker_id,
                child_id=child_a.child_id,
                gpu_uuid=child_a.gpu_uuid,
                sequence=child_a.next_sequence(),
            )
        )
        await child_b.outbound.put(
            command_message(
                _start_command_for("b"),
                worker_id=child_b.worker_id,
                child_id=child_b.child_id,
                gpu_uuid=child_b.gpu_uuid,
                sequence=child_b.next_sequence(),
            )
        )

        received_a = await asyncio.wait_for(call_a.read(), timeout=3)
        received_b = await asyncio.wait_for(call_b.read(), timeout=3)

        assert received_a.meta.child_id == "child-a"
        assert received_a.meta.session_id == "session-a"
        assert received_b.meta.child_id == "child-b"
        assert received_b.meta.session_id == "session-b"
    finally:
        call_a.cancel()
        call_b.cancel()
        await events_a.put(None)
        await events_b.put(None)
        await channel_a.close()
        await channel_b.close()
        await server.stop(0)


@pytest.mark.asyncio
async def test_child_runs_session_commands_over_grpc_uds(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket_path = tmp_path / "master.sock"
    token_path = tmp_path / "child.token"
    token_path.write_text("one-time-token")
    monkeypatch.setenv("PERSONA_MASTER_SOCKET", str(socket_path))
    monkeypatch.setenv("PERSONA_CHILD_ID", "child-1")
    monkeypatch.setenv("PERSONA_SESSION_ID", "session-1")
    monkeypatch.setenv("PERSONA_ASSIGNED_GPU_UUID", "GPU-test")
    monkeypatch.setenv("PERSONA_CHILD_BOOTSTRAP_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("PERSONA_PARENT_WORKER_ID", "worker-1")
    monkeypatch.setenv("PERSONA_CHILD_LEASE_TOKEN", "lease-1")
    monkeypatch.setenv("PERSONA_CHILD_GENERATION", "7")
    monkeypatch.setattr(
        child_runtime_module,
        "LiveKitRenderSession",
        FakeLiveKitRenderSession,
    )
    FakeLiveKitRenderSession.instances.clear()

    observed: list[str] = []
    observed_events: dict[str, asyncio.Event] = {}
    service: ChildControlService

    async def on_event(child: ChildConnection, event: Any) -> None:
        payload = event.WhichOneof("payload")
        observed.append(payload)
        observed_events.setdefault(payload, asyncio.Event()).set()
        if payload == "stopped":
            await service.unregister(child.child_id)

    service = ChildControlService(on_event)
    server = ChildControlServer(socket_path, service)
    child = ChildConnection(
        worker_id="worker-1",
        child_id="child-1",
        session_id="session-1",
        gpu_uuid="GPU-test",
        lease_token="lease-1",
        generation=7,
        bootstrap_token="one-time-token",
        queue_size=20,
    )
    await service.register(child)
    await server.start()
    runtime = CompositeChildRuntime(WorkerSettings.from_env())
    runtime_task = asyncio.create_task(runtime.run())
    try:
        await asyncio.wait_for(child.connected.wait(), timeout=3)
        await _send(child, _start_command())
        await _wait_event(observed_events, "ready")

        await _send(child, _state_command())
        await _send(child, _speech_command())
        await _wait_event(observed_events, "playback_completed")

        await _send(child, _stop_command())
        await _wait_event(observed_events, "stopped")
        await asyncio.wait_for(runtime_task, timeout=3)
    finally:
        if not runtime_task.done():
            runtime_task.cancel()
            await asyncio.gather(runtime_task, return_exceptions=True)
        await server.stop(0)

    assert token_path.exists() is False
    assert "accepted" in observed
    session = FakeLiveKitRenderSession.instances[0]
    assert session.kwargs["session_id"] == "session-1"
    assert session.states == [(AvatarState.THINKING, 2)]
    assert session.speech[0]["utterance_id"] == "utterance-1"


async def _send(child: ChildConnection, command: dict[str, Any]) -> None:
    command_id = str(command["command_id"])
    accepted = child.accepted_future(command_id)
    child.commands[command_id] = command
    await child.outbound.put(
        command_message(
            command,
            worker_id=child.worker_id,
            child_id=child.child_id,
            gpu_uuid=child.gpu_uuid,
            sequence=child.next_sequence(),
        )
    )
    await asyncio.wait_for(accepted, timeout=3)


async def _wait_event(events: dict[str, asyncio.Event], name: str) -> None:
    for _ in range(100):
        event = events.get(name)
        if event is not None:
            await asyncio.wait_for(event.wait(), timeout=3)
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"Child event was not observed: {name}")


def _base_command(command_id: str, command_type: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command_id": command_id,
        "type": command_type,
        "session_id": "session-1",
        "worker_id": "worker-1",
        "lease_token": "lease-1",
        "generation": 7,
        "slot_id": "slot-1",
    }


def _start_command() -> dict[str, Any]:
    command = _base_command("command-start", "avatar.session.start")
    command["payload"] = {
        "character_id": "default",
        "livekit_url": "ws://livekit.test",
        "publisher_token": "publisher-token",
    }
    return command


def _state_command() -> dict[str, Any]:
    command = _base_command("command-state", "avatar.state.set")
    command["payload"] = {"state": "thinking", "state_version": 2}
    return command


def _speech_command() -> dict[str, Any]:
    command = _base_command("command-speech", "avatar.speech.synthesize")
    command["payload"] = {
        "turn_id": "turn-1",
        "utterance_id": "utterance-1",
        "text": "xin chao",
        "sequence": 1,
        "is_final": True,
    }
    return command


def _stop_command() -> dict[str, Any]:
    command = _base_command("command-stop", "avatar.session.stop")
    command["payload"] = {}
    return command


def _connection(suffix: str) -> ChildConnection:
    return ChildConnection(
        worker_id="worker-1",
        child_id=f"child-{suffix}",
        session_id=f"session-{suffix}",
        gpu_uuid="GPU-test",
        lease_token=f"lease-{suffix}",
        generation=1,
        bootstrap_token=f"token-{suffix}",
        queue_size=10,
    )


async def _hello_stream(child: ChildConnection, queue: asyncio.Queue[Any]) -> Any:
    hello = child_control_pb2.ChildToMaster(
        meta=message_meta(
            message_id=f"hello-{child.child_id}",
            worker_id=child.worker_id,
            child_id=child.child_id,
            session_id=child.session_id,
            gpu_uuid=child.gpu_uuid,
            lease_token=child.lease_token,
            generation=child.generation,
            sequence=1,
        ),
        hello=child_control_pb2.ChildHello(
            pid=100,
            process_create_time=1,
            protocol_version=PROTOCOL_VERSION,
            bootstrap_token=f"token-{child.child_id.removeprefix('child-')}",
        ),
    )
    yield hello
    while True:
        event = await queue.get()
        if event is None:
            return
        yield event


def _start_command_for(suffix: str) -> dict[str, Any]:
    command = {
        **_start_command(),
        "command_id": f"command-{suffix}",
        "session_id": f"session-{suffix}",
        "lease_token": f"lease-{suffix}",
        "generation": 1,
    }
    return command
