import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import grpc
import jwt

from personaforge.agent.v1 import agent_pb2, agent_pb2_grpc
from personaforge_agent.application.admission import (
    AdmissionRejected,
    TurnAdmissionController,
)
from personaforge_agent.application.execute_turn import ExecuteAgentTurn
from personaforge_agent.application.ports import DelegationVerifierPort, TurnRegistryPort
from personaforge_agent.domain.models import AgentEventType, AgentMessage, AgentTurn


@dataclass(frozen=True, slots=True)
class AgentRequestLimits:
    max_message_chars: int = 20_000
    max_history_messages: int = 50
    max_history_chars: int = 100_000
    max_scopes: int = 100


class AgentRuntimeGrpcService(agent_pb2_grpc.AgentRuntimeServicer):
    def __init__(
        self,
        execute_turn: ExecuteAgentTurn,
        delegation_tokens: DelegationVerifierPort,
        admission: TurnAdmissionController,
        turns: TurnRegistryPort,
        limits: AgentRequestLimits | None = None,
        status_token: str | None = None,
    ) -> None:
        self._execute_turn = execute_turn
        self._delegation_tokens = delegation_tokens
        self._admission = admission
        self._turns = turns
        self._limits = limits or AgentRequestLimits()
        self._status_token = status_token
        self._active: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()

    async def ExecuteTurn(
        self,
        request: agent_pb2.AgentTurnRequest,
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[agent_pb2.AgentEvent]:
        try:
            _validate_request(request, self._limits)
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        turn = _turn(request)
        try:
            token = _bearer_token(context)
            self._delegation_tokens.verify(token, turn)
        except (jwt.PyJWTError, PermissionError, ValueError) as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
        claim = await self._turns.claim(turn.turn_id)
        if claim is None:
            await context.abort(
                grpc.StatusCode.ALREADY_EXISTS,
                f"Turn is already active or completed: {turn.turn_id}",
            )
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
            try:
                completed = False
                failed = False
                async with self._admission.slot(turn, cancelled):
                    async for event in self._execute_turn.execute(turn, cancelled):
                        await claim.ensure_valid()
                        completed = completed or event.type is AgentEventType.TURN_COMPLETED
                        failed = failed or event.type is AgentEventType.TURN_FAILED
                        yield agent_pb2.AgentEvent(
                            turn_id=turn.turn_id,
                            sequence=event.sequence,
                            type=event.type.value,
                            text=event.text,
                            stage=event.stage,
                            error_code=event.error_code,
                            error_detail=event.error_detail,
                        )
                if cancelled.is_set():
                    await self._admission.record_cancelled()
                    await claim.complete("cancelled")
                elif failed:
                    await self._admission.record_failed()
                    await claim.complete("failed")
                elif completed:
                    await self._admission.record_completed()
                    await claim.complete("completed")
                else:
                    await self._admission.record_failed()
                    await claim.complete("failed")
            except AdmissionRejected as exc:
                await claim.release()
                await context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
            except asyncio.CancelledError:
                await self._admission.record_cancelled()
                await claim.complete("cancelled")
                raise
            except BaseException:
                await claim.complete("failed")
                raise
        finally:
            async with self._lock:
                self._active.pop(turn.turn_id, None)
            await claim.release()

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

    async def GetRuntimeStatus(
        self,
        request: agent_pb2.RuntimeStatusRequest,
        context: grpc.aio.ServicerContext,
    ) -> agent_pb2.RuntimeStatusResponse:
        del request
        if self._status_token is not None and _metadata_value(
            context, "x-agent-status-token"
        ) != self._status_token:
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Agent runtime status token is required",
            )
        status = await self._admission.snapshot()
        return agent_pb2.RuntimeStatusResponse(
            active_turns=status.active_turns,
            queued_turns=status.queued_turns,
            max_concurrent_turns=status.max_concurrent_turns,
            max_queued_turns=status.max_queued_turns,
            accepted_total=status.accepted_total,
            rejected_total=status.rejected_total,
            completed_total=status.completed_total,
            failed_total=status.failed_total,
            cancelled_total=status.cancelled_total,
            ready=status.ready,
            draining=status.draining,
        )


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


def _validate_request(
    request: agent_pb2.AgentTurnRequest,
    limits: AgentRequestLimits,
) -> None:
    required = {
        "turn_id": request.turn_id,
        "session_id": request.session_id,
        "user_id": request.user_id,
        "agent_id": request.agent_id,
        "user_message": request.user_message,
    }
    missing = next((name for name, value in required.items() if not value.strip()), None)
    if missing is not None:
        raise ValueError(f"Missing required Agent turn field: {missing}")
    if len(request.user_message) > limits.max_message_chars:
        raise ValueError("Agent user message exceeds configured size limit")
    if len(request.history) > limits.max_history_messages:
        raise ValueError("Agent history exceeds configured message limit")
    if sum(len(message.content) for message in request.history) > limits.max_history_chars:
        raise ValueError("Agent history exceeds configured character limit")
    if (
        len(request.tool_permissions) > limits.max_scopes
        or len(request.knowledge_base_ids) > limits.max_scopes
    ):
        raise ValueError("Agent authorization scope count exceeds configured limit")


def _bearer_token(context: grpc.aio.ServicerContext) -> str:
    authorization = _metadata_value(context, "authorization")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise ValueError("Agent delegation token is required")
    return token


def _metadata_value(context: grpc.aio.ServicerContext, key: str) -> str:
    return next(
        (
            item.value
            for item in context.invocation_metadata()
            if item.key.lower() == key.lower()
        ),
        "",
    )
