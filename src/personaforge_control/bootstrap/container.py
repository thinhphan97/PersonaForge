from dataclasses import dataclass
from datetime import timedelta

from personaforge_control.adapters.outbound.enrollment_jwt import EnrollmentJwtAdapter
from personaforge_control.adapters.outbound.redis_control import RedisControlPlane
from personaforge_control.adapters.outbound.redis_identity import (
    RedisAclCredentialIssuer,
    RedisIdentityRepository,
    RedisWorkerRegistrationRepository,
)
from personaforge_control.adapters.outbound.system import SecureSecrets, SystemClock
from personaforge_control.adapters.outbound.telemetry_ingestor import RedisTelemetryIngestor
from personaforge_control.adapters.outbound.telemetry_store import PostgresTelemetryStore
from personaforge_control.application.identity import (
    EnrollmentService,
    WorkerRegistrationService,
)
from personaforge_control.application.inventory import ListWorkerInventory
from personaforge_control.application.scheduler import CompositeScheduler
from personaforge_control.bootstrap.settings import ControlSettings


@dataclass(slots=True)
class ControlContainer:
    enrollment: EnrollmentService
    registration: WorkerRegistrationService
    scheduler: CompositeScheduler
    worker_inventory: ListWorkerInventory
    identity_repository: RedisIdentityRepository
    credential_issuer: RedisAclCredentialIssuer
    control_backend: RedisControlPlane
    telemetry_store: PostgresTelemetryStore
    telemetry_ingestor: RedisTelemetryIngestor
    operator_token: str
    core_token: str

    async def startup(self) -> None:
        await self.telemetry_store.startup()
        await self.identity_repository.startup()
        await self.credential_issuer.startup()
        await self.control_backend.startup()
        await self.telemetry_ingestor.startup()

    async def shutdown(self) -> None:
        await self.telemetry_ingestor.shutdown()
        await self.telemetry_store.shutdown()
        await self.control_backend.shutdown()
        await self.credential_issuer.shutdown()
        await self.identity_repository.shutdown()


def build_control_container(settings: ControlSettings) -> ControlContainer:
    secrets = SecureSecrets()
    clock = SystemClock()
    identity = RedisIdentityRepository(settings.redis_admin_url)
    registrations = RedisWorkerRegistrationRepository(identity)
    credentials = RedisAclCredentialIssuer(
        settings.redis_admin_url,
        settings.redis_worker_url,
        secrets,
    )
    token_adapter = EnrollmentJwtAdapter(
        settings.enrollment_jwt_secret,
        settings.enrollment_issuer,
        settings.enrollment_audience,
    )
    control_backend = RedisControlPlane(
        settings.redis_admin_url,
        slot_lease_seconds=settings.slot_lease_seconds,
    )
    telemetry_store = PostgresTelemetryStore(
        settings.database_url,
        bootstrap_peak_vram_mb=settings.resource_default_peak_vram_mb,
        bootstrap_gpu_duty_cycle=settings.resource_default_gpu_duty_cycle,
        compatible_vram_margin_mb=settings.resource_compatible_vram_margin_mb,
    )
    telemetry_ingestor = RedisTelemetryIngestor(
        settings.redis_admin_url,
        telemetry_store,
        consumer_name=settings.telemetry_consumer_name,
        batch_size=settings.telemetry_batch_size,
        poll_interval_seconds=settings.telemetry_poll_seconds,
        claim_idle_ms=settings.telemetry_claim_idle_ms,
        aggregate_interval_seconds=settings.telemetry_aggregate_seconds,
        retention_days=settings.telemetry_retention_days,
        retention_interval_seconds=settings.telemetry_retention_interval_seconds,
    )
    return ControlContainer(
        enrollment=EnrollmentService(identity, token_adapter, secrets, clock),
        registration=WorkerRegistrationService(
            identity,
            registrations,
            credentials,
            token_adapter,
            secrets,
            clock,
            registration_ttl=timedelta(seconds=settings.registration_ttl_seconds),
            credential_ttl=timedelta(seconds=settings.credential_ttl_seconds),
        ),
        scheduler=CompositeScheduler(
            control_backend,
            control_backend,
            control_backend,
            control_backend,
            clock,
            lease_ttl=timedelta(seconds=settings.slot_lease_seconds),
            resource_profiles=telemetry_store,
        ),
        worker_inventory=ListWorkerInventory(control_backend, clock),
        identity_repository=identity,
        credential_issuer=credentials,
        control_backend=control_backend,
        telemetry_store=telemetry_store,
        telemetry_ingestor=telemetry_ingestor,
        operator_token=settings.operator_token,
        core_token=settings.core_token,
    )
