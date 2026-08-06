import hashlib
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx

from personaforge_worker.domain.hardware import GpuHardwareSnapshot, WorkerResourceSnapshot
from personaforge_worker.settings import WorkerSettings


@dataclass(frozen=True, slots=True)
class WorkerRedisIdentity:
    worker_id: str
    boot_id: str
    owner_token: str
    management_token: str
    management_family_id: str
    identity_generation: int
    redis_url: str
    redis_username: str
    redis_password: str


class WorkerIdentityClient:
    def __init__(self, settings: WorkerSettings) -> None:
        if settings.registration_url is None:
            raise ValueError("AVATAR_REGISTRATION_URL is required")
        self._settings = settings
        self._base_url = settings.registration_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=10)
        self._identity: WorkerRedisIdentity | None = None

    async def enroll(
        self,
        hardware: GpuHardwareSnapshot | None = None,
        resources: WorkerResourceSnapshot | None = None,
    ) -> WorkerRedisIdentity:
        persisted = _read_identity_state(self._settings.state_dir)
        if persisted is not None:
            try:
                identity = await self._reregister(persisted, hardware, resources)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 401 or not self._has_bootstrap_token():
                    raise
                worker_id = persisted.get("worker_id")
                if not isinstance(worker_id, str) or not worker_id:
                    raise RuntimeError("Persistent worker identity has no worker ID") from exc
                identity = await self._register(
                    hardware,
                    resources,
                    proposed_worker_id=worker_id,
                )
            _write_identity_state(self._settings.state_dir, identity)
            self._identity = identity
            return identity
        identity = await self._register(hardware, resources)
        _write_identity_state(self._settings.state_dir, identity)
        self._identity = identity
        return identity

    async def _register(
        self,
        hardware: GpuHardwareSnapshot | None,
        resources: WorkerResourceSnapshot | None,
        proposed_worker_id: str | None = None,
    ) -> WorkerRedisIdentity:
        token = self._read_bootstrap_token()
        host_id = self._settings.host_id or _stable_host_id()
        gpu_uuid = hardware.uuid if hardware is not None else self._settings.gpu_uuid
        instance_id = _read_or_create_instance_id(
            self._settings.state_dir,
            gpu_uuid,
        )
        proposed_worker_id = (
            proposed_worker_id
            or self._settings.worker_id
            or (f"{host_id}:{gpu_uuid}:{instance_id[:12]}")
        )
        boot_id = uuid4().hex
        request_id = uuid4().hex
        response = await self._client.post(
            f"{self._base_url}/internal/workers/register",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "registration_request_id": request_id,
                "proposed_worker_id": proposed_worker_id,
                "boot_id": boot_id,
                "capabilities": self._capabilities(host_id, hardware, resources),
            },
        )
        response.raise_for_status()
        return _identity_from_response(response.json())

    async def _reregister(
        self,
        persisted: dict[str, object],
        hardware: GpuHardwareSnapshot | None,
        resources: WorkerResourceSnapshot | None,
    ) -> WorkerRedisIdentity:
        worker_id = persisted.get("worker_id")
        management_token = persisted.get("management_token")
        if not isinstance(worker_id, str) or not isinstance(management_token, str):
            raise RuntimeError("Persistent worker identity is invalid")
        response = await self._client.post(
            f"{self._base_url}/internal/workers/reregister",
            headers={"Authorization": f"Bearer {management_token}"},
            json={
                "worker_id": worker_id,
                "boot_id": uuid4().hex,
                "capabilities": self._capabilities(
                    self._settings.host_id or _stable_host_id(),
                    hardware,
                    resources,
                ),
            },
        )
        response.raise_for_status()
        return _identity_from_response(response.json())

    async def renew(self) -> None:
        identity = self._require_identity()
        response = await self._client.post(
            f"{self._base_url}/internal/workers/{identity.worker_id}/renew",
            json={"boot_id": identity.boot_id, "owner_token": identity.owner_token},
        )
        response.raise_for_status()

    async def deregister(self) -> None:
        if self._identity is None:
            return
        identity = self._identity
        response = await self._client.request(
            "DELETE",
            f"{self._base_url}/internal/workers/{identity.worker_id}",
            json={"boot_id": identity.boot_id, "owner_token": identity.owner_token},
        )
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()

    def _read_bootstrap_token(self) -> str:
        if self._settings.bootstrap_token:
            return self._settings.bootstrap_token
        if self._settings.bootstrap_token_file:
            token = self._settings.bootstrap_token_file.read_text().strip()
            if token:
                return token
        raise RuntimeError(
            "AVATAR_WORKER_BOOTSTRAP_TOKEN or AVATAR_WORKER_BOOTSTRAP_TOKEN_FILE is required"
        )

    def _has_bootstrap_token(self) -> bool:
        if self._settings.bootstrap_token:
            return True
        token_file = self._settings.bootstrap_token_file
        return (
            token_file is not None and token_file.is_file() and bool(token_file.read_text().strip())
        )

    def _require_identity(self) -> WorkerRedisIdentity:
        if self._identity is None:
            raise RuntimeError("Worker has not enrolled")
        return self._identity

    def _capabilities(
        self,
        host_id: str,
        hardware: GpuHardwareSnapshot | None,
        resources: WorkerResourceSnapshot | None,
    ) -> dict[str, object]:
        gpu_uuid = hardware.uuid if hardware is not None else self._settings.gpu_uuid
        gpu_name = hardware.name if hardware is not None else self._settings.gpu_name
        compute_capability = (
            hardware.compute_capability
            if hardware is not None
            else self._settings.gpu_compute_capability
        )
        total_vram_mb = (
            hardware.total_vram_mb if hardware is not None else self._settings.total_vram_mb
        )
        configured_capacity = self._settings.schedulable_vram_mb
        schedulable_vram_mb = (
            configured_capacity
            if configured_capacity > 0
            else max(0, total_vram_mb - self._settings.gpu_reserve_vram_mb)
        )
        visible = list(resources.gpus) if resources is not None else []
        if hardware is not None and not any(item.uuid == hardware.uuid for item in visible):
            visible.insert(0, hardware)
        if hardware is not None:
            visible.sort(key=lambda item: item.uuid != hardware.uuid)
        visible = visible[: self._settings.maximum_gpu_count]

        def gpu_capability(item: GpuHardwareSnapshot) -> dict[str, object]:
            capacity = self._settings.schedulable_vram_mb
            return {
                "gpu_uuid": item.uuid,
                "gpu_name": item.name,
                "compute_capability": item.compute_capability,
                "total_vram_mb": item.total_vram_mb,
                "schedulable_vram_mb": (
                    capacity
                    if capacity > 0
                    else max(0, item.total_vram_mb - self._settings.gpu_reserve_vram_mb)
                ),
                "max_slots": self._settings.maximum_slots,
                "compute_budget": self._settings.compute_budget,
            }

        return {
            "worker_type": "persona-render",
            "host_id": host_id,
            "gpu_uuid": gpu_uuid,
            "gpu_name": gpu_name,
            "compute_capability": compute_capability,
            "total_vram_mb": total_vram_mb,
            "schedulable_vram_mb": schedulable_vram_mb,
            "max_slots": self._settings.maximum_slots,
            "compute_budget": self._settings.compute_budget,
            "supported_runtimes": ["recorded", "scripted"],
            "gpus": [gpu_capability(item) for item in visible],
        }


