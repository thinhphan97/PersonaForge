import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import grpc

from personaforge.worker.v1 import child_control_pb2, child_control_pb2_grpc
from personaforge_worker.ipc.messages import PROTOCOL_VERSION, validate_meta

logger = logging.getLogger(__name__)

ChildEventHandler = Callable[
    ["ChildConnection", child_control_pb2.ChildToMaster], Awaitable[None]
]


@dataclass(slots=True)
class ChildConnection:
    worker_id: str
    child_id: str
    session_id: str
    gpu_uuid: str
    lease_token: str
    generation: int
    bootstrap_token: str
    queue_size: int
    reserved_vram_mb: int = 0
    reserved_gpu_duty_cycle: float = 0
    process: asyncio.subprocess.Process | None = None
    process_create_time: float = 0
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: asyncio.Event = field(default_factory=asyncio.Event)
    outbound: asyncio.Queue[child_control_pb2.MasterToChild | None] = field(init=False)
    accepted: dict[str, asyncio.Future[None]] = field(default_factory=dict)
    commands: dict[str, dict[str, Any]] = field(default_factory=dict)
    lifecycle_command: dict[str, Any] | None = None
    last_heartbeat_monotonic: float = field(default_factory=time.monotonic)
    sequence: int = 0
    last_event_sequence: int = 0
    stream_active: bool = False
    token_consumed: bool = False
    runtime_state: str = "starting"
    livekit_connected: bool = False
    observed_fps: float = 0
    av_drift_ms: float = 0
    last_command_sequence: int = 0

    def __post_init__(self) -> None:
        self.outbound = asyncio.Queue(maxsize=self.queue_size)

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    def accepted_future(self, command_id: str) -> asyncio.Future[None]:
        future = asyncio.get_running_loop().create_future()
        self.accepted[command_id] = future
        return future

    def resolve_accepted(self, command_id: str) -> None:
        future = self.accepted.pop(command_id, None)
        if future is not None and not future.done():
            future.set_result(None)

    def fail_waiters(self, error: BaseException) -> None:
        for future in self.accepted.values():
            if not future.done():
                future.set_exception(error)
        self.accepted.clear()


class ChildControlService(child_control_pb2_grpc.CompositeChildControlServicer):
    def __init__(self, event_handler: ChildEventHandler) -> None:
        self._event_handler = event_handler
        self._expected: dict[str, ChildConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, child: ChildConnection) -> None:
        async with self._lock:
            if child.child_id in self._expected:
                raise RuntimeError(f"Child is already registered: {child.child_id}")
            self._expected[child.child_id] = child

    async def unregister(self, child_id: str) -> None:
        async with self._lock:
            child = self._expected.pop(child_id, None)
        if child is not None:
            child.fail_waiters(RuntimeError("Child connection was unregistered"))
            await _offer_sentinel(child.outbound)

    async def Connect(self, request_iterator: Any, context: Any) -> Any:
        child: ChildConnection | None = None
        reader: asyncio.Task[None] | None = None
        try:
            first = await anext(request_iterator)
            if first.WhichOneof("payload") != "hello":
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "ChildHello is required")
            child = await self._authenticate(first, context)
            reader = asyncio.create_task(
                self._read_events(child, request_iterator),
                name=f"child-events-{child.child_id}",
            )
            while True:
                command = await child.outbound.get()
                if command is None:
                    return
                yield command
        except StopAsyncIteration:
            return
        finally:
            if reader is not None:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            if child is not None:
                child.stream_active = False
                child.fail_waiters(RuntimeError("Child gRPC stream disconnected"))

    async def _authenticate(
        self,
        first: child_control_pb2.ChildToMaster,
        context: Any,
    ) -> ChildConnection:
        child_id = first.meta.child_id
        async with self._lock:
            child = self._expected.get(child_id)
            if child is None:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "Unknown child")
            assert child is not None
            try:
                validate_meta(
                    first.meta,
                    worker_id=child.worker_id,
                    child_id=child.child_id,
                    session_id=child.session_id,
                    gpu_uuid=child.gpu_uuid,
                    lease_token=child.lease_token,
                    generation=child.generation,
                )
            except ValueError as exc:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, str(exc))
            hello = first.hello
            if hello.protocol_version != PROTOCOL_VERSION:
                await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "Protocol mismatch")
            if child.token_consumed or hello.bootstrap_token != child.bootstrap_token:
                await context.abort(grpc.StatusCode.UNAUTHENTICATED, "Invalid bootstrap token")
            if child.process is not None and hello.pid != child.process.pid:
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, "Child PID mismatch")
            if child.stream_active:
                await context.abort(grpc.StatusCode.ALREADY_EXISTS, "Child already connected")
            child.token_consumed = True
            child.bootstrap_token = ""
            child.process_create_time = hello.process_create_time
            child.stream_active = True
            child.last_event_sequence = first.meta.sequence
            child.last_heartbeat_monotonic = time.monotonic()
            child.connected.set()
            return child

    async def _read_events(self, child: ChildConnection, request_iterator: Any) -> None:
        async for event in request_iterator:
            validate_meta(
                event.meta,
                worker_id=child.worker_id,
                child_id=child.child_id,
                session_id=child.session_id,
                gpu_uuid=child.gpu_uuid,
                lease_token=child.lease_token,
                generation=child.generation,
            )
            payload_name = event.WhichOneof("payload")
            if payload_name == "hello" or payload_name is None:
                raise ValueError("Unexpected child event payload")
            if event.meta.sequence <= child.last_event_sequence:
                raise ValueError("Child event sequence is duplicate or out of order")
            child.last_event_sequence = event.meta.sequence
            if payload_name == "heartbeat":
                child.last_heartbeat_monotonic = time.monotonic()
                child.runtime_state = event.heartbeat.state
                child.livekit_connected = event.heartbeat.livekit_connected
                child.observed_fps = event.heartbeat.fps
                child.av_drift_ms = event.heartbeat.av_drift_ms
                child.last_command_sequence = event.heartbeat.last_command_sequence
            elif payload_name == "accepted":
                child.resolve_accepted(event.accepted.command_id)
            elif payload_name == "stopped":
                child.stopped.set()
            await self._event_handler(child, event)


class ChildControlServer:
    def __init__(self, socket_path: Path, service: ChildControlService) -> None:
        self._socket_path = socket_path
        self._service = service
        self._server = grpc.aio.server()
        child_control_pb2_grpc.add_CompositeChildControlServicer_to_server(
            service,
            self._server,
        )

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._socket_path.parent.chmod(0o700)
        if self._socket_path.exists() or self._socket_path.is_socket():
            self._socket_path.unlink()
        bound = self._server.add_insecure_port(f"unix:{self._socket_path}")
        if bound == 0:
            raise RuntimeError(f"Could not bind child control socket: {self._socket_path}")
        await self._server.start()
        logger.info("Child control server listening socket=%s", self._socket_path)

    async def stop(self, grace_seconds: float = 3) -> None:
        await self._server.stop(grace_seconds)
        if self._socket_path.exists() or self._socket_path.is_socket():
            self._socket_path.unlink()


async def _offer_sentinel(
    queue: asyncio.Queue[child_control_pb2.MasterToChild | None],
) -> None:
    try:
        queue.put_nowait(None)
    except asyncio.QueueFull:
        await queue.put(None)
