import asyncio
import logging
import signal

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from personaforge.agent.v1 import agent_pb2_grpc
from personaforge_agent.adapters.inbound.grpc_service import AgentRuntimeGrpcService
from personaforge_agent.adapters.outbound.delegation_tokens import JwtDelegationVerifier
from personaforge_agent.adapters.outbound.scripted_model import (
    ScriptedLanguageModelAdapter,
)
from personaforge_agent.application.execute_turn import ExecuteAgentTurn
from personaforge_agent.bootstrap.settings import AgentSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def serve(settings: AgentSettings | None = None) -> None:
    resolved = settings or AgentSettings.from_env()
    if resolved.provider != "scripted":
        raise ValueError(f"Unsupported AGENT_MODEL_PROVIDER: {resolved.provider}")
    model = ScriptedLanguageModelAdapter(resolved.chunk_delay_seconds)
    execute = ExecuteAgentTurn(model, resolved.heartbeat_interval_seconds)
    verifier = JwtDelegationVerifier(
        resolved.delegation_secret,
        resolved.delegation_issuer,
        resolved.delegation_audience,
    )
    server = grpc.aio.server(
        options=(
            ("grpc.keepalive_time_ms", 10_000),
            ("grpc.keepalive_timeout_ms", 3_000),
            ("grpc.http2.min_ping_interval_without_data_ms", 5_000),
        )
    )
    agent_pb2_grpc.add_AgentRuntimeServicer_to_server(
        AgentRuntimeGrpcService(execute, verifier),
        server,
    )
    health_service = health.aio.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_service, server)
    service_name = "personaforge.agent.v1.AgentRuntime"
    await health_service.set(service_name, health_pb2.HealthCheckResponse.SERVING)
    address = f"{resolved.host}:{resolved.port}"
    server.add_insecure_port(address)
    await server.start()
    logger.info("PersonaForge Agent Runtime listening on %s", address)

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    await stopped.wait()
    await health_service.set(service_name, health_pb2.HealthCheckResponse.NOT_SERVING)
    await server.stop(grace=5)


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
