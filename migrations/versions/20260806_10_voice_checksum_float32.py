"""Canonicalize voice checksums to PostgreSQL REAL precision."""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "20260806_10"
down_revision = "20260806_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT voice_id, speaker_embedding, reference_codes,
                   reference_code_frames, reference_codebooks
            FROM system_voices
            """
        )
    ).mappings()
    for row in rows:
        codebooks = int(row["reference_codebooks"])
        frames = int(row["reference_code_frames"])
        flattened_codes = row["reference_codes"]
        codes = [
            flattened_codes[offset : offset + codebooks]
            for offset in range(0, frames * codebooks, codebooks)
        ]
        checksum = _checksum(row["speaker_embedding"], codes)
        connection.execute(
            sa.text(
                """
                UPDATE system_voices
                SET artifact_checksum = :checksum
                WHERE voice_id = :voice_id
                """
            ),
            {"checksum": checksum, "voice_id": row["voice_id"]},
        )


def downgrade() -> None:
    # The former checksums described pre-storage float64 values and could not
    # validate the float32 artifacts read back from PostgreSQL.
    pass


def _checksum(speaker_embedding: list[float], codes: list[list[int]]) -> str:
    payload = json.dumps(
        {
            # Values selected from a PostgreSQL REAL[] are already float32.
            "speaker_emb": [float(value) for value in speaker_embedding],
            "codes": [[int(value) for value in frame] for frame in codes],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
