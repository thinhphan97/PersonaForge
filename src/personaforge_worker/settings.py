import os
from dataclasses import dataclass
from pathlib import Path

from personaforge_chat.domain.models import AvatarState


def _project_root() -> Path:
    configured = os.getenv("PROJECT_ROOT")
    if configured:
        return Path(configured).resolve()
    working_directory = Path.cwd()
    if working_directory.joinpath("pyproject.toml").is_file():
        return working_directory
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    role: str
    master_socket: Path
    master_max_children: int
    child_handshake_timeout_seconds: float
    child_heartbeat_interval_seconds: float
    child_heartbeat_timeout_seconds: float
    child_stop_grace_seconds: float
    child_command_queue_size: int
    worker_id: str
    redis_url: str
    registration_url: str | None
    bootstrap_token: str | None
    bootstrap_token_file: Path | None
    state_dir: Path
    host_id: str | None
    gpu_uuid: str
    gpu_name: str
    gpu_compute_capability: str
    total_vram_mb: int
    schedulable_vram_mb: int
    maximum_slots: int
    maximum_gpu_count: int
    supported_runtimes: tuple[str, ...]
    compute_budget: float
    registration_ttl_seconds: int
    heartbeat_interval_seconds: float
    telemetry_stream_maxlen: int
    gpu_probe_provider: str
    gpu_probe_timeout_seconds: float
    gpu_reserve_vram_mb: int
    video_width: int
    video_height: int
    video_fps: float
    video_bitrate: int
    audio_sample_rate: int
    audio_frame_duration_seconds: float
    audio_buffer_max_ms: int
    tts_runtime_provider: str
    vieneu_backend: str
    vieneu_model_path: str
    vieneu_onnx_precision: str
    vieneu_apply_watermark: bool
    lam_a2e_model_repo: str
    lam_a2e_model_revision: str
    lam_a2e_cache_dir: Path
    lam_a2e_allow_download: bool
    lam_a2e_source_dir: Path
    lam_a2e_device: str
    lam_a2e_motion_fps: int
    avatar_runtime_provider: str
    scripted_tts_ms_per_character: int
    video_queue_size_ms: float
    av_sync_debug: bool
    av_sync_interval_seconds: float
    av_sync_pulse_duration_seconds: float
    characters_root: Path
    avatar_clips: dict[AvatarState, Path]

    @classmethod
    def from_env(cls) -> "WorkerSettings":
        root = _project_root()
        default_assets = root / "assets" / "characters" / "default"
        return cls(
            role=os.getenv("PERSONA_WORKER_ROLE", "master").lower(),
            master_socket=Path(
                os.getenv(
                    "PERSONA_MASTER_SOCKET",
                    "/run/personaforge/worker-master/control.sock",
                )
            ),
            master_max_children=int(os.getenv("PERSONA_MASTER_MAX_CHILDREN", "32")),
            child_handshake_timeout_seconds=float(
                os.getenv("PERSONA_CHILD_HANDSHAKE_TIMEOUT_SECONDS", "10")
            ),
            child_heartbeat_interval_seconds=float(
                os.getenv("PERSONA_CHILD_HEARTBEAT_INTERVAL_SECONDS", "1")
            ),
            child_heartbeat_timeout_seconds=float(
                os.getenv("PERSONA_CHILD_HEARTBEAT_TIMEOUT_SECONDS", "5")
            ),
            child_stop_grace_seconds=float(os.getenv("PERSONA_CHILD_STOP_GRACE_SECONDS", "10")),
            child_command_queue_size=int(os.getenv("PERSONA_CHILD_COMMAND_QUEUE_SIZE", "100")),
            worker_id=os.getenv("AVATAR_WORKER_ID", os.getenv("WORKER_ID", "")),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            registration_url=os.getenv("AVATAR_REGISTRATION_URL") or None,
            bootstrap_token=os.getenv("AVATAR_WORKER_BOOTSTRAP_TOKEN") or None,
            bootstrap_token_file=(
                Path(value) if (value := os.getenv("AVATAR_WORKER_BOOTSTRAP_TOKEN_FILE")) else None
            ),
            state_dir=Path(
                os.getenv(
                    "AVATAR_WORKER_STATE_DIR",
                    "/tmp/personaforge/avatar-worker",
                )
            ),
            host_id=os.getenv("AVATAR_HOST_ID") or None,
            gpu_uuid=os.getenv("AVATAR_GPU_UUID", "GPU-none"),
            gpu_name=os.getenv("AVATAR_GPU_NAME", "CPU or unreported GPU"),
            gpu_compute_capability=os.getenv("AVATAR_GPU_COMPUTE_CAPABILITY", "0.0"),
            total_vram_mb=int(os.getenv("AVATAR_GPU_TOTAL_VRAM_MB", "0")),
            schedulable_vram_mb=int(os.getenv("AVATAR_GPU_SCHEDULABLE_VRAM_MB", "0")),
            maximum_slots=int(os.getenv("AVATAR_WORKER_MAX_SLOTS", "1")),
            maximum_gpu_count=int(os.getenv("AVATAR_WORKER_MAX_GPU_COUNT", "1")),
            supported_runtimes=tuple(
                runtime.strip()
                for runtime in os.getenv(
                    "AVATAR_WORKER_SUPPORTED_RUNTIMES",
                    "recorded,scripted,vieneu-v3-turbo",
                ).split(",")
                if runtime.strip()
            ),
            compute_budget=float(os.getenv("AVATAR_WORKER_COMPUTE_BUDGET", "0.85")),
            registration_ttl_seconds=int(os.getenv("AVATAR_WORKER_REGISTRATION_TTL_SECONDS", "15")),
            heartbeat_interval_seconds=float(
                os.getenv("AVATAR_WORKER_HEARTBEAT_INTERVAL_SECONDS", "5")
            ),
            telemetry_stream_maxlen=int(
                os.getenv("AVATAR_WORKER_TELEMETRY_STREAM_MAXLEN", "17280")
            ),
            gpu_probe_provider=os.getenv(
                "PERSONA_RESOURCE_PROBE_PROVIDER",
                os.getenv("AVATAR_GPU_PROBE_PROVIDER", "auto"),
            ).lower(),
            gpu_probe_timeout_seconds=float(
                os.getenv(
                    "PERSONA_RESOURCE_PROBE_TIMEOUT_SECONDS",
                    os.getenv("AVATAR_GPU_PROBE_TIMEOUT_SECONDS", "3"),
                )
            ),
            gpu_reserve_vram_mb=int(os.getenv("AVATAR_GPU_RESERVE_VRAM_MB", "512")),
            video_width=int(os.getenv("WORKER_VIDEO_WIDTH", "640")),
            video_height=int(os.getenv("WORKER_VIDEO_HEIGHT", "360")),
            video_fps=float(os.getenv("WORKER_VIDEO_FPS", "25")),
            video_bitrate=int(os.getenv("WORKER_VIDEO_BITRATE", "1500000")),
            audio_sample_rate=int(os.getenv("WORKER_AUDIO_SAMPLE_RATE", "48000")),
            audio_frame_duration_seconds=float(os.getenv("WORKER_AUDIO_FRAME_DURATION", "0.02")),
            audio_buffer_max_ms=int(os.getenv("TTS_AUDIO_BUFFER_MAX_MS", "2000")),
            tts_runtime_provider=os.getenv("TTS_RUNTIME_PROVIDER", "vieneu-v3-turbo").lower(),
            vieneu_backend=os.getenv("TTS_VIENEU_BACKEND", "onnx").lower(),
            vieneu_model_path=os.getenv("TTS_VIENEU_MODEL_PATH", "pnnbao-ump/VieNeu-TTS-v3-Turbo"),
            vieneu_onnx_precision=os.getenv("TTS_VIENEU_ONNX_PRECISION", "fp32").lower(),
            vieneu_apply_watermark=os.getenv("TTS_VIENEU_APPLY_WATERMARK", "false").lower()
            in {"1", "true", "yes", "on"},
            lam_a2e_model_repo=os.getenv("LAM_A2E_MODEL_REPO", "3DAIGC/LAM_audio2exp"),
            lam_a2e_model_revision=os.getenv(
                "LAM_A2E_MODEL_REVISION",
                "0fe5f4dbb283ec7d9c01688681e6e4b6ac314858",
            ),
            lam_a2e_cache_dir=Path(
                os.getenv("LAM_A2E_CACHE_DIR", "/var/cache/personaforge/lam")
            ),
            lam_a2e_allow_download=os.getenv("LAM_A2E_ALLOW_DOWNLOAD", "true").lower()
            in {"1", "true", "yes", "on"},
            lam_a2e_source_dir=Path(
                os.getenv("LAM_A2E_SOURCE_DIR", root / "third_party" / "LAM_Audio2Expression")
            ),
            lam_a2e_device=os.getenv("LAM_A2E_DEVICE", "cuda"),
            lam_a2e_motion_fps=int(os.getenv("LAM_A2E_MOTION_FPS", "30")),
            avatar_runtime_provider=os.getenv("AVATAR_RUNTIME_PROVIDER", "recorded").lower(),
            scripted_tts_ms_per_character=int(os.getenv("SCRIPTED_TTS_MS_PER_CHARACTER", "40")),
            video_queue_size_ms=float(os.getenv("WORKER_VIDEO_QUEUE_MS", "80")),
            av_sync_debug=os.getenv("AV_SYNC_DEBUG", "false").lower() in {"1", "true", "yes", "on"},
            av_sync_interval_seconds=float(os.getenv("AV_SYNC_INTERVAL", "2.0")),
            av_sync_pulse_duration_seconds=float(os.getenv("AV_SYNC_PULSE_DURATION", "0.1")),
            characters_root=Path(
                os.getenv("AVATAR_CHARACTERS_ROOT", root / "assets" / "characters")
            ),
            avatar_clips={
                AvatarState.IDLE: Path(os.getenv("AVATAR_IDLE_VIDEO", default_assets / "idle.mp4")),
                AvatarState.THINKING: Path(
                    os.getenv("AVATAR_THINKING_VIDEO", default_assets / "thinking.mp4")
                ),
                AvatarState.SPEAKING: Path(
                    os.getenv("AVATAR_SPEAKING_VIDEO", default_assets / "speaking.mp4")
                ),
            },
        )
