import asyncio
import logging
import os
import secrets
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from personaforge.worker.v1 import child_control_pb2
from personaforge_worker.ipc.messages import (
    command_message,
    optional_float,
    optional_int,
    optional_string,
    required_string,
)
from personaforge_worker.master.child_server import ChildConnection, ChildControlService
from personaforge_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)

SupervisorEventHandler = Callable[
    [dict[str, Any], str, str | None, str | None], Awaitable[None]
]


class ChildSupervisor:
    def __init__(
        self,
        settings: WorkerSettings,
        worker_id: str,
        default_gpu_uuid: str,
        service: ChildControlService,
        event_handler: SupervisorEventHandler,
    ) -> None:
        self._settings = settings
        self._worker_id = worker_id
        self._default_gpu_uuid = default_gpu_uuid
        self._service = service
        self._event_handler = event_handler
        self._children: dict[str, ChildConnection] = {}
        self._highest_generation: dict[str, int] = {}
        self._process_monitors: dict[str, asyncio.Task[None]] = {}
        self._heartbeat_monitor: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start_monitoring(self) -> None:
        if self._heartbeat_monitor is None:
            self._heartbeat_monitor = asyncio.create_task(
                self._monitor_heartbeats(),
                name="child-heartbeat-monitor",
            )

    async def start(self, command: dict[str, Any]) -> None:
        session_id = required_string(command, "session_id")
        generation = optional_int(command, "generation", 0)
        lease_token = optional_string(command, "lease_token", "legacy")
        async with self._lock:
            if generation < self._highest_generation.get(session_id, -1):
                raise RuntimeError(f"Stale render generation for session: {session_id}")
            current = self._children.get(session_id)
            if current is not None:
                if current.generation == generation and current.lease_token == lease_token:
                    await self._send_and_wait_accepted(current, command)
                    return
                await self._stop_child(current, send_command=None)
            local_limit = self._settings.master_max_children
            if len(self._children) >= local_limit:
                raise RuntimeError("Worker Master child capacity is full")
            self._assert_local_capacity(command)
            child = self._new_child(command)
            self._children[session_id] = child
            self._highest_generation[session_id] = max(
                generation,
                self._highest_generation.get(session_id, generation),
            )
            await self._service.register(child)
            try:
                await self._spawn(child)
            except BaseException:
                self._children.pop(session_id, None)
                await self._service.unregister(child.child_id)
                raise
        try:
            await asyncio.wait_for(
                child.connected.wait(),
                timeout=self._settings.child_handshake_timeout_seconds,
            )
            await self._send_and_wait_accepted(child, command)
        except BaseException:
            await self._remove_and_terminate(child)
            raise

    async def dispatch(self, command: dict[str, Any]) -> None:
        session_id = required_string(command, "session_id")
        child = self._require_child(session_id)
        self._require_owner(child, command)
        await self._send_and_wait_accepted(child, command)

    async def stop(self, command: dict[str, Any]) -> None:
        session_id = required_string(command, "session_id")
        child = self._children.get(session_id)
        if child is None:
            return
        self._require_owner(child, command)
        await self._stop_child(child, send_command=command)

    async def on_child_event(
        self,
        child: ChildConnection,
        event: child_control_pb2.ChildToMaster,
    ) -> None:
        payload_name = event.WhichOneof("payload")
        if payload_name in {None, "hello", "heartbeat", "accepted"}:
            return
        command_id = _event_command_id(event, payload_name)
        command = child.commands.get(command_id)
        if command is None and payload_name == "failed":
            command = child.lifecycle_command
        if command is None:
            logger.warning(
                "Child event has no command context child=%s type=%s command=%s",
                child.child_id,
                payload_name,
                command_id,
            )
            return
        if payload_name == "ready":
            child.commands.pop(command_id, None)
            await self._event_handler(command, "avatar.session.ready", None, None)
        elif payload_name == "playback_completed":
            child.commands.pop(command_id, None)
            await self._event_handler(
                command,
                "avatar.speech.completed",
                None,
                event.playback_completed.utterance_id,
            )
        elif payload_name == "failed":
            child.commands.pop(command_id, None)
            await self._event_handler(
                command,
                "avatar.session.failed",
                event.failed.detail,
                None,
            )
            asyncio.create_task(
                self._remove_and_terminate(child),
                name=f"terminate-failed-{child.child_id}",
            )
        elif payload_name == "stopped":
            child.commands.pop(command_id, None)
            await self._event_handler(command, "avatar.session.stopped", None, None)

    async def shutdown(self) -> None:
        if self._heartbeat_monitor is not None:
            self._heartbeat_monitor.cancel()
            await asyncio.gather(self._heartbeat_monitor, return_exceptions=True)
            self._heartbeat_monitor = None
        for child in tuple(self._children.values()):
            await self._stop_child(child, send_command=None)

    @property
    def active_sessions(self) -> int:
        return len(self._children)

    @property
    def reserved_vram_mb(self) -> int:
        return sum(child.reserved_vram_mb for child in self._children.values())

    @property
    def reserved_compute(self) -> float:
        return sum(child.reserved_gpu_duty_cycle for child in self._children.values())

    @property
    def children(self) -> tuple[ChildConnection, ...]:
        return tuple(self._children.values())

    def _new_child(self, command: dict[str, Any]) -> ChildConnection:
        return ChildConnection(
            worker_id=self._worker_id,
            child_id=f"child-{uuid4().hex[:12]}",
            session_id=required_string(command, "session_id"),
            gpu_uuid=optional_string(command, "gpu_uuid", self._default_gpu_uuid),
            lease_token=optional_string(command, "lease_token", "legacy"),
            generation=optional_int(command, "generation", 0),
            bootstrap_token=secrets.token_urlsafe(32),
            queue_size=self._settings.child_command_queue_size,
            reserved_vram_mb=optional_int(command, "reserved_vram_mb", 0),
            reserved_gpu_duty_cycle=optional_float(
                command,
                "reserved_gpu_duty_cycle",
                0,
            ),
            lifecycle_command=command,
        )

    async def _spawn(self, child: ChildConnection) -> None:
        bootstrap_file = self._bootstrap_file(child)
        bootstrap_file.write_text(child.bootstrap_token)
        bootstrap_file.chmod(0o600)
        environment = os.environ.copy()
        environment.update(
            {
                "PERSONA_WORKER_ROLE": "child",
                "PERSONA_CHILD_ID": child.child_id,
                "PERSONA_SESSION_ID": child.session_id,
                "PERSONA_ASSIGNED_GPU_UUID": child.gpu_uuid,
                "PERSONA_CHILD_BOOTSTRAP_TOKEN_FILE": str(bootstrap_file),
                "PERSONA_PARENT_WORKER_ID": child.worker_id,
                "PERSONA_CHILD_LEASE_TOKEN": child.lease_token,
                "PERSONA_CHILD_GENERATION": str(child.generation),
            }
        )
        if child.gpu_uuid and child.gpu_uuid != "GPU-none":
            environment["CUDA_VISIBLE_DEVICES"] = child.gpu_uuid
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "personaforge_worker.child.main",
            env=environment,
        )
        child.process = process
        monitor = asyncio.create_task(
            self._monitor_process(child),
            name=f"child-process-{child.child_id}",
        )
        self._process_monitors[child.child_id] = monitor
        logger.info(
            "Spawned composite child child=%s session=%s pid=%s gpu=%s",
            child.child_id,
            child.session_id,
            process.pid,
            child.gpu_uuid,
        )

    async def _send_and_wait_accepted(
        self,
        child: ChildConnection,
        command: dict[str, Any],
    ) -> None:
        if not child.stream_active:
            raise RuntimeError("Composite Child is not connected")
        command_id = required_string(command, "command_id")
        future = child.accepted_future(command_id)
        child.commands[command_id] = command
        message = command_message(
            command,
            worker_id=child.worker_id,
            child_id=child.child_id,
            gpu_uuid=child.gpu_uuid,
            sequence=child.next_sequence(),
        )
        try:
            await asyncio.wait_for(
                child.outbound.put(message),
                timeout=self._settings.child_heartbeat_timeout_seconds,
            )
            await asyncio.wait_for(
                future,
                timeout=self._settings.child_heartbeat_timeout_seconds,
            )
            if command.get("type") == "avatar.state.set":
                child.commands.pop(command_id, None)
        except BaseException:
            child.accepted.pop(command_id, None)
            child.commands.pop(command_id, None)
            raise

    async def _stop_child(
        self,
        child: ChildConnection,
        send_command: dict[str, Any] | None,
    ) -> None:
        if send_command is not None and child.stream_active:
            try:
                await self._send_and_wait_accepted(child, send_command)
                await asyncio.wait_for(
                    child.stopped.wait(),
                    timeout=self._settings.child_stop_grace_seconds,
                )
            except (TimeoutError, RuntimeError):
                logger.warning("Child did not stop gracefully child=%s", child.child_id)
        await self._remove_and_terminate(child)

    async def _remove_and_terminate(self, child: ChildConnection) -> None:
        self._children.pop(child.session_id, None)
        await self._service.unregister(child.child_id)
        process = child.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self._settings.child_stop_grace_seconds,
                )
            except TimeoutError:
                process.kill()
                await process.wait()
        bootstrap_file = self._bootstrap_file(child)
        if bootstrap_file.exists():
            bootstrap_file.unlink()

    async def _monitor_process(self, child: ChildConnection) -> None:
        assert child.process is not None
        return_code = await child.process.wait()
        self._process_monitors.pop(child.child_id, None)
        current = self._children.get(child.session_id)
        if current is not child:
            return
        self._children.pop(child.session_id, None)
        await self._service.unregister(child.child_id)
        command = _latest_command(child)
        if command is not None and not child.stopped.is_set():
            await self._event_handler(
                command,
                "avatar.session.failed",
                f"Composite Child exited with code {return_code}",
                None,
            )

    async def _monitor_heartbeats(self) -> None:
        interval = max(0.25, self._settings.child_heartbeat_interval_seconds)
        while True:
            await asyncio.sleep(interval)
            now = asyncio.get_running_loop().time()
            timed_out = [
                child
                for child in self._children.values()
                if child.stream_active
                and now - child.last_heartbeat_monotonic
                > self._settings.child_heartbeat_timeout_seconds
            ]
            for child in timed_out:
                logger.error("Composite Child heartbeat timed out child=%s", child.child_id)
                command = _latest_command(child)
                await self._remove_and_terminate(child)
                if command is not None:
                    await self._event_handler(
                        command,
                        "avatar.session.failed",
                        "Composite Child heartbeat timed out",
                        None,
                    )

    def _require_child(self, session_id: str) -> ChildConnection:
        child = self._children.get(session_id)
        if child is None:
            raise RuntimeError(f"Composite Child not found: {session_id}")
        return child

    @staticmethod
    def _require_owner(child: ChildConnection, command: dict[str, Any]) -> None:
        if (
            optional_int(command, "generation", 0) != child.generation
            or optional_string(command, "lease_token", "legacy") != child.lease_token
        ):
            raise RuntimeError("Render command does not own the active Composite Child")

    def _bootstrap_file(self, child: ChildConnection) -> Path:
        return self._settings.master_socket.parent / f"{child.child_id}.token"

    def _assert_local_capacity(self, command: dict[str, Any]) -> None:
        gpu_uuid = optional_string(command, "gpu_uuid", self._default_gpu_uuid)
        requested_vram = optional_int(command, "reserved_vram_mb", 0)
        requested_compute = optional_float(command, "reserved_gpu_duty_cycle", 0)
        gpu_children = tuple(
            child for child in self._children.values() if child.gpu_uuid == gpu_uuid
        )
        if len(gpu_children) >= self._settings.maximum_slots:
            raise RuntimeError("Worker Master observed insufficient GPU slot capacity")
        reserved_vram = sum(child.reserved_vram_mb for child in gpu_children)
        reserved_compute = sum(child.reserved_gpu_duty_cycle for child in gpu_children)
        if (
            self._settings.schedulable_vram_mb > 0
            and reserved_vram + requested_vram
            > self._settings.schedulable_vram_mb
        ):
            raise RuntimeError("Worker Master observed insufficient reserved VRAM capacity")
        if reserved_compute + requested_compute > self._settings.compute_budget:
            raise RuntimeError("Worker Master observed insufficient compute capacity")


def _event_command_id(event: child_control_pb2.ChildToMaster, payload_name: str) -> str:
    payload = getattr(event, payload_name)
    command_id = getattr(payload, "command_id", "")
    return command_id or event.meta.message_id


def _latest_command(child: ChildConnection) -> dict[str, Any] | None:
    if not child.commands:
        return child.lifecycle_command
    command_id = next(reversed(child.commands))
    return child.commands[command_id]
