import os
from dataclasses import dataclass


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

    @classmethod
    def from_env(cls) -> "ControlSettings":
        return cls(
            host=os.getenv("CONTROL_HOST", "0.0.0.0"),
            port=int(os.getenv("CONTROL_PORT", "8090")),
            redis_admin_url=os.getenv("CONTROL_REDIS_ADMIN_URL", "redis://redis:6379/0"),
            redis_worker_url=os.getenv("CONTROL_REDIS_WORKER_URL", "redis://redis:6379/0"),
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
            registration_ttl_seconds=int(
                os.getenv("CONTROL_REGISTRATION_TTL_SECONDS", "15")
            ),
            credential_ttl_seconds=int(
                os.getenv("CONTROL_REDIS_CREDENTIAL_TTL_SECONDS", "3600")
            ),
            slot_lease_seconds=int(os.getenv("CONTROL_SLOT_LEASE_SECONDS", "30")),
        )
