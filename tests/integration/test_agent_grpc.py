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
from personaforge.agent.v1 import agent_pb2_grpc
from personaforge_agent.adapters.inbound.grpc_service import AgentRuntimeGrpcService
from personaforge_agent.adapters.outbound.delegation_tokens import JwtDelegationVerifier
from personaforge_agent.adapters.outbound.scripted_model import ScriptedLanguageModelAdapter
from personaforge_agent.application.execute_turn import ExecuteAgentTurn


@pytest.mark.asyncio
async def test_core_streams_a_delegated_turn_over_grpc() -> None:
    secret = "test-agent-delegation-secret-with-enough-bytes"
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentRuntimeServicer_to_server(
        AgentRuntimeGrpcService(
            ExecuteAgentTurn(ScriptedLanguageModelAdapter(0), 0.01),
            JwtDelegationVerifier(secret, "test-api", "test-agent"),
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
    finally:
        await adapter.shutdown()
        await server.stop(grace=None)

    assert events[0].type is AgentRuntimeEventType.TURN_STARTED
    assert events[-1].type is AgentRuntimeEventType.TURN_COMPLETED
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
