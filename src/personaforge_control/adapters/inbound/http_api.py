import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from personaforge_control.bootstrap.container import ControlContainer
from personaforge_control.domain.errors import (
    ControlPlaneError,
    EnrollmentReplay,
    InvalidEnrollment,
    NoWorkerCapacity,
    RegistrationLeaseLost,
    SlotLeaseLost,
    WorkerIdentityCollision,
)
from personaforge_control.domain.models import (
    CompositeRequirement,
    CompositeSlotLease,
    GpuCapability,
    WorkerCapabilities,
)


class EnrollmentTokenRequest(BaseModel):
    worker_type: str = "persona-render"
    cluster_id: str = Field(min_length=1, max_length=100)
    host_id: str = Field(min_length=1, max_length=180)
    max_gpu_count: int = Field(default=1, ge=1, le=64)
    ttl_seconds: int = Field(default=600, ge=30, le=900)


class EnrollmentTokenResponse(BaseModel):
    bootstrap_token: str
    token_type: str = "Bearer"


class GpuCapabilityRequest(BaseModel):
    gpu_uuid: str
    gpu_name: str
    compute_capability: str
    total_vram_mb: int = Field(ge=0)
    schedulable_vram_mb: int = Field(ge=0)
    max_slots: int = Field(ge=1)
    compute_budget: float = Field(gt=0, le=1)


class WorkerCapabilitiesRequest(GpuCapabilityRequest):
    worker_type: str = "persona-render"
    host_id: str
    gpu_uuid: str
    gpu_name: str
    compute_capability: str
    total_vram_mb: int = Field(ge=0)
    schedulable_vram_mb: int = Field(ge=0)
    max_slots: int = Field(ge=1)
    compute_budget: float = Field(gt=0, le=1)
    supported_runtimes: tuple[str, ...] = ()
    gpus: tuple[GpuCapabilityRequest, ...] = ()


class WorkerRegistrationRequest(BaseModel):
    registration_request_id: str = Field(min_length=1, max_length=180)
    proposed_worker_id: str = Field(min_length=1, max_length=180)
    boot_id: str = Field(min_length=1, max_length=180)
    capabilities: WorkerCapabilitiesRequest


class WorkerReregistrationRequest(BaseModel):
    worker_id: str = Field(min_length=1, max_length=180)
    boot_id: str = Field(min_length=1, max_length=180)
    capabilities: WorkerCapabilitiesRequest | None = None


class RedisCredentialResponse(BaseModel):
    username: str
    password: str
    redis_url: str
    expires_at: str


class WorkerRegistrationResponse(BaseModel):
    worker_id: str
    boot_id: str
    owner_token: str
    management_token: str
    management_family_id: str
    identity_generation: int
    credential: RedisCredentialResponse
    registration_expires_at: str


class RegistrationRenewRequest(BaseModel):
    boot_id: str
    owner_token: str


class WorkerHeartbeatResponse(BaseModel):
    status: str
    draining: bool
    active_slots: int
    reserved_vram_mb: int
    reserved_compute: float
    free_vram_mb: int
    gpu_used_vram_mb: int
    gpu_utilization: float
    gpu_memory_utilization: float
    gpu_temperature_c: float | None
    gpu_power_watts: float | None
    observed_fps: float
    hardware_source: str
    hardware_sampled_at: str
    hardware_probe_error: str | None
    expires_at: str
    reserved_slots: int
    queue_depth: int
    heartbeat_at: str
    host_resources: dict[str, object] | None
    gpu_resources: list[dict[str, object]]
    child_resources: list[dict[str, object]]


class WorkerInventoryResponse(BaseModel):
    worker_id: str
    boot_id: str
    identity_generation: int
    status: str
    registration_expires_at: str
    capabilities: WorkerCapabilitiesRequest
    heartbeat: WorkerHeartbeatResponse | None


class CompositeProvisionRequest(BaseModel):
    character_id: str
    avatar_model_id: str = "recorded-avatar"
    tts_model_id: str = "scripted-tone"
    runtime: str = "scripted"
    minimum_compute_capability: str = "0.0"
    resolution: str = "640x360"
    fps: float = Field(default=25, gt=0, le=120)
    peak_vram_mb: int = Field(default=0, ge=0)
    gpu_duty_cycle: float = Field(default=0.1, gt=0, le=1)
    voice_id: str = "default"
    avatar_revision: str = "default"
    tts_revision: str = "default"
    runtime_version: str = "default"
    precision: str = "fp32"
    isolation_mode: str = "subprocess"
    media_payload: dict[str, str]


class CompositeLeaseResponse(BaseModel):
    session_id: str
    worker_id: str
    slot_id: str
    lease_token: str
    generation: int
    reserved_vram_mb: int
    reserved_gpu_duty_cycle: float
    expires_at: str
    gpu_uuid: str
    resource_source: str
    resource_profile_id: str | None


