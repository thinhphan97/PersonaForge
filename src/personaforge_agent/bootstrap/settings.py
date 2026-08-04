import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentSettings:
    host: str
    port: int
    tls_cert_file: str | None
    tls_key_file: str | None
    tls_client_ca_file: str | None
    provider: str
    chunk_delay_seconds: float
    heartbeat_interval_seconds: float
    max_concurrent_turns: int
    max_queued_turns: int
    max_concurrent_per_user: int
    max_concurrent_per_session: int
    queue_timeout_seconds: float
    turn_registry_provider: str
    redis_url: str
    turn_claim_lease_seconds: float
    turn_result_retention_seconds: int
    graceful_shutdown_seconds: float
    max_message_chars: int
    max_history_messages: int
    max_history_chars: int
    max_scopes: int
    status_token: str | None
    delegation_secret: str
    delegation_issuer: str
    delegation_audience: str

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(
            host=os.getenv("AGENT_GRPC_HOST", "0.0.0.0"),
            port=int(os.getenv("AGENT_GRPC_PORT", "50051")),
            tls_cert_file=os.getenv("AGENT_GRPC_TLS_CERT_FILE") or None,
            tls_key_file=os.getenv("AGENT_GRPC_TLS_KEY_FILE") or None,
            tls_client_ca_file=os.getenv("AGENT_GRPC_TLS_CLIENT_CA_FILE") or None,
            provider=os.getenv("AGENT_MODEL_PROVIDER", "scripted").lower(),
            chunk_delay_seconds=float(os.getenv("AGENT_CHUNK_DELAY", "0.1")),
            heartbeat_interval_seconds=float(
                os.getenv("AGENT_HEARTBEAT_INTERVAL", "5")
            ),
            max_concurrent_turns=int(os.getenv("AGENT_MAX_CONCURRENT_TURNS", "20")),
            max_queued_turns=int(os.getenv("AGENT_MAX_QUEUED_TURNS", "100")),
            max_concurrent_per_user=int(
                os.getenv("AGENT_MAX_CONCURRENT_PER_USER", "3")
            ),
            max_concurrent_per_session=int(
                os.getenv("AGENT_MAX_CONCURRENT_PER_SESSION", "1")
            ),
            queue_timeout_seconds=float(os.getenv("AGENT_QUEUE_TIMEOUT", "10")),
            turn_registry_provider=os.getenv(
                "AGENT_TURN_REGISTRY_PROVIDER", "memory"
            ).lower(),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            turn_claim_lease_seconds=float(
                os.getenv("AGENT_TURN_CLAIM_LEASE", "60")
            ),
            turn_result_retention_seconds=int(
                os.getenv("AGENT_TURN_RESULT_RETENTION", "86400")
            ),
            graceful_shutdown_seconds=float(
                os.getenv("AGENT_GRACEFUL_SHUTDOWN_TIMEOUT", "30")
            ),
            max_message_chars=int(os.getenv("AGENT_MAX_MESSAGE_CHARS", "20000")),
            max_history_messages=int(os.getenv("AGENT_MAX_HISTORY_MESSAGES", "50")),
            max_history_chars=int(os.getenv("AGENT_MAX_HISTORY_CHARS", "100000")),
            max_scopes=int(os.getenv("AGENT_MAX_SCOPES", "100")),
            status_token=os.getenv("AGENT_STATUS_TOKEN") or None,
            delegation_secret=os.getenv(
                "AGENT_DELEGATION_SECRET",
                "development-agent-delegation-secret-change-me",
            ),
            delegation_issuer=os.getenv("AGENT_DELEGATION_ISSUER", "personaforge-api"),
            delegation_audience=os.getenv(
                "AGENT_DELEGATION_AUDIENCE",
                "personaforge-agent",
            ),
        )
