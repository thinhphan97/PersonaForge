import asyncio
import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from avatar_chat.domain.models import AvatarState
from avatar_worker.livekit_session import LiveKitRenderSession
from avatar_worker.settings import WorkerSettings

logger = logging.getLogger(__name__)


class RenderSessionManager:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._sessions: dict[str, tuple[LiveKitRenderSession, asyncio.Task[None]]] = {}

    async def start(self, command: dict[str, Any]) -> None:
        session_id = _required_string(command, "session_id")
        payload = _required_mapping(command, "payload")
        await self.stop(session_id)
        session = LiveKitRenderSession(
            session_id=session_id,
            livekit_url=_required_string(payload, "livekit_url"),
            publisher_token=_required_string(payload, "publisher_token"),
            settings=self._settings,
        )
        task = asyncio.create_task(session.run(), name=f"render-{session_id}")
        self._sessions[session_id] = (session, task)

        def on_done(completed: asyncio.Task[None]) -> None:
            self._session_done(session_id, completed)

        task.add_done_callback(on_done)
        try:
            await session.wait_ready()
        except BaseException:
            await self.stop(session_id)
            raise

    async def set_state(self, command: dict[str, Any]) -> None:
        session_id = _required_string(command, "session_id")
        payload = _required_mapping(command, "payload")
        context = self._sessions.get(session_id)
        if context is None:
            raise RuntimeError(f"Render session not found: {session_id}")
        context[0].set_state(
            AvatarState(_required_string(payload, "state")),
            _required_int(payload, "state_version"),
        )

    async def stop(self, session_id: str) -> None:
        context = self._sessions.pop(session_id, None)
        if context is None:
            return
        session, task = context
        await session.stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        await asyncio.gather(*(self.stop(session_id) for session_id in tuple(self._sessions)))

    @property
    def active_sessions(self) -> int:
        return len(self._sessions)

    def _session_done(self, session_id: str, task: asyncio.Task[None]) -> None:
        current = self._sessions.get(session_id)
        if current is not None and current[1] is task:
            self._sessions.pop(session_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "Render session crashed session=%s",
                session_id,
                exc_info=task.exception(),
            )


class RedisRenderWorker:
    def __init__(self, settings: WorkerSettings) -> None:
        self._settings = settings
        self._redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
        self._stream = f"avatar:commands:{settings.worker_id}"
        self._group = "avatar-render-workers"
        self._manager = RenderSessionManager(settings)

    async def run(self) -> None:
        await self._ensure_group()
        heartbeat = asyncio.create_task(self._heartbeat(), name="worker-heartbeat")
        try:
            while True:
                batches = await self._redis.xreadgroup(
                    groupname=self._group,
                    consumername=self._settings.worker_id,
                    streams={self._stream: ">"},
                    count=10,
                    block=1_000,
                )
                for _stream_name, messages in batches:
                    for message_id, fields in messages:
                        await self._handle_with_retry(message_id, fields)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            await self._manager.shutdown()
            await self._redis.aclose()

    async def _handle_with_retry(
        self,
        message_id: str,
        fields: dict[str, Any],
    ) -> None:
        command: dict[str, Any] | None = None
        for attempt in range(1, 4):
            try:
                raw = fields.get("data")
                if not isinstance(raw, str):
                    raise ValueError("Redis command has no string data field")
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise ValueError("Redis command data must be an object")
                command_id = _required_string(command, "command_id")
                processed_key = f"avatar:command:processed:{command_id}"
                if await self._redis.exists(processed_key):
                    await self._redis.xack(self._stream, self._group, message_id)
                    return
                await self._dispatch(command)
                await self._redis.set(processed_key, "1", ex=86_400)
                await self._redis.xack(self._stream, self._group, message_id)
                return
            except Exception:
                logger.exception(
                    "Command failed id=%s attempt=%s",
                    message_id,
                    attempt,
                )
                if attempt < 3:
                    await asyncio.sleep(0.25 * 2 ** (attempt - 1))
        if command is not None:
            await self._publish_event(
                command,
                "avatar.session.failed",
                detail="Render command failed after three attempts",
            )

    async def _dispatch(self, command: dict[str, Any]) -> None:
        command_type = _required_string(command, "type")
        if command_type == "avatar.session.start":
            await self._manager.start(command)
            await self._publish_event(command, "avatar.session.ready")
        elif command_type == "avatar.state.set":
            await self._manager.set_state(command)
        elif command_type == "avatar.session.stop":
            await self._manager.stop(_required_string(command, "session_id"))
            await self._publish_event(command, "avatar.session.stopped")
        else:
            raise ValueError(f"Unsupported render command: {command_type}")

    async def _publish_event(
        self,
        command: dict[str, Any],
        event_type: str,
        detail: str | None = None,
    ) -> None:
        session_id = _required_string(command, "session_id")
        event = {
            "schema_version": 1,
            "type": event_type,
            "session_id": session_id,
            "command_id": command.get("command_id"),
        }
        if detail is not None:
            event["detail"] = detail
        await self._redis.xadd(
            f"avatar:events:{session_id}",
            {"data": json.dumps(event)},
            maxlen=100,
            approximate=True,
        )

    async def _ensure_group(self) -> None:
        try:
            await self._redis.xgroup_create(
                self._stream,
                self._group,
                id="0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _heartbeat(self) -> None:
        key = f"avatar:worker:{self._settings.worker_id}"
        while True:
            payload = json.dumps(
                {
                    "worker_id": self._settings.worker_id,
                    "status": "ready",
                    "active_sessions": self._manager.active_sessions,
                }
            )
            await self._redis.set(key, payload, ex=15)
            await asyncio.sleep(5)


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _required_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"Missing or invalid '{key}'")
    return value


def _required_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Missing or invalid '{key}'")
    return value
