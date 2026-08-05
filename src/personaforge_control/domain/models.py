from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EnrollmentStatus(str, Enum):
    ISSUED = "issued"
    CLAIMING = "claiming"
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class WorkerStatus(str, Enum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class EnrollmentClaims:
    subject: str
    token_id: str
    cluster_id: str
    host_id: str
    worker_type: str
    max_gpu_count: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EnrollmentGrant:
    token_id: str
    subject: str
    cluster_id: str
    host_id: str
    worker_type: str
    max_gpu_count: int
    issued_at: datetime
    expires_at: datetime
    status: EnrollmentStatus = EnrollmentStatus.ISSUED
    registration_request_id: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerCapabilities:
    worker_type: str
    host_id: str
    gpu_uuid: str
    gpu_name: str
    compute_capability: str
    total_vram_mb: int
    schedulable_vram_mb: int
    max_slots: int
    compute_budget: float
    supported_runtimes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RedisWorkerCredential:
    username: str
    password: str
    redis_url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerRegistration:
    worker_id: str
    boot_id: str
    owner_token_hash: str
    management_token_hash: str
    management_family_id: str
    identity_generation: int
    capabilities: WorkerCapabilities
    status: WorkerStatus
    registered_at: datetime
    lease_expires_at: datetime
    redis_username: str


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    worker_id: str
    boot_id: str
    owner_token: str
    management_token: str
    management_family_id: str
    identity_generation: int
    credential: RedisWorkerCredential
    registration_expires_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerHeartbeat:
    worker_id: str
    boot_id: str
    identity_generation: int
    status: WorkerStatus
    draining: bool
    active_slots: int
    reserved_slots: int
    reserved_vram_mb: int
    reserved_compute: float
    free_vram_mb: int
    queue_depth: int
    observed_fps: float
    gpu_used_vram_mb: int
    gpu_utilization: float
    gpu_memory_utilization: float
    gpu_temperature_c: float | None
    gpu_power_watts: float | None
    hardware_source: str
    hardware_sampled_at: datetime
    hardware_probe_error: str | None
    heartbeat_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompositeRequirement:
    session_id: str
    character_id: str
    avatar_model_id: str
    tts_model_id: str
    runtime: str
    minimum_compute_capability: str
    resolution: str
    fps: float
    peak_vram_mb: int
    gpu_duty_cycle: float


@dataclass(frozen=True, slots=True)
class CompositeSlotLease:
    session_id: str
    worker_id: str
    slot_id: str
    lease_token: str
    generation: int
    reserved_vram_mb: int
    reserved_gpu_duty_cycle: float
    expires_at: datetime
