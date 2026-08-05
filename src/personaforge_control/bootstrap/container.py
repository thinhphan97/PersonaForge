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
    operator_token: str
    core_token: str

    async def startup(self) -> None:
        await self.identity_repository.startup()
        await self.credential_issuer.startup()
        await self.control_backend.startup()

    async def shutdown(self) -> None:
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
        ),
        worker_inventory=ListWorkerInventory(control_backend, clock),
        identity_repository=identity,
        credential_issuer=credentials,
        control_backend=control_backend,
        operator_token=settings.operator_token,
        core_token=settings.core_token,
    )
