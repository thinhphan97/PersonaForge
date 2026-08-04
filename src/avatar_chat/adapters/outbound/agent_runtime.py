import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import grpc
import jwt

from avatar_chat.application.dto import (
    AgentAuthorizationScope,
    AgentRuntimeEvent,
    AgentRuntimeEventType,
    AgentTurnRequest,
)
from avatar_chat.application.ports import ReplyGeneratorPort
from personaforge.agent.v1 import agent_pb2, agent_pb2_grpc


class AgentDelegationJwtAdapter:
    def __init__(
        self,
        secret: str,
        issuer: str,
        audience: str,
        ttl: timedelta = timedelta(minutes=2),
    ) -> None:
        self._secret = secret
        self._issuer = issuer
        self._audience = audience
        self._ttl = ttl

    def issue(
        self,
        user_id: str,
        session_id: str,
        turn_id: str,
        agent_id: str,
        tool_permissions: tuple[str, ...],
        knowledge_base_ids: tuple[str, ...],
        issued_at: datetime,
    ) -> str:
        now = issued_at.astimezone(timezone.utc)
        return jwt.encode(
            {
                "iss": self._issuer,
                "aud": self._audience,
                "sub": user_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "agent_id": agent_id,
                "tool_permissions": list(tool_permissions),
                "knowledge_base_ids": list(knowledge_base_ids),
                "jti": str(uuid4()),
                "iat": now,
                "exp": now + self._ttl,
            },
            self._secret,
            algorithm="HS256",
        )


class DenyAllAgentAuthorizationAdapter:
    """Safe default until agent configuration grants explicit resources."""

    async def resolve(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
    ) -> AgentAuthorizationScope:
        del user_id, session_id, agent_id
        return AgentAuthorizationScope()


class GrpcAgentRuntimeAdapter:
    def __init__(
        self,
        target: str,
        turn_timeout_seconds: float = 120,
        idle_timeout_seconds: float = 15,
    ) -> None:
        self._turn_timeout_seconds = turn_timeout_seconds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._channel = grpc.aio.insecure_channel(
            target,
            options=(
                ("grpc.keepalive_time_ms", 10_000),
                ("grpc.keepalive_timeout_ms", 3_000),
                ("grpc.keepalive_permit_without_calls", 1),
            ),
        )
        self._stub = agent_pb2_grpc.AgentRuntimeStub(self._channel)

    async def stream_turn(
        self,
        request: AgentTurnRequest,
        delegation_token: str,
    ) -> AsyncIterator[AgentRuntimeEvent]:
        call = self._stub.ExecuteTurn(
            agent_pb2.AgentTurnRequest(
                turn_id=request.turn_id,
                session_id=request.session_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
                agent_config_version=request.agent_config_version,
                user_message=request.user_message,
                history=[
                    agent_pb2.ChatMessage(
                        role=message.role.value,
                        content=message.content,
                    )
                    for message in request.history
                ],
                tool_permissions=request.tool_permissions,
                knowledge_base_ids=request.knowledge_base_ids,
            ),
            metadata=(("authorization", f"Bearer {delegation_token}"),),
            timeout=self._turn_timeout_seconds,
        )
        iterator = call.__aiter__()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        anext(iterator),
                        timeout=self._idle_timeout_seconds,
                    )
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError:
                    call.cancel()
                    raise TimeoutError(
                        f"Agent turn became idle: {request.turn_id}"
                    ) from None
                yield AgentRuntimeEvent(
                    type=AgentRuntimeEventType(event.type),
                    sequence=event.sequence,
                    text=event.text,
                    stage=event.stage,
                    error_code=event.error_code,
                    error_detail=event.error_detail,
                )
        finally:
            if not call.done():
                call.cancel()

    async def cancel_turn(self, turn_id: str) -> bool:
        response = await self._stub.CancelTurn(
            agent_pb2.CancelTurnRequest(turn_id=turn_id),
            timeout=3,
        )
        return bool(response.cancelled)

    async def shutdown(self) -> None:
        await self._channel.close()


class LegacyReplyAgentAdapter:
    """Compatibility bridge while HTTP/scripted reply providers are phased out."""

    def __init__(self, replies: ReplyGeneratorPort) -> None:
        self._replies = replies

    async def stream_turn(
        self,
        request: AgentTurnRequest,
        delegation_token: str,
    ) -> AsyncIterator[AgentRuntimeEvent]:
        del delegation_token
        yield AgentRuntimeEvent(AgentRuntimeEventType.TURN_STARTED, 1, stage="model")
        sequence = 1
        async for chunk in self._replies.stream_reply(request.session_id, request.history):
            sequence += 1
            yield AgentRuntimeEvent(
                AgentRuntimeEventType.TEXT_DELTA,
                sequence,
                text=chunk.text,
            )
        yield AgentRuntimeEvent(
            AgentRuntimeEventType.TURN_COMPLETED,
            sequence + 1,
            stage="complete",
        )

    async def cancel_turn(self, turn_id: str) -> bool:
        del turn_id
        return False

    async def shutdown(self) -> None:
        return None
