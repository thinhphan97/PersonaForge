import hmac
import re
from datetime import timedelta

from personaforge_control.application.ports import (
    ClockPort,
    EnrollmentRepositoryPort,
    EnrollmentTokenPort,
    SecretPort,
    WorkerCredentialIssuerPort,
    WorkerRegistrationRepositoryPort,
)
from personaforge_control.domain.errors import (
    EnrollmentReplay,
    InvalidEnrollment,
    RegistrationLeaseLost,
    WorkerIdentityCollision,
)
from personaforge_control.domain.models import (
    EnrollmentGrant,
    RegistrationResult,
    WorkerCapabilities,
    WorkerRegistration,
    WorkerStatus,
)

_WORKER_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_.:-]+")


class EnrollmentService:
    def __init__(
        self,
        repository: EnrollmentRepositoryPort,
        tokens: EnrollmentTokenPort,
        secrets: SecretPort,
        clock: ClockPort,
        maximum_ttl: timedelta = timedelta(minutes=15),
    ) -> None:
        self._repository = repository
        self._tokens = tokens
        self._secrets = secrets
        self._clock = clock
        self._maximum_ttl = maximum_ttl

    async def issue(
        self,
        subject: str,
        cluster_id: str,
        host_id: str,
        worker_type: str,
        max_gpu_count: int,
        ttl: timedelta,
    ) -> str:
        if not subject or not cluster_id or not host_id or worker_type != "persona-render":
            raise InvalidEnrollment("Invalid enrollment token request")
        if max_gpu_count < 1 or ttl <= timedelta(0) or ttl > self._maximum_ttl:
            raise InvalidEnrollment("Enrollment constraints are outside the allowed range")
        now = self._clock.now()
        grant = EnrollmentGrant(
            token_id=self._secrets.generate(),
            subject=subject,
            cluster_id=cluster_id,
            host_id=host_id,
            worker_type=worker_type,
            max_gpu_count=max_gpu_count,
            issued_at=now,
            expires_at=now + ttl,
        )
        await self._repository.save(grant)
        return self._tokens.issue(grant)


