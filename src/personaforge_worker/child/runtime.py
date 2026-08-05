import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc

from personaforge.worker.v1 import child_control_pb2, child_control_pb2_grpc
from personaforge_chat.domain.models import AvatarState
from personaforge_worker.ipc.messages import (
    PROTOCOL_VERSION,
    decode_command,
    message_meta,
    required_bool,
    required_int,
    required_mapping,
    required_string,
    validate_meta,
)
from personaforge_worker.livekit_session import LiveKitRenderSession
from personaforge_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)


class CompositeChildRuntime:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._worker_id = _required_env("PERSONA_PARENT_WORKER_ID")
        self._child_id = _required_env("PERSONA_CHILD_ID")
        self._session_id = _required_env("PERSONA_SESSION_ID")
        self._gpu_uuid = _required_env("PERSONA_ASSIGNED_GPU_UUID")
        self._lease_token = _required_env("PERSONA_CHILD_LEASE_TOKEN")
        self._generation = int(_required_env("PERSONA_CHILD_GENERATION"))
        self._bootstrap_token = _read_bootstrap_token()
        self._events: asyncio.Queue[child_control_pb2.ChildToMaster | None] = asyncio.Queue(
            maxsize=settings.child_command_queue_size
        )
        self._commands: asyncio.PriorityQueue[
            tuple[int, int, child_control_pb2.MasterToChild]
        ] = asyncio.PriorityQueue(maxsize=settings.child_command_queue_size)
        self._finished = asyncio.Event()
        self._session: LiveKitRenderSession | None = None
        self._session_task: asyncio.Task[None] | None = None
        self._state = "starting"
        self._last_command_sequence = 0
        self._last_received_sequence = 0
        self._event_sequence = 0

    async def run(self) -> None:
        channel = grpc.aio.insecure_channel(f"unix:{self._settings.master_socket}")
        stub = child_control_pb2_grpc.CompositeChildControlStub(channel)
        actor = asyncio.create_task(self._actor(), name="composite-child-actor")
        heartbeat = asyncio.create_task(self._heartbeat(), name="composite-child-heartbeat")
        try:
            call = stub.Connect(self._event_stream())
            async for command in call:
                self._validate_command(command)
                await self._commands.put(
                    (_command_priority(command), command.meta.sequence, command)
                )
                await self._accepted(command.meta.message_id)
                if command.WhichOneof("payload") == "stop":
                    await self._finished.wait()
            if not self._finished.is_set():
                raise RuntimeError("Worker Master closed the child control stream")
        finally:
            heartbeat.cancel()
            actor.cancel()
            await asyncio.gather(heartbeat, actor, return_exceptions=True)
            await self._cleanup_session()
            await self._events.put(None)
            await channel.close()

    async def _actor(self) -> None:
        while True:
            _priority, _sequence, message = await self._commands.get()
            command = decode_command(message)
            self._last_command_sequence = message.meta.sequence
            try:
                await self._execute(message, command)
            except Exception as exc:
                logger.exception(
                    "Composite Child command failed child=%s command=%s",
                    self._child_id,
                    message.meta.message_id,
                )
                await self._failed(message.meta.message_id, str(exc))
                self._finished.set()
                return

    async def _execute(
        self,
        message: child_control_pb2.MasterToChild,
        command: dict[str, Any],
    ) -> None:
        payload_name = message.WhichOneof("payload")
        if payload_name == "start":
            await self._start(command, message.meta.message_id)
            await self._ready(message.meta.message_id)
        elif payload_name == "change_state":
            self._change_state(command)
        elif payload_name == "speech":
            utterance_id = await self._speak(command)
            await self._playback_completed(message.meta.message_id, utterance_id)
        elif payload_name == "stop":
            await self._cleanup_session()
            self._state = "stopped"
            await self._stopped(message.meta.message_id)
            self._finished.set()
        else:
            raise ValueError(f"Unsupported child command payload: {payload_name}")

    async def _start(self, command: dict[str, Any], command_id: str) -> None:
        if self._session is not None:
            return
        payload = required_mapping(command, "payload")
        session = LiveKitRenderSession(
            session_id=self._session_id,
            character_id=required_string(payload, "character_id"),
            livekit_url=required_string(payload, "livekit_url"),
            publisher_token=required_string(payload, "publisher_token"),
            settings=self._settings,
        )
        self._session = session
        self._session_task = asyncio.create_task(
            session.run(),
            name=f"child-render-{self._session_id}",
        )
        self._session_task.add_done_callback(
            lambda task: self._session_done(task, command_id)
        )
        await session.wait_ready()
        self._state = "idle"

    def _change_state(self, command: dict[str, Any]) -> None:
        session = self._require_session()
        payload = required_mapping(command, "payload")
        state = AvatarState(required_string(payload, "state"))
        session.set_state(state, required_int(payload, "state_version"))
        self._state = state.value

    async def _speak(self, command: dict[str, Any]) -> str:
        session = self._require_session()
        payload = required_mapping(command, "payload")
        utterance_id = required_string(payload, "utterance_id")
        self._state = "speaking"
        await session.speak(
            turn_id=required_string(payload, "turn_id"),
            utterance_id=utterance_id,
            text=required_string(payload, "text"),
            sequence=required_int(payload, "sequence"),
            is_final=required_bool(payload, "is_final"),
        )
        return utterance_id

    async def _cleanup_session(self) -> None:
        session = self._session
        task = self._session_task
        self._session = None
        self._session_task = None
        if session is not None:
            await session.stop()
        if task is not None:
            try:
                await asyncio.wait_for(
                    task,
                    timeout=self._settings.child_stop_grace_seconds,
                )
            except TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self._settings.child_heartbeat_interval_seconds)
            event = self._event(message_id=uuid4().hex)
            event.heartbeat.CopyFrom(
                child_control_pb2.ChildHeartbeat(
                    state=self._state,
                    pid=os.getpid(),
                    last_command_sequence=self._last_command_sequence,
                    livekit_connected=self._session is not None,
                    fps=self._settings.video_fps,
                )
            )
            await self._events.put(event)

    async def _event_stream(self) -> Any:
        hello = self._event(message_id=uuid4().hex)
        hello.hello.CopyFrom(
            child_control_pb2.ChildHello(
                pid=os.getpid(),
                process_create_time=_process_create_time(),
                protocol_version=PROTOCOL_VERSION,
                bootstrap_token=self._bootstrap_token,
            )
        )
        self._bootstrap_token = ""
        yield hello
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def _accepted(self, command_id: str) -> None:
        event = self._event(message_id=command_id)
        event.accepted.command_id = command_id
        await self._events.put(event)

    async def _ready(self, command_id: str) -> None:
        event = self._event(message_id=command_id)
        event.ready.command_id = command_id
        await self._events.put(event)

    async def _playback_completed(self, command_id: str, utterance_id: str) -> None:
        event = self._event(message_id=command_id)
        event.playback_completed.command_id = command_id
        event.playback_completed.utterance_id = utterance_id
        await self._events.put(event)

    async def _failed(self, command_id: str, detail: str) -> None:
        event = self._event(message_id=command_id)
        event.failed.command_id = command_id
        event.failed.detail = detail
        await self._events.put(event)

    async def _stopped(self, command_id: str) -> None:
        event = self._event(message_id=command_id)
        event.stopped.command_id = command_id
        await self._events.put(event)

    def _event(self, message_id: str) -> child_control_pb2.ChildToMaster:
        self._event_sequence += 1
        return child_control_pb2.ChildToMaster(
            meta=message_meta(
                message_id=message_id,
                worker_id=self._worker_id,
                child_id=self._child_id,
                session_id=self._session_id,
                gpu_uuid=self._gpu_uuid,
                lease_token=self._lease_token,
                generation=self._generation,
                sequence=self._event_sequence,
            )
        )

    def _validate_command(self, message: child_control_pb2.MasterToChild) -> None:
        validate_meta(
            message.meta,
            worker_id=self._worker_id,
            child_id=self._child_id,
            session_id=self._session_id,
            gpu_uuid=self._gpu_uuid,
            lease_token=self._lease_token,
            generation=self._generation,
        )
        if message.meta.sequence != self._last_received_sequence + 1:
            raise ValueError("Master command sequence is missing, duplicate or out of order")
        self._last_received_sequence = message.meta.sequence

    def _require_session(self) -> LiveKitRenderSession:
        if self._session is None:
            raise RuntimeError("Composite session has not started")
        return self._session

    def _session_done(self, task: asyncio.Task[None], command_id: str) -> None:
        if self._session_task is not task or self._finished.is_set():
            return
        if task.cancelled():
            detail = "LiveKit render session was cancelled unexpectedly"
        elif task.exception() is not None:
            detail = f"LiveKit render session crashed: {task.exception()}"
        else:
            detail = "LiveKit render session stopped unexpectedly"
        asyncio.create_task(
            self._fail_runtime(command_id, detail),
            name=f"child-session-failed-{self._session_id}",
        )

    async def _fail_runtime(self, command_id: str, detail: str) -> None:
        if self._finished.is_set():
            return
        await self._failed(command_id, detail)
        self._finished.set()


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required for Composite Child")
    return value


def _read_bootstrap_token() -> str:
    path = Path(_required_env("PERSONA_CHILD_BOOTSTRAP_TOKEN_FILE"))
    token = path.read_text().strip()
    path.unlink(missing_ok=True)
    if not token:
        raise RuntimeError("Composite Child bootstrap token is empty")
    return token


def _process_create_time() -> float:
    try:
        from nvitop import HostProcess

        value = HostProcess().create_time()
        return float(value)
    except (ImportError, OSError, TypeError, ValueError):
        return time.time()


def _command_priority(message: child_control_pb2.MasterToChild) -> int:
    payload = message.WhichOneof("payload")
    if payload in {"stop", "cancel", "drain"}:
        return 0
    if payload in {"start", "change_state"}:
        return 1
    return 2
