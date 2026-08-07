"""Canonical serialization helpers for portable voice artifacts."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterable


def normalize_float32(value: float) -> float:
    """Round a number exactly as PostgreSQL REAL / NumPy float32 does."""

    return struct.unpack("!f", struct.pack("!f", float(value)))[0]


def voice_artifact_checksum(
    speaker_embedding: Iterable[float],
    reference_codes: Iterable[Iterable[int]],
) -> str:
    """Hash the canonical, storage-safe representation of a VieNeu voice."""

    payload = json.dumps(
        {
            "speaker_emb": [normalize_float32(value) for value in speaker_embedding],
            "codes": [[int(value) for value in frame] for frame in reference_codes],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
