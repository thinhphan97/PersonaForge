import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from personaforge_control.adapters.outbound.redis_control import RedisControlPlane
from personaforge_control.adapters.outbound.redis_identity import (
    RedisIdentityRepository,
    RedisWorkerRegistrationRepository,
)
from personaforge_control.domain.models import (
    CompositeRequirement,
    EnrollmentGrant,
    WorkerCapabilities,
    WorkerHeartbeat,
    WorkerRegistration,
    WorkerStatus,
)


def redis_url() -> str:
    value = os.getenv("TEST_REDIS_URL")
    if not value:
        pytest.skip("TEST_REDIS_URL is required for Redis control-plane integration")
    return value


@pytest.mark.asyncio
async def test_identity_assignment_and_fencing_survive_adapter_restart() -> None:
    url = redis_url()
    suffix = uuid4().hex
    token_id = f"enrollment-{suffix}"
    worker_id = f"worker-{suffix}"
    session_id = f"session-{suffix}"
    now = datetime.now(timezone.utc)
    identity = RedisIdentityRepository(url)
    registrations = RedisWorkerRegistrationRepository(identity)
    control = RedisControlPlane(url)
    await identity.startup()
    await control.startup()
    capabilities = WorkerCapabilities(
        worker_type="persona-render",
        host_id="host-test",
        gpu_uuid="GPU-test",
        gpu_name="Fake GPU",
        compute_capability="8.0",
        total_vram_mb=12_000,
        schedulable_vram_mb=10_000,
        max_slots=2,
        compute_budget=0.8,
        supported_runtimes=("scripted",),
    )
    registration = WorkerRegistration(
        worker_id=worker_id,
        boot_id="boot-1",
        owner_token_hash="owner-hash",
        management_token_hash="management-hash",
        management_family_id="family-1",
        identity_generation=1,
        capabilities=capabilities,
        status=WorkerStatus.READY,
        registered_at=now,
        lease_expires_at=now + timedelta(seconds=60),
        redis_username="test-user",
    )
    requirement = CompositeRequirement(
        session_id=session_id,
        character_id="luna",
        avatar_model_id="recorded-luna",
        tts_model_id="scripted-tone",
        runtime="scripted",
        minimum_compute_capability="8.0",
        resolution="640x360",
        fps=25,
        peak_vram_mb=4_000,
        gpu_duty_cycle=0.3,
    )
    try:
        await identity.save(
            EnrollmentGrant(
                token_id=token_id,
                subject="test",
                cluster_id="test",
                host_id="host-test",
                worker_type="persona-render",
                max_gpu_count=1,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )
        await registrations.save(registration)
        heartbeat = WorkerHeartbeat(
            worker_id=worker_id,
            boot_id="boot-1",
            identity_generation=1,
            status=WorkerStatus.READY,
            draining=False,
            active_slots=0,
            reserved_slots=0,
            reserved_vram_mb=0,
            reserved_compute=0,
            free_vram_mb=10_000,
            queue_depth=0,
            observed_fps=25,
            gpu_used_vram_mb=2_000,
            gpu_utilization=10,
            gpu_memory_utilization=15,
            gpu_temperature_c=55,
            gpu_power_watts=80,
            hardware_source="test",
            hardware_sampled_at=now,
            hardware_probe_error=None,
            heartbeat_at=now,
            expires_at=now + timedelta(seconds=60),
        )
        await control.heartbeat(heartbeat)
        inventory = await control.inventory(now)
        assert len(inventory) == 1
        assert inventory[0][1] is not None
        assert inventory[0][1].gpu_used_vram_mb == 2_000
        assert inventory[0][1].hardware_source == "test"
        candidates = await control.candidates(requirement, now)
        assert len(candidates) == 1
        await control.heartbeat(replace(heartbeat, free_vram_mb=1_000))
        assert not await control.candidates(requirement, now)
        await control.heartbeat(heartbeat)
        lease = await control.acquire(
            registration,
            heartbeat,
            requirement,
            now,
            timedelta(seconds=60),
        )
        assert lease is not None
        await control.save(lease)

        await control.shutdown()
        await identity.shutdown()
        restarted_identity = RedisIdentityRepository(url)
        restarted_control = RedisControlPlane(url)
        await restarted_identity.startup()
        await restarted_control.startup()
        try:
            persisted = await restarted_identity.get_registration(worker_id)
            assignment = await restarted_control.get(session_id)
            assert persisted is not None
            assert persisted.identity_generation == 1
            assert assignment == lease
            assert await restarted_control.release(lease)
            assert await restarted_control.delete(
                session_id,
                lease.lease_token,
                lease.generation,
            )
        finally:
            await restarted_control.shutdown()
            await restarted_identity.shutdown()
    finally:
        redis = Redis.from_url(url)
        await redis.delete(
            f"personaforge:control:enrollment:{token_id}",
            f"avatar:worker:{worker_id}:registration",
            f"personaforge:control:worker-identity:{worker_id}",
            f"avatar:worker:{worker_id}:heartbeat",
            f"personaforge:control:worker-usage:{worker_id}",
            f"personaforge:control:slot-lease:{session_id}",
            f"personaforge:control:session-generation:{session_id}",
            f"personaforge:control:assignment:{session_id}",
        )
        await redis.srem("personaforge:control:workers", worker_id)
        await redis.srem("personaforge:control:assignments", session_id)
        await redis.aclose()
