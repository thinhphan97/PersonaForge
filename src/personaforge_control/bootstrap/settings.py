import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class ControlSettings:
    host: str
    port: int
    redis_admin_url: str
    redis_worker_url: str
    enrollment_jwt_secret: str
    enrollment_issuer: str
    enrollment_audience: str
    operator_token: str
    core_token: str
    registration_ttl_seconds: int
    credential_ttl_seconds: int
    slot_lease_seconds: int
    database_url: str
    telemetry_consumer_name: str
    telemetry_batch_size: int
    telemetry_poll_seconds: float
    telemetry_claim_idle_ms: int
    telemetry_aggregate_seconds: float
    telemetry_retention_days: int
    telemetry_retention_interval_seconds: float
    resource_default_peak_vram_mb: int
    resource_default_gpu_duty_cycle: float
    resource_compatible_vram_margin_mb: int

    @classmethod
    def from_env(cls) -> "ControlSettings":
        redis_worker_url = (
            os.getenv("CONTROL_REDIS_ADVERTISED_URL")
            or os.getenv("CONTROL_REDIS_WORKER_URL")
            or "redis://redis:6379/0"
        )
        if os.getenv("REMOTE_WORKERS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}:
            parsed_redis_worker_url = urlsplit(redis_worker_url)
            hostname = parsed_redis_worker_url.hostname
            if hostname in {None, "redis", "localhost", "127.0.0.1", "::1"}:
                raise ValueError(
                    "CONTROL_REDIS_ADVERTISED_URL must use an address reachable from remote "
                    "workers when REMOTE_WORKERS_ENABLED=true"
                )
            published_port = os.getenv("REDIS_PORT")
            if published_port and (parsed_redis_worker_url.port or 6379) != int(published_port):
                raise ValueError(
                    "CONTROL_REDIS_ADVERTISED_URL port must match the Redis port published "
                    "by this deployment (REDIS_PORT)"
                )
        return cls(
            host=os.getenv("CONTROL_HOST", "0.0.0.0"),
            port=int(os.getenv("CONTROL_PORT", "8090")),
            redis_admin_url=os.getenv("CONTROL_REDIS_ADMIN_URL", "redis://redis:6379/0"),
            redis_worker_url=redis_worker_url,
            enrollment_jwt_secret=os.getenv(
                "CONTROL_ENROLLMENT_JWT_SECRET",
                "development-control-enrollment-secret-change-me",
            ),
            enrollment_issuer=os.getenv("CONTROL_ENROLLMENT_ISSUER", "personaforge-control"),
            enrollment_audience=os.getenv(
                "CONTROL_ENROLLMENT_AUDIENCE",
                "personaforge-render-worker-enrollment",
            ),
            operator_token=os.getenv(
                "CONTROL_OPERATOR_TOKEN",
                "development-operator-token-change-me",
            ),
            core_token=os.getenv(
                "CONTROL_CORE_TOKEN",
                "development-core-control-token-change-me",
            ),
            registration_ttl_seconds=int(os.getenv("CONTROL_REGISTRATION_TTL_SECONDS", "15")),
            credential_ttl_seconds=int(os.getenv("CONTROL_REDIS_CREDENTIAL_TTL_SECONDS", "3600")),
            slot_lease_seconds=int(os.getenv("CONTROL_SLOT_LEASE_SECONDS", "30")),
            database_url=os.getenv(
                "CONTROL_DATABASE_URL",
                os.getenv(
                    "DATABASE_URL",
                    "postgresql+asyncpg://avatar:avatar@postgres:5432/avatar",
                ),
            ),
            telemetry_consumer_name=os.getenv("CONTROL_TELEMETRY_CONSUMER_NAME", "control-1"),
            telemetry_batch_size=int(os.getenv("CONTROL_TELEMETRY_BATCH_SIZE", "100")),
            telemetry_poll_seconds=float(os.getenv("CONTROL_TELEMETRY_POLL_SECONDS", "1")),
            telemetry_claim_idle_ms=int(os.getenv("CONTROL_TELEMETRY_CLAIM_IDLE_MS", "30000")),
            telemetry_aggregate_seconds=float(
                os.getenv("CONTROL_TELEMETRY_AGGREGATE_SECONDS", "60")
            ),
            telemetry_retention_days=int(os.getenv("CONTROL_TELEMETRY_RETENTION_DAYS", "30")),
            telemetry_retention_interval_seconds=float(
                os.getenv("CONTROL_TELEMETRY_RETENTION_INTERVAL_SECONDS", "3600")
            ),
            resource_default_peak_vram_mb=int(
                os.getenv("CONTROL_RESOURCE_DEFAULT_PEAK_VRAM_MB", "4096")
            ),
            resource_default_gpu_duty_cycle=float(
                os.getenv("CONTROL_RESOURCE_DEFAULT_GPU_DUTY_CYCLE", "0.25")
            ),
            resource_compatible_vram_margin_mb=int(
                os.getenv("CONTROL_RESOURCE_COMPATIBLE_VRAM_MARGIN_MB", "1024")
            ),
        )
