import asyncio
from collections.abc import AsyncIterator

import grpc
import jwt

from personaforge.agent.v1 import agent_pb2, agent_pb2_grpc
from personaforge_agent.application.execute_turn import ExecuteAgentTurn
from personaforge_agent.application.ports import DelegationVerifierPort
from personaforge_agent.domain.models import AgentMessage, AgentTurn


class AgentRuntimeGrpcService(agent_pb2_grpc.AgentRuntimeServicer):
    def __init__(
        self,
        execute_turn: ExecuteAgentTurn,
        delegation_tokens: DelegationVerifierPort,
    ) -> None:
        self._execute_turn = execute_turn
        self._delegation_tokens = delegation_tokens
        self._active: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def ExecuteTurn(
        self,
        request: agent_pb2.AgentTurnRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[agent_pb2.AgentEvent]:
        turn = _turn(request)
        token = _bearer_token(context)
        try:
            self._delegation_tokens.verify(token, turn)
        except (jwt.PyJWTError, PermissionError, ValueError) as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        cancelled = asyncio.Event()
        async with self._lock:
            if turn.turn_id in self._active:
                await context.abort(
                    grpc.StatusCode.ALREADY_EXISTS,
                    f"Turn is already active: {turn.turn_id}",
                )
            self._active[turn.turn_id] = cancelled
        context.add_done_callback(lambda _context: cancelled.set())
        try:
            async for event in self._execute_turn.execute(turn, cancelled):
                yield agent_pb2.AgentEvent(
                    turn_id=turn.turn_id,
                    sequence=event.sequence,
                    type=event.type.value,
                    text=event.text,
                    stage=event.stage,
                    error_code=event.error_code,
                    error_detail=event.error_detail,
                )
        finally:
            async with self._lock:
                self._active.pop(turn.turn_id, None)

    async def CancelTurn(
        self,
        request: agent_pb2.CancelTurnRequest,
        context: grpc.aio.ServicerContext,
    ) -> agent_pb2.CancelTurnResponse:
        del context
        async with self._lock:
            cancelled = self._active.get(request.turn_id)
            if cancelled is None:
                return agent_pb2.CancelTurnResponse(cancelled=False)
            cancelled.set()
            return agent_pb2.CancelTurnResponse(cancelled=True)


def _turn(request: agent_pb2.AgentTurnRequest) -> AgentTurn:
    return AgentTurn(
        turn_id=request.turn_id,
        session_id=request.session_id,
        user_id=request.user_id,
        agent_id=request.agent_id,
        agent_config_version=request.agent_config_version,
        user_message=request.user_message,
        history=tuple(
            AgentMessage(message.role, message.content) for message in request.history
        ),
        tool_permissions=tuple(request.tool_permissions),
        knowledge_base_ids=tuple(request.knowledge_base_ids),
    )


def _bearer_token(context: grpc.aio.ServicerContext) -> str:
    authorization = next(
        (
            item.value
            for item in context.invocation_metadata()
            if item.key.lower() == "authorization"
        ),
        "",
    )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ValueError("Agent delegation token is required")
    return token
