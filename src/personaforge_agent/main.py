import asyncio
import logging
import signal
from pathlib import Path

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

from personaforge.agent.v1 import agent_pb2_grpc
from personaforge_agent.adapters.inbound.grpc_service import (
    AgentRequestLimits,
    AgentRuntimeGrpcService,
)
from personaforge_agent.adapters.outbound.delegation_tokens import JwtDelegationVerifier
from personaforge_agent.adapters.outbound.scripted_model import (
    ScriptedLanguageModelAdapter,
)
from personaforge_agent.adapters.outbound.turn_registry import (
    InMemoryTurnRegistry,
    RedisTurnRegistry,
)
from personaforge_agent.application.admission import TurnAdmissionController
from personaforge_agent.application.execute_turn import ExecuteAgentTurn
from personaforge_agent.bootstrap.settings import AgentSettings
from personaforge_observability import configure_logging

configure_logging("personaforge-agent")
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
    health_service = health.aio.HealthServicer()
    service_name = "personaforge.agent.v1.AgentRuntime"
    liveness_name = f"{service_name}.Liveness"
    readiness_name = f"{service_name}.Readiness"

    async def capacity_changed(ready: bool) -> None:
        status = (
            health_pb2.HealthCheckResponse.SERVING
            if ready
            else health_pb2.HealthCheckResponse.NOT_SERVING
        )
        await asyncio.gather(
            health_service.set(service_name, status),
            health_service.set(readiness_name, status),
        )

    admission = TurnAdmissionController(
        resolved.max_concurrent_turns,
        resolved.max_queued_turns,
        resolved.max_concurrent_per_user,
        resolved.max_concurrent_per_session,
        resolved.queue_timeout_seconds,
        capacity_changed,
    )
    if resolved.turn_registry_provider == "redis":
        turns = RedisTurnRegistry(
            resolved.redis_url,
            resolved.turn_claim_lease_seconds,
            resolved.turn_result_retention_seconds,
        )
    elif resolved.turn_registry_provider == "memory":
        turns = InMemoryTurnRegistry()
    else:
        raise ValueError(
            f"Unsupported AGENT_TURN_REGISTRY_PROVIDER: {resolved.turn_registry_provider}"
        )
    await turns.startup()
    server = grpc.aio.server(
        options=(
            ("grpc.keepalive_time_ms", 10_000),
            ("grpc.keepalive_timeout_ms", 3_000),
            ("grpc.http2.min_ping_interval_without_data_ms", 5_000),
        )
    )
    agent_pb2_grpc.add_AgentRuntimeServicer_to_server(
        AgentRuntimeGrpcService(
            execute,
            verifier,
            admission,
            turns,
            AgentRequestLimits(
                resolved.max_message_chars,
                resolved.max_history_messages,
                resolved.max_history_chars,
                resolved.max_scopes,
            ),
            resolved.status_token,
        ),
        server,
    )
    health_pb2_grpc.add_HealthServicer_to_server(health_service, server)
    await asyncio.gather(
        health_service.set(service_name, health_pb2.HealthCheckResponse.SERVING),
        health_service.set(readiness_name, health_pb2.HealthCheckResponse.SERVING),
        health_service.set(liveness_name, health_pb2.HealthCheckResponse.SERVING),
    )
    address = f"{resolved.host}:{resolved.port}"
    if bool(resolved.tls_cert_file) != bool(resolved.tls_key_file):
        await turns.shutdown()
        raise ValueError(
            "AGENT_GRPC_TLS_CERT_FILE and AGENT_GRPC_TLS_KEY_FILE must be configured together"
        )
    if resolved.tls_cert_file and resolved.tls_key_file:
        client_ca = (
            await asyncio.to_thread(Path(resolved.tls_client_ca_file).read_bytes)
            if resolved.tls_client_ca_file
            else None
        )
        private_key, certificate = await asyncio.gather(
            asyncio.to_thread(Path(resolved.tls_key_file).read_bytes),
            asyncio.to_thread(Path(resolved.tls_cert_file).read_bytes),
        )
        credentials = grpc.ssl_server_credentials(
            ((private_key, certificate),),
            root_certificates=client_ca,
            require_client_auth=client_ca is not None,
        )
        bound_port = server.add_secure_port(address, credentials)
    else:
        bound_port = server.add_insecure_port(address)
    if bound_port == 0:
        await turns.shutdown()
        raise RuntimeError(f"Could not bind Agent Runtime to {address}")

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    try:
        await server.start()
        logger.info(
            "Agent runtime is listening",
            extra={"event": "agent.service.started", "listen_address": address},
        )
        await stopped.wait()
        logger.info(
            "Agent runtime is draining",
            extra={"event": "agent.service.draining"},
        )
        await admission.start_draining()
        await server.stop(grace=resolved.graceful_shutdown_seconds)
    finally:
        await asyncio.gather(
            health_service.set(service_name, health_pb2.HealthCheckResponse.NOT_SERVING),
            health_service.set(readiness_name, health_pb2.HealthCheckResponse.NOT_SERVING),
            health_service.set(liveness_name, health_pb2.HealthCheckResponse.NOT_SERVING),
        )
        await server.stop(grace=0)
        await turns.shutdown()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
