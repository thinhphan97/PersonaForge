import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import grpc
import pytest

from avatar_chat.adapters.outbound.agent_runtime import (
    AgentDelegationJwtAdapter,
    GrpcAgentRuntimeAdapter,
)
from avatar_chat.application.dto import (
    AgentRuntimeEventType,
    AgentTurnRequest,
)
from personaforge.agent.v1 import agent_pb2, agent_pb2_grpc
from personaforge_agent.adapters.inbound.grpc_service import AgentRuntimeGrpcService
from personaforge_agent.adapters.outbound.delegation_tokens import JwtDelegationVerifier
from personaforge_agent.adapters.outbound.scripted_model import ScriptedLanguageModelAdapter
from personaforge_agent.adapters.outbound.turn_registry import InMemoryTurnRegistry
from personaforge_agent.application.admission import TurnAdmissionController
from personaforge_agent.application.execute_turn import ExecuteAgentTurn
from personaforge_agent.domain.models import AgentTurn


def admission() -> TurnAdmissionController:
    return TurnAdmissionController(10, 10, 3, 1, 1)


class BlockingModel:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, turn: AgentTurn) -> AsyncIterator[str]:
        del turn
        self.entered.set()
        await self.release.wait()
        yield "done"


@pytest.mark.asyncio
async def test_core_streams_a_delegated_turn_over_grpc() -> None:
    secret = "test-agent-delegation-secret-with-enough-bytes"
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentRuntimeServicer_to_server(
        AgentRuntimeGrpcService(
            ExecuteAgentTurn(ScriptedLanguageModelAdapter(0), 0.01),
            JwtDelegationVerifier(secret, "test-api", "test-agent"),
            admission(),
            InMemoryTurnRegistry(),
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    adapter = GrpcAgentRuntimeAdapter(
        f"127.0.0.1:{port}",
        turn_timeout_seconds=5,
        idle_timeout_seconds=1,
    )
    request = AgentTurnRequest(
        turn_id="turn-1",
        session_id="session-1",
        user_id="user-1",
        agent_id="default",
        agent_config_version=1,
        user_message="PersonaForge hoạt động thế nào?",
        history=(),
    )
    token = AgentDelegationJwtAdapter(
        secret,
        "test-api",
        "test-agent",
    ).issue(
        request.user_id,
        request.session_id,
        request.turn_id,
        request.agent_id,
        (),
        (),
        datetime.now(timezone.utc),
    )

    try:
        events = [event async for event in adapter.stream_turn(request, token)]
        status_channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
        status = await agent_pb2_grpc.AgentRuntimeStub(status_channel).GetRuntimeStatus(
            agent_pb2.RuntimeStatusRequest(),
            timeout=1,
        )
        await status_channel.close()
    finally:
        await adapter.shutdown()
        await server.stop(grace=None)

    assert events[0].type is AgentRuntimeEventType.TURN_STARTED
    assert events[-1].type is AgentRuntimeEventType.TURN_COMPLETED
    assert status.active_turns == 0
    assert status.completed_total == 1
    assert status.ready is True
    assert "PersonaForge hoạt động thế nào?" in "".join(
        event.text
        for event in events
        if event.type is AgentRuntimeEventType.TEXT_DELTA
    )


@pytest.mark.asyncio
async def test_agent_rejects_a_token_for_another_turn() -> None:
    secret = "test-agent-delegation-secret-with-enough-bytes"
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentRuntimeServicer_to_server(
        AgentRuntimeGrpcService(
            ExecuteAgentTurn(ScriptedLanguageModelAdapter(0)),
            JwtDelegationVerifier(secret, "test-api", "test-agent"),
            admission(),
            InMemoryTurnRegistry(),
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    adapter = GrpcAgentRuntimeAdapter(f"127.0.0.1:{port}", 5, 1)
    request = AgentTurnRequest(
        "turn-requested",
        "session-1",
        "user-1",
        "default",
        1,
        "hello",
        (),
    )
    token = AgentDelegationJwtAdapter(secret, "test-api", "test-agent").issue(
        "user-1",
        "session-1",
        "another-turn",
        "default",
        (),
        (),
        datetime.now(timezone.utc),
    )

    try:
        with pytest.raises(grpc.aio.AioRpcError) as captured:
            _ = [event async for event in adapter.stream_turn(request, token)]
    finally:
        await adapter.shutdown()
        await server.stop(grace=None)

    assert captured.value.code() is grpc.StatusCode.UNAUTHENTICATED


@pytest.mark.asyncio
async def test_agent_returns_resource_exhausted_when_no_queue_capacity() -> None:
    secret = "test-agent-delegation-secret-with-enough-bytes"
    model = BlockingModel()
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentRuntimeServicer_to_server(
        AgentRuntimeGrpcService(
            ExecuteAgentTurn(model),
            JwtDelegationVerifier(secret, "test-api", "test-agent"),
            TurnAdmissionController(1, 0, 1, 1, 1),
            InMemoryTurnRegistry(),
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    adapter = GrpcAgentRuntimeAdapter(f"127.0.0.1:{port}", 5, 1)

    def request_and_token(turn_id: str, user_id: str, session_id: str):
        request = AgentTurnRequest(
            turn_id,
            session_id,
            user_id,
            "default",
            1,
            "hello",
            (),
        )
        token = AgentDelegationJwtAdapter(secret, "test-api", "test-agent").issue(
            user_id,
            session_id,
            turn_id,
            "default",
            (),
            (),
            datetime.now(timezone.utc),
        )
        return request, token

    first_request, first_token = request_and_token("turn-1", "user-1", "session-1")
    second_request, second_token = request_and_token("turn-2", "user-2", "session-2")
    first_task = asyncio.create_task(
        _collect_events(adapter, first_request, first_token)
    )
    try:
        await asyncio.wait_for(model.entered.wait(), timeout=1)
        with pytest.raises(grpc.aio.AioRpcError) as captured:
            await _collect_events(adapter, second_request, second_token)
        assert captured.value.code() is grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        model.release.set()
        await first_task
        await adapter.shutdown()
        await server.stop(grace=None)


async def _collect_events(
    adapter: GrpcAgentRuntimeAdapter,
    request: AgentTurnRequest,
    token: str,
):
    return [event async for event in adapter.stream_turn(request, token)]


@pytest.mark.asyncio
async def test_runtime_status_can_require_a_monitoring_token() -> None:
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentRuntimeServicer_to_server(
        AgentRuntimeGrpcService(
            ExecuteAgentTurn(ScriptedLanguageModelAdapter(0)),
            JwtDelegationVerifier("secret", "issuer", "audience"),
            admission(),
            InMemoryTurnRegistry(),
            status_token="metrics-secret",
        ),
        server,
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    stub = agent_pb2_grpc.AgentRuntimeStub(channel)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as captured:
            await stub.GetRuntimeStatus(agent_pb2.RuntimeStatusRequest(), timeout=1)
        assert captured.value.code() is grpc.StatusCode.UNAUTHENTICATED
        status = await stub.GetRuntimeStatus(
            agent_pb2.RuntimeStatusRequest(),
            metadata=(("x-agent-status-token", "metrics-secret"),),
            timeout=1,
        )
        assert status.ready is True
    finally:
        await channel.close()
        await server.stop(grace=None)