def _stable_host_id() -> str:
    machine_id = Path("/etc/machine-id")
    seed = machine_id.read_text().strip() if machine_id.is_file() else socket.gethostname()
    return f"host-{hashlib.sha256(seed.encode()).hexdigest()[:12]}"


def _read_or_create_instance_id(state_dir: Path, gpu_uuid: str) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    safe_gpu = hashlib.sha256(gpu_uuid.encode()).hexdigest()[:16]
    path = state_dir / f"instance-{safe_gpu}.id"
    if path.is_file():
        value = path.read_text().strip()
        if value:
            return value
    value = uuid4().hex
    temporary = path.with_suffix(".tmp")
    temporary.write_text(value)
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return value


def _write_identity_state(state_dir: Path, identity: WorkerRedisIdentity) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "identity.json"
    temporary = state_dir / "identity.json.tmp"
    temporary.write_text(
        json.dumps(
            {
                "worker_id": identity.worker_id,
                "management_token": identity.management_token,
                "management_token_family_id": identity.management_family_id,
                "identity_generation": identity.identity_generation,
            }
        )
    )
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _read_identity_state(state_dir: Path) -> dict[str, object] | None:
    path = state_dir / "identity.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError("Persistent worker identity must be a JSON object")
    return data


def _identity_from_response(data: dict[str, object]) -> WorkerRedisIdentity:
    credential = data.get("credential")
    if not isinstance(credential, dict):
        raise RuntimeError("Registration response has no Redis credential")
    return WorkerRedisIdentity(
        worker_id=str(data["worker_id"]),
        boot_id=str(data["boot_id"]),
        owner_token=str(data["owner_token"]),
        management_token=str(data["management_token"]),
        management_family_id=str(data["management_family_id"]),
        identity_generation=int(str(data["identity_generation"])),
        redis_url=str(credential["redis_url"]),
        redis_username=str(credential["username"]),
        redis_password=str(credential["password"]),
    )