class CompositeStateRequest(BaseModel):
    state: str
    state_version: int = Field(ge=0)


class CompositeSpeechRequest(BaseModel):
    turn_id: str
    utterance_id: str
    text: str = Field(min_length=1, max_length=100_000)


class PersonaResourceConfigRequest(BaseModel):
    character_id: str
    avatar_model_id: str
    avatar_revision: str = "default"
    voice_id: str = "default"
    tts_model_id: str
    tts_revision: str = "default"
    runtime: str
    runtime_version: str = "default"
    precision: str = "fp32"
    resolution: str = "640x360"
    fps: float = Field(default=25, gt=0, le=120)
    isolation_mode: str = "subprocess"
    gpu_name: str | None = None
    minimum_compute_capability: str = "0.0"
    manifest_peak_vram_mb: int = Field(ge=0)
    manifest_gpu_duty_cycle: float = Field(gt=0, le=1)
    bootstrap_vram_margin_mb: int = Field(default=512, ge=0)
    bootstrap_compute_margin: float = Field(default=0.05, ge=0, le=1)
    active: bool = True


def create_control_app(container: ControlContainer) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await container.startup()
        try:
            yield
        finally:
            await container.shutdown()

    app = FastAPI(title="PersonaForge Control Plane", lifespan=lifespan)

    def require_operator(authorization: Annotated[str | None, Header()] = None) -> str:
        expected = f"Bearer {container.operator_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return "operator"

    def require_core(authorization: Annotated[str | None, Header()] = None) -> str:
        expected = f"Bearer {container.core_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return "core"

    @app.exception_handler(ControlPlaneError)
    async def control_error_handler(_request: object, exc: ControlPlaneError) -> JSONResponse:
        status_code = status.HTTP_400_BAD_REQUEST
        if isinstance(exc, EnrollmentReplay | WorkerIdentityCollision):
            status_code = status.HTTP_409_CONFLICT
        elif isinstance(exc, RegistrationLeaseLost):
            status_code = status.HTTP_412_PRECONDITION_FAILED
        elif isinstance(exc, SlotLeaseLost):
            status_code = status.HTTP_412_PRECONDITION_FAILED
        elif isinstance(exc, NoWorkerCapacity):
            status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        elif isinstance(exc, InvalidEnrollment):
            status_code = status.HTTP_401_UNAUTHORIZED
        return JSONResponse(
            content={"detail": str(exc)},
            status_code=status_code,
        )

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/internal/workers",
        response_model=list[WorkerInventoryResponse],
        dependencies=[Depends(require_operator)],
    )
    async def list_workers() -> list[WorkerInventoryResponse]:
        snapshots = await container.worker_inventory.execute()
        return [
            WorkerInventoryResponse(
                worker_id=registration.worker_id,
                boot_id=registration.boot_id,
                identity_generation=registration.identity_generation,
                status=registration.status.value,
                registration_expires_at=registration.lease_expires_at.isoformat(),
                capabilities=WorkerCapabilitiesRequest(
                    worker_type=registration.capabilities.worker_type,
                    host_id=registration.capabilities.host_id,
                    gpu_uuid=registration.capabilities.gpu_uuid,
                    gpu_name=registration.capabilities.gpu_name,
                    compute_capability=registration.capabilities.compute_capability,
                    total_vram_mb=registration.capabilities.total_vram_mb,
                    schedulable_vram_mb=(
                        registration.capabilities.schedulable_vram_mb
                    ),
                    max_slots=registration.capabilities.max_slots,
                    compute_budget=registration.capabilities.compute_budget,
                    supported_runtimes=registration.capabilities.supported_runtimes,
                    gpus=tuple(
                        GpuCapabilityRequest(
                            gpu_uuid=gpu.gpu_uuid,
                            gpu_name=gpu.gpu_name,
                            compute_capability=gpu.compute_capability,
                            total_vram_mb=gpu.total_vram_mb,
                            schedulable_vram_mb=gpu.schedulable_vram_mb,
                            max_slots=gpu.max_slots,
                            compute_budget=gpu.compute_budget,
                        )
                        for gpu in registration.capabilities.advertised_gpus()
                    ),
                ),
                heartbeat=(
                    WorkerHeartbeatResponse(
                        status=heartbeat.status.value,
                        draining=heartbeat.draining,
                        active_slots=heartbeat.active_slots,
                        reserved_vram_mb=heartbeat.reserved_vram_mb,
                        reserved_compute=heartbeat.reserved_compute,
                        free_vram_mb=heartbeat.free_vram_mb,
                        gpu_used_vram_mb=heartbeat.gpu_used_vram_mb,
                        gpu_utilization=heartbeat.gpu_utilization,
                        gpu_memory_utilization=heartbeat.gpu_memory_utilization,
                        gpu_temperature_c=heartbeat.gpu_temperature_c,
                        gpu_power_watts=heartbeat.gpu_power_watts,
                        observed_fps=heartbeat.observed_fps,
                        hardware_source=heartbeat.hardware_source,
                        hardware_sampled_at=heartbeat.hardware_sampled_at.isoformat(),
                        hardware_probe_error=heartbeat.hardware_probe_error,
                        expires_at=heartbeat.expires_at.isoformat(),
                        reserved_slots=heartbeat.reserved_slots,
                        queue_depth=heartbeat.queue_depth,
                        heartbeat_at=heartbeat.heartbeat_at.isoformat(),
                        host_resources=heartbeat.host_resources,
                        gpu_resources=[
                            {
                                "gpu_uuid": gpu.gpu_uuid,
                                "gpu_name": gpu.gpu_name,
                                "compute_capability": gpu.compute_capability,
                                "total_vram_mb": gpu.total_vram_mb,
                                "free_vram_mb": gpu.free_vram_mb,
                                "used_vram_mb": gpu.used_vram_mb,
                                "gpu_utilization": gpu.gpu_utilization,
                                "memory_utilization": gpu.memory_utilization,
                                "encoder_utilization": gpu.encoder_utilization,
                                "decoder_utilization": gpu.decoder_utilization,
                                "temperature_c": gpu.temperature_c,
                                "power_watts": gpu.power_watts,
                                "healthy": gpu.healthy,
                            }
                            for gpu in heartbeat.gpu_resources
                        ],
                        child_resources=list(heartbeat.child_resources),
                    )
                    if heartbeat is not None
                    else None
                ),
            )
            for registration, heartbeat in snapshots
        ]

    @app.post(
        "/internal/worker-enrollment-tokens",
        response_model=EnrollmentTokenResponse,
        dependencies=[Depends(require_operator)],
    )
    async def issue_enrollment(request: EnrollmentTokenRequest) -> EnrollmentTokenResponse:
        token = await container.enrollment.issue(
            subject="operator",
            cluster_id=request.cluster_id,
            host_id=request.host_id,
            worker_type=request.worker_type,
            max_gpu_count=request.max_gpu_count,
            ttl=timedelta(seconds=request.ttl_seconds),
        )
        return EnrollmentTokenResponse(bootstrap_token=token)

    @app.put(
        "/internal/resource-configs",
        dependencies=[Depends(require_operator)],
    )
    async def upsert_resource_config(
        request: PersonaResourceConfigRequest,
    ) -> dict[str, str]:
        config_id = await container.telemetry_store.upsert_config(request.model_dump())
        return {"config_id": config_id}

    @app.get(
        "/internal/resource-profiles",
        dependencies=[Depends(require_operator)],
    )
    async def list_resource_profiles(limit: int = 100) -> list[dict[str, object]]:
        return await container.telemetry_store.list_profiles(min(max(limit, 1), 1000))

    @app.get(
        "/internal/resource-configs",
        dependencies=[Depends(require_operator)],
    )
    async def list_resource_configs(limit: int = 100) -> list[dict[str, object]]:
        return await container.telemetry_store.list_configs(min(max(limit, 1), 1000))

    @app.get(
        "/internal/telemetry/status",
        dependencies=[Depends(require_operator)],
    )
    async def telemetry_status() -> dict[str, object]:
        return {
            "redis": await container.telemetry_ingestor.status(),
            "postgres": await container.telemetry_store.statistics(),
        }

    @app.post("/internal/workers/register", response_model=WorkerRegistrationResponse)
    async def register_worker(
        request: WorkerRegistrationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerRegistrationResponse:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        result = await container.registration.register(
            authorization.removeprefix("Bearer "),
            request.registration_request_id,
            request.proposed_worker_id,
            request.boot_id,
            _capabilities_from_request(request.capabilities),
        )
        return WorkerRegistrationResponse(
            worker_id=result.worker_id,
            boot_id=result.boot_id,
            owner_token=result.owner_token,
            management_token=result.management_token,
            management_family_id=result.management_family_id,
            identity_generation=result.identity_generation,
            credential=RedisCredentialResponse(
                username=result.credential.username,
                password=result.credential.password,
                redis_url=result.credential.redis_url,
                expires_at=result.credential.expires_at.isoformat(),
            ),
            registration_expires_at=result.registration_expires_at.isoformat(),
        )

    @app.post("/internal/workers/reregister", response_model=WorkerRegistrationResponse)
    async def reregister_worker(
        request: WorkerReregistrationRequest,
        authorization: Annotated[str | None, Header()] = None,
    ) -> WorkerRegistrationResponse:
        if authorization is None or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        result = await container.registration.reregister(
            request.worker_id,
            authorization.removeprefix("Bearer "),
            request.boot_id,
            (
                _capabilities_from_request(request.capabilities)
                if request.capabilities is not None
                else None
            ),
        )
        return WorkerRegistrationResponse(
            worker_id=result.worker_id,
            boot_id=result.boot_id,
            owner_token=result.owner_token,
            management_token=result.management_token,
            management_family_id=result.management_family_id,
            identity_generation=result.identity_generation,
            credential=RedisCredentialResponse(
                username=result.credential.username,
                password=result.credential.password,
                redis_url=result.credential.redis_url,
                expires_at=result.credential.expires_at.isoformat(),
            ),
            registration_expires_at=result.registration_expires_at.isoformat(),
        )

    @app.post("/internal/workers/{worker_id}/renew", status_code=204)
    async def renew_worker(worker_id: str, request: RegistrationRenewRequest) -> Response:
        await container.registration.renew(worker_id, request.boot_id, request.owner_token)
        return Response(status_code=204)

    @app.delete("/internal/workers/{worker_id}", status_code=204)
    async def deregister_worker(worker_id: str, request: RegistrationRenewRequest) -> Response:
        await container.registration.deregister(worker_id, request.boot_id, request.owner_token)
        return Response(status_code=204)

    @app.post(
        "/internal/render-sessions/{session_id}/provision",
        response_model=CompositeLeaseResponse,
        dependencies=[Depends(require_core)],
    )
    async def provision_composite(
        session_id: str,
        request: CompositeProvisionRequest,
    ) -> CompositeLeaseResponse:
        lease = await container.scheduler.provision(
            CompositeRequirement(
                session_id=session_id,
                character_id=request.character_id,
                avatar_model_id=request.avatar_model_id,
                tts_model_id=request.tts_model_id,
                runtime=request.runtime,
                minimum_compute_capability=request.minimum_compute_capability,
                resolution=request.resolution,
                fps=request.fps,
                peak_vram_mb=request.peak_vram_mb,
                gpu_duty_cycle=request.gpu_duty_cycle,
                voice_id=request.voice_id,
                avatar_revision=request.avatar_revision,
                tts_revision=request.tts_revision,
                runtime_version=request.runtime_version,
                precision=request.precision,
                isolation_mode=request.isolation_mode,
            ),
            dict(request.media_payload),
        )
        return _lease_response(lease)

    @app.post(
        "/internal/render-sessions/{session_id}/renew",
        response_model=CompositeLeaseResponse,
        dependencies=[Depends(require_core)],
    )
    async def renew_composite(session_id: str) -> CompositeLeaseResponse:
        lease = await container.scheduler.renew(session_id)
        return _lease_response(lease)

    @app.post(
        "/internal/render-sessions/{session_id}/state",
        response_model=CompositeLeaseResponse,
        dependencies=[Depends(require_core)],
    )
    async def change_composite_state(
        session_id: str,
        request: CompositeStateRequest,
    ) -> CompositeLeaseResponse:
        lease = await container.scheduler.change_state(
            session_id,
            request.state,
            request.state_version,
        )
        return _lease_response(lease)

    @app.post(
        "/internal/render-sessions/{session_id}/speech",
        response_model=CompositeLeaseResponse,
        dependencies=[Depends(require_core)],
    )
    async def synthesize_composite_speech(
        session_id: str,
        request: CompositeSpeechRequest,
    ) -> CompositeLeaseResponse:
        lease = await container.scheduler.speak(
            session_id,
            request.turn_id,
            request.utterance_id,
            request.text,
        )
        return _lease_response(lease)

    @app.delete(
        "/internal/render-sessions/{session_id}",
        status_code=204,
        dependencies=[Depends(require_core)],
    )
    async def release_composite(session_id: str) -> Response:
        await container.scheduler.release(session_id)
        return Response(status_code=204)

    return app


def _capabilities_from_request(request: WorkerCapabilitiesRequest) -> WorkerCapabilities:
    data = request.model_dump(exclude={"gpus"})
    return WorkerCapabilities(
        **data,
        gpus=tuple(GpuCapability(**gpu.model_dump()) for gpu in request.gpus),
    )


def _lease_response(lease: CompositeSlotLease) -> CompositeLeaseResponse:
    return CompositeLeaseResponse(
        session_id=lease.session_id,
        worker_id=lease.worker_id,
        gpu_uuid=lease.gpu_uuid,
        slot_id=lease.slot_id,
        lease_token=lease.lease_token,
        generation=lease.generation,
        reserved_vram_mb=lease.reserved_vram_mb,
        reserved_gpu_duty_cycle=lease.reserved_gpu_duty_cycle,
        resource_source=lease.resource_source,
        resource_profile_id=lease.resource_profile_id,
        expires_at=lease.expires_at.isoformat(),
    )
