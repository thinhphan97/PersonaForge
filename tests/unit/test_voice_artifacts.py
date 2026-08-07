import json
from pathlib import Path

from personaforge.voice_artifacts import normalize_float32, voice_artifact_checksum


def test_voice_checksum_is_stable_after_postgresql_real_roundtrip() -> None:
    source = json.loads(Path("config/system_voices.v3.2.4.json").read_text(encoding="utf-8"))[
        "presets"
    ]["Minh Đức"]
    stored_embedding = [normalize_float32(value) for value in source["speaker_emb"]]

    assert voice_artifact_checksum(
        source["speaker_emb"], source["codes"]
    ) == voice_artifact_checksum(stored_embedding, source["codes"])
