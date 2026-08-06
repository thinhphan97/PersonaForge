"""Smoke-test enrollment and worker-scoped Redis ACL inside the Compose network."""

import asyncio
import json
import os
import time
from uuid import uuid4

import httpx
from redis.asyncio import Redis
from redis.exceptions import NoPermissionError


async def main() -> None:
    control_url = os.getenv("CONTROL_SMOKE_URL", "http://personaforge-control:8090")
    operator_token = os.getenv(
        "CONTROL_OPERATOR_TOKEN",
        "development-operator-token-change-me",
    )
    redis_admin_url = os.getenv("CONTROL_REDIS_ADMIN_URL", "redis://redis:6379/0")
    suffix = uuid4().hex
    worker_id = f"smoke-worker-{suffix}"
    boot_id = f"boot-{suffix}"
    async with httpx.AsyncClient(timeout=10) as client:
        issued = await client.post(
            f"{control_url}/internal/worker-enrollment-tokens",
            headers={"Authorization": f"Bearer {operator_token}"},
            json={
                "worker_type": "persona-render",
                "cluster_id": "smoke",
                "host_id": "smoke-host",
                "max_gpu_count": 1,
                "ttl_seconds": 120,
            },
        )
        issued.raise_for_status()
        registered = await client.post(
            f"{control_url}/internal/workers/register",
            headers={"Authorization": f"Bearer {issued.json()['bootstrap_token']}"},
            json={
                "registration_request_id": f"request-{suffix}",
                "proposed_worker_id": worker_id,
                "boot_id": boot_id,
                "capabilities": {
                    "worker_type": "persona-render",
                    "host_id": "smoke-host",
                    "gpu_uuid": "GPU-smoke",
                    "gpu_name": "Smoke GPU",
                    "compute_capability": "0.0",
                    "total_vram_mb": 0,
                    "schedulable_vram_mb": 0,
                    "max_slots": 1,
                    "compute_budget": 0.5,
                    "supported_runtimes": ["scripted"],
                },
            },
        )
        registered.raise_for_status()
        identity = registered.json()
        renewed = await client.post(
            f"{control_url}/internal/workers/{worker_id}/renew",
            json={"boot_id": boot_id, "owner_token": identity["owner_token"]},
        )
        renewed.raise_for_status()
        credential = identity["credential"]
        redis = Redis.from_url(
            credential["redis_url"],
            username=credential["username"],
            password=credential["password"],
            decode_responses=True,
        )
        try:
            own_key = f"avatar:worker:{worker_id}:heartbeat"
            await redis.set(
                own_key,
                json.dumps(
                    {
                        "worker_id": worker_id,
                        "boot_id": boot_id,
                        "identity_generation": identity["identity_generation"],
                        "status": "ready",
                        "draining": False,
                        "active_slots": 0,
                        "reserved_slots": 0,
                        "reserved_vram_mb": 0,
                        "reserved_compute": 0,
                        "free_vram_mb": 0,
                        "queue_depth": 0,
                        "observed_fps": 25,
                        "gpu_used_vram_mb": 0,
                        "gpu_utilization": 0,
                        "gpu_memory_utilization": 0,
                        "gpu_temperature_c": None,
                        "gpu_power_watts": None,
                        "hardware_source": "smoke",
                        "hardware_sampled_at": time.time(),
                        "hardware_probe_error": None,
                    }
                ),
                ex=10,
            )
            assert await redis.get(own_key)
            inventory = await client.get(
                f"{control_url}/internal/workers",
                headers={"Authorization": f"Bearer {operator_token}"},
            )
            inventory.raise_for_status()
            observed = next(
                item for item in inventory.json() if item["worker_id"] == worker_id
            )
            assert observed["heartbeat"]["hardware_source"] == "smoke"
            try:
                await redis.set("avatar:worker:another-worker:heartbeat", "forbidden")
            except NoPermissionError:
                pass
            else:
                raise AssertionError("Worker ACL wrote outside its own namespace")
        finally:
            await redis.aclose()
            revoked = await client.request(
                "DELETE",
                f"{control_url}/internal/workers/{worker_id}",
                json={
                    "boot_id": boot_id,
                    "owner_token": identity["owner_token"],
                },
            )
            revoked.raise_for_status()
            admin = Redis.from_url(redis_admin_url)
            try:
                await admin.delete(
                    f"personaforge:control:worker-identity:{worker_id}",
                    f"avatar:worker:{worker_id}:heartbeat",
                )
                await admin.srem("personaforge:control:workers", worker_id)
            finally:
                await admin.aclose()
    print("control-plane enrollment and Redis ACL smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
