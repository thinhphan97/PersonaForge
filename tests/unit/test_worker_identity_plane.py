from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from personaforge_control.application.identity import (
    EnrollmentService,
    WorkerRegistrationService,
)
from personaforge_control.domain.errors import (
    EnrollmentReplay,
    InvalidEnrollment,
    RegistrationLeaseLost,
)
from personaforge_control.domain.models import (
    EnrollmentClaims,
    EnrollmentGrant,
    GpuCapability,
    WorkerCapabilities,
)

from ..support.persona_control_memory import (
    InMemoryEnrollmentRepository,
    InMemoryWorkerCredentialIssuer,
    InMemoryWorkerRegistrationRepository,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class DeterministicSecrets:
    def __init__(self) -> None:
        self._counter = 0

    def generate(self) -> str:
        self._counter += 1
        return f"secret-{self._counter:04d}"

    def hash(self, value: str) -> str:
        return f"hash:{value}"


class FakeEnrollmentTokens:
    def __init__(self) -> None:
        self._grants: dict[str, EnrollmentGrant] = {}

    def issue(self, grant: EnrollmentGrant) -> str:
        token = f"bootstrap:{grant.token_id}"
        self._grants[token] = grant
        return token

    def decode(self, token: str) -> EnrollmentClaims:
        grant = self._grants.get(token)
        if grant is None:
            raise InvalidEnrollment("Invalid token")
        return EnrollmentClaims(
            subject=grant.subject,
            token_id=grant.token_id,
            cluster_id=grant.cluster_id,
            host_id=grant.host_id,
            worker_type=grant.worker_type,
            max_gpu_count=grant.max_gpu_count,
            expires_at=grant.expires_at,
        )


def capabilities() -> WorkerCapabilities:
    return WorkerCapabilities(
        worker_type="persona-render",
        host_id="gpu-host-a",
        gpu_uuid="GPU-test",
        gpu_name="Fake GPU",
        compute_capability="8.0",
        total_vram_mb=24_000,
        schedulable_vram_mb=20_000,
        max_slots=2,
        compute_budget=0.85,
        supported_runtimes=("recorded", "scripted"),
    )


def services() -> tuple[
    EnrollmentService,
    WorkerRegistrationService,
    InMemoryWorkerRegistrationRepository,
    InMemoryWorkerCredentialIssuer,
]:
    clock = FixedClock()
    secrets = DeterministicSecrets()
    enrollments = InMemoryEnrollmentRepository()
    registrations = InMemoryWorkerRegistrationRepository()
    credentials = InMemoryWorkerCredentialIssuer("redis://redis:6379/0", secrets)
    jwt = FakeEnrollmentTokens()
    return (
        EnrollmentService(enrollments, jwt, secrets, clock),
        WorkerRegistrationService(
            enrollments,
            registrations,
            credentials,
            jwt,
            secrets,
            clock,
        ),
        registrations,
        credentials,
    )


@pytest.mark.asyncio
async def test_bootstrap_token_is_one_time_and_same_request_is_idempotent() -> None:
    enrollment, registration, registrations, credentials = services()
    token = await enrollment.issue(
        subject="deployment-controller",
        cluster_id="development",
        host_id="gpu-host-a",
        worker_type="persona-render",
        max_gpu_count=1,
        ttl=timedelta(minutes=10),
    )

    first = await registration.register(
        token,
        "request-1",
        "gpu-host-a:GPU-test:persona-1",
        "boot-1",
        capabilities(),
    )
    retried = await registration.register(
        token,
        "request-1",
        "gpu-host-a:GPU-test:persona-1",
        "boot-1",
        capabilities(),
    )

    assert retried == first
    assert len(credentials.active) == 1
    persisted = await registrations.get(first.worker_id)
    assert persisted is not None
    assert persisted.owner_token_hash == f"hash:{first.owner_token}"

    with pytest.raises(EnrollmentReplay):
        await registration.register(
            token,
            "request-2",
            "gpu-host-a:GPU-test:persona-2",
            "boot-2",
            capabilities(),
        )


@pytest.mark.asyncio
async def test_registration_renew_and_deregister_are_owner_fenced() -> None:
    enrollment, registration, registrations, credentials = services()
    token = await enrollment.issue(
        "controller",
        "development",
        "gpu-host-a",
        "persona-render",
        1,
        timedelta(minutes=5),
    )
    result = await registration.register(
        token,
        "request-1",
        "configured-worker",
        "boot-1",
        capabilities(),
    )

    with pytest.raises(RegistrationLeaseLost):
        await registration.renew(result.worker_id, result.boot_id, "wrong-owner")

    await registration.renew(result.worker_id, result.boot_id, result.owner_token)
    current = await registrations.get(result.worker_id)
    assert current is not None
    assert current.status.value == "ready"

    await registration.deregister(result.worker_id, result.boot_id, result.owner_token)
    assert await registrations.get(result.worker_id) is None
    assert not credentials.active


@pytest.mark.asyncio
async def test_management_token_reregistration_rotates_token_and_boot_identity() -> None:
    enrollment, registration, _registrations, credentials = services()
    token = await enrollment.issue(
        "controller",
        "development",
        "gpu-host-a",
        "persona-render",
        1,
        timedelta(minutes=5),
    )
    first = await registration.register(
        token,
        "request-1",
        "configured-worker",
        "boot-1",
        capabilities(),
    )

    refreshed_capabilities = replace(capabilities(), total_vram_mb=23_500)
    restarted = await registration.reregister(
        first.worker_id,
        first.management_token,
        "boot-2",
        refreshed_capabilities,
    )

    assert restarted.boot_id == "boot-2"
    assert restarted.identity_generation == first.identity_generation + 1
    assert restarted.management_token != first.management_token
    persisted = await _registrations.get(restarted.worker_id)
    assert persisted is not None
    assert persisted.capabilities.total_vram_mb == 23_500
    assert first.credential.username not in credentials.active
    assert restarted.credential.username in credentials.active
    with pytest.raises(InvalidEnrollment):
        await registration.reregister(
            first.worker_id,
            first.management_token,
            "boot-replay",
        )


@pytest.mark.asyncio
async def test_registration_rejects_gpu_inventory_above_enrollment_grant() -> None:
    enrollment, registration, _registrations, _credentials = services()
    token = await enrollment.issue(
        "controller",
        "development",
        "gpu-host-a",
        "persona-render",
        1,
        timedelta(minutes=5),
    )
    gpu = capabilities()
    advertised = replace(
        gpu,
        gpus=(
            GpuCapability(
                gpu_uuid="GPU-1",
                gpu_name="GPU one",
                compute_capability="8.6",
                total_vram_mb=24_576,
                schedulable_vram_mb=24_064,
                max_slots=4,
                compute_budget=0.85,
            ),
            GpuCapability(
                gpu_uuid="GPU-2",
                gpu_name="GPU two",
                compute_capability="8.6",
                total_vram_mb=24_576,
                schedulable_vram_mb=24_064,
                max_slots=4,
                compute_budget=0.85,
            ),
        ),
    )

    with pytest.raises(InvalidEnrollment, match="more GPUs"):
        await registration.register(
            token,
            "request-too-many-gpus",
            "configured-worker",
            "boot-1",
            advertised,
        )