class WorkerRegistrationService:
    def __init__(
        self,
        enrollments: EnrollmentRepositoryPort,
        registrations: WorkerRegistrationRepositoryPort,
        credentials: WorkerCredentialIssuerPort,
        tokens: EnrollmentTokenPort,
        secrets: SecretPort,
        clock: ClockPort,
        registration_ttl: timedelta = timedelta(seconds=15),
        credential_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        self._enrollments = enrollments
        self._registrations = registrations
        self._credentials = credentials
        self._tokens = tokens
        self._secrets = secrets
        self._clock = clock
        self._registration_ttl = registration_ttl
        self._credential_ttl = credential_ttl

    async def register(
        self,
        bootstrap_token: str,
        registration_request_id: str,
        proposed_worker_id: str,
        boot_id: str,
        capabilities: WorkerCapabilities,
    ) -> RegistrationResult:
        if not registration_request_id or not boot_id:
            raise InvalidEnrollment("Registration request and boot IDs are required")
        claims = self._tokens.decode(bootstrap_token)
        now = self._clock.now()
        if claims.expires_at <= now:
            raise InvalidEnrollment("Enrollment token expired")
        if capabilities.host_id != claims.host_id:
            raise InvalidEnrollment("Worker host does not match enrollment host")
        if capabilities.worker_type != claims.worker_type:
            raise InvalidEnrollment("Worker type does not match enrollment grant")

        existing_result = await self._enrollments.result(
            claims.token_id,
            registration_request_id,
        )
        if existing_result is not None:
            return existing_result
        claimed = await self._enrollments.claim(claims.token_id, registration_request_id, now)
        if claimed.registration_request_id != registration_request_id:
            raise EnrollmentReplay("Enrollment token was claimed by another request")

        worker_id = _canonical_worker_id(proposed_worker_id)
        current = await self._registrations.get(worker_id)
        if current is not None and current.lease_expires_at > now:
            raise WorkerIdentityCollision(f"Worker identity is active: {worker_id}")

        owner_token = self._secrets.generate()
        management_token = self._secrets.generate()
        family_id = self._secrets.generate()
        credential = await self._credentials.create(
            worker_id,
            boot_id,
            self._credential_ttl,
        )
        stable_identity = await self._registrations.get_identity(worker_id)
        generation = (
            1 if stable_identity is None else stable_identity.identity_generation + 1
        )
        registration = WorkerRegistration(
            worker_id=worker_id,
            boot_id=boot_id,
            owner_token_hash=self._secrets.hash(owner_token),
            management_token_hash=self._secrets.hash(management_token),
            management_family_id=family_id,
            identity_generation=generation,
            capabilities=capabilities,
            status=WorkerStatus.STARTING,
            registered_at=now,
            lease_expires_at=now + self._registration_ttl,
            redis_username=credential.username,
        )
        result = RegistrationResult(
            worker_id=worker_id,
            boot_id=boot_id,
            owner_token=owner_token,
            management_token=management_token,
            management_family_id=family_id,
            identity_generation=generation,
            credential=credential,
            registration_expires_at=registration.lease_expires_at,
        )
        try:
            await self._registrations.save(registration)
            await self._registrations.save_identity(registration)
            await self._enrollments.activate(
                claims.token_id,
                registration_request_id,
                result,
            )
        except BaseException:
            await self._credentials.revoke(credential.username)
            raise
        return result

    async def reregister(
        self,
        worker_id: str,
        management_token: str,
        boot_id: str,
        capabilities: WorkerCapabilities | None = None,
    ) -> RegistrationResult:
        if not worker_id or not management_token or not boot_id:
            raise InvalidEnrollment("Worker identity, management token and boot ID are required")
        identity = await self._registrations.get_identity(worker_id)
        if identity is None:
            raise InvalidEnrollment("Persistent worker identity was not found")
        if not hmac.compare_digest(
            identity.management_token_hash,
            self._secrets.hash(management_token),
        ):
            raise InvalidEnrollment("Management token is invalid or was already rotated")
        if capabilities is not None and (
            capabilities.worker_type != identity.capabilities.worker_type
            or capabilities.host_id != identity.capabilities.host_id
            or capabilities.gpu_uuid != identity.capabilities.gpu_uuid
        ):
            raise InvalidEnrollment(
                "Re-registration cannot change worker type, host ID or GPU UUID"
            )
        now = self._clock.now()
        previous_registration = await self._registrations.get(worker_id)
        owner_token = self._secrets.generate()
        next_management_token = self._secrets.generate()
        credential = await self._credentials.create(
            worker_id,
            boot_id,
            self._credential_ttl,
        )
        registration = WorkerRegistration(
            worker_id=worker_id,
            boot_id=boot_id,
            owner_token_hash=self._secrets.hash(owner_token),
            management_token_hash=self._secrets.hash(next_management_token),
            management_family_id=identity.management_family_id,
            identity_generation=identity.identity_generation + 1,
            capabilities=capabilities or identity.capabilities,
            status=WorkerStatus.STARTING,
            registered_at=now,
            lease_expires_at=now + self._registration_ttl,
            redis_username=credential.username,
        )
        result = RegistrationResult(
            worker_id=worker_id,
            boot_id=boot_id,
            owner_token=owner_token,
            management_token=next_management_token,
            management_family_id=registration.management_family_id,
            identity_generation=registration.identity_generation,
            credential=credential,
            registration_expires_at=registration.lease_expires_at,
        )
        try:
            await self._registrations.save(registration)
            await self._registrations.save_identity(registration)
        except BaseException:
            await self._credentials.revoke(credential.username)
            raise
        if (
            previous_registration is not None
            and previous_registration.redis_username != credential.username
        ):
            await self._credentials.revoke(previous_registration.redis_username)
        return result

    async def renew(
        self,
        worker_id: str,
        boot_id: str,
        owner_token: str,
    ) -> None:
        now = self._clock.now()
        renewed = await self._registrations.renew(
            worker_id,
            boot_id,
            self._secrets.hash(owner_token),
            now + self._registration_ttl,
        )
        if not renewed:
            raise RegistrationLeaseLost("Registration lease ownership changed")

    async def deregister(
        self,
        worker_id: str,
        boot_id: str,
        owner_token: str,
    ) -> None:
        registration = await self._registrations.get(worker_id)
        if registration is None:
            return
        owner_hash = self._secrets.hash(owner_token)
        deleted = await self._registrations.delete(worker_id, boot_id, owner_hash)
        if not deleted:
            raise RegistrationLeaseLost("Registration lease ownership changed")
        await self._credentials.revoke(registration.redis_username)


def _canonical_worker_id(value: str) -> str:
    normalized = _WORKER_ID_PATTERN.sub("-", value.strip()).strip("-.")
    if not normalized or len(normalized) > 180:
        raise InvalidEnrollment("Invalid proposed worker ID")
    return normalized
