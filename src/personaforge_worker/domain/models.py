from dataclasses import dataclass
from enum import Enum


class PersonaSessionState(str, Enum):
    RESERVED = "reserved"
    LOADING = "loading"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CompositeReservation:
    session_id: str
    slot_id: str
    lease_token: str
    generation: int
    reserved_vram_mb: int
    reserved_gpu_duty_cycle: float


@dataclass(frozen=True, slots=True)
class PersonaRuntimeSpec:
    character_id: str
    avatar_model_id: str
    tts_model_id: str
    voice_id: str
    runtime: str
    resolution: str
    fps: float


@dataclass(frozen=True, slots=True)
class CompositeStartRequest:
    reservation: CompositeReservation
    specification: PersonaRuntimeSpec
    media_payload: dict[str, str]


@dataclass(frozen=True, slots=True)
class CompositeSessionSnapshot:
    session_id: str
    slot_id: str
    generation: int
    state: PersonaSessionState
    reserved_vram_mb: int
    reserved_gpu_duty_cycle: float
