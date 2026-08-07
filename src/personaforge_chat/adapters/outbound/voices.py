import json
import os
from pathlib import Path

from personaforge.voice_artifacts import normalize_float32, voice_artifact_checksum
from personaforge_chat.application.dto import VoiceSummary

_MODEL_REVISION = "a8c9fbf99749d5ce45c89111f71558d6ceef3424"
_VOICE_IDS = {
    "Minh Đức": ("system-vi-minh-duc", "Nam · Bắc · Tin tức"),
    "Phạm Tuyên": ("system-vi-pham-tuyen", "Nam · Bắc · Tự nhiên"),
    "Trúc Ly": ("system-vi-truc-ly", "Nữ · Bắc · Tự nhiên"),
    "Đoan Trang": ("system-vi-doan-trang", "Nữ · Bắc · Tự nhiên"),
    "Xuân Vĩnh": ("system-vi-xuan-vinh", "Nam · Nam · Tự nhiên"),
    "Ngọc Trân": ("system-vi-ngoc-tran", "Nữ · Trung · Tự nhiên"),
}


def load_system_voice_fixtures() -> tuple[VoiceSummary, ...]:
    root = Path(os.getenv("PROJECT_ROOT", Path.cwd()))
    source = json.loads(
        root.joinpath("config", "system_voices.v3.2.4.json").read_text(encoding="utf-8")
    )
    voices: list[VoiceSummary] = []
    for provider_key, (voice_id, description) in _VOICE_IDS.items():
        artifact = source["presets"][provider_key]
        voices.append(
            VoiceSummary(
                voice_id=voice_id,
                display_name=provider_key,
                description=description,
                language="vi",
                provider="vieneu",
                provider_voice_key=provider_key,
                model_id="vieneu-v3-turbo",
                model_revision=_MODEL_REVISION,
                artifact_revision="v3.2.4",
                artifact_checksum=voice_artifact_checksum(
                    artifact["speaker_emb"], artifact["codes"]
                ),
                speaker_embedding=tuple(
                    normalize_float32(value) for value in artifact["speaker_emb"]
                ),
                reference_codes=tuple(
                    tuple(int(value) for value in frame) for frame in artifact["codes"]
                ),
                is_default=provider_key == source["default_voice"],
            )
        )
    return tuple(voices)


class InMemorySystemVoiceCatalog:
    """Test adapter only; PostgreSQL is the production source of truth."""

    def __init__(self) -> None:
        self._voices = load_system_voice_fixtures()
        self._by_id = {voice.voice_id: voice for voice in self._voices}

    async def get(self, voice_id: str) -> VoiceSummary | None:
        return self._by_id.get(voice_id)

    async def list(self) -> tuple[VoiceSummary, ...]:
        return self._voices

    async def default(self) -> VoiceSummary:
        return next(voice for voice in self._voices if voice.is_default)
