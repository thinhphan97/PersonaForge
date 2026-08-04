import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentSettings:
    host: str
    port: int
    provider: str
    chunk_delay_seconds: float
    heartbeat_interval_seconds: float
    delegation_secret: str
    delegation_issuer: str
    delegation_audience: str

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(
            host=os.getenv("AGENT_GRPC_HOST", "0.0.0.0"),
            port=int(os.getenv("AGENT_GRPC_PORT", "50051")),
            provider=os.getenv("AGENT_MODEL_PROVIDER", "scripted").lower(),
            chunk_delay_seconds=float(os.getenv("AGENT_CHUNK_DELAY", "0.1")),
            heartbeat_interval_seconds=float(
                os.getenv("AGENT_HEARTBEAT_INTERVAL", "5")
            ),
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
