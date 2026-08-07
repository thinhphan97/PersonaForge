"""Store VieNeu system voice artifacts and bind sessions to a voice."""

import hashlib
import json
import struct
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "20260806_09"
down_revision = "20260805_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    voices = op.create_table(
        "system_voices",
        sa.Column("voice_id", sa.String(100), primary_key=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("provider_voice_key", sa.String(200), nullable=False),
        sa.Column("model_id", sa.String(100), nullable=False),
        sa.Column("model_revision", sa.String(100), nullable=False),
        sa.Column("artifact_revision", sa.String(100), nullable=False),
        sa.Column("artifact_checksum", sa.String(64), nullable=False),
        sa.Column("speaker_embedding", sa.ARRAY(sa.REAL()), nullable=False),
        sa.Column("reference_codes", sa.ARRAY(sa.SmallInteger()), nullable=False),
        sa.Column("reference_code_frames", sa.Integer(), nullable=False),
        sa.Column("reference_codebooks", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    source = json.loads(
        (Path(__file__).resolve().parents[2] / "config" / "system_voices.v3.2.4.json").read_text(
            encoding="utf-8"
        )
    )
    model_revision = source["source_revision"]
    rows = [
        ("system-vi-minh-duc", "Minh Đức", "Nam · Bắc · Tin tức", "Minh Đức", True),
        ("system-vi-pham-tuyen", "Phạm Tuyên", "Nam · Bắc · Tự nhiên", "Phạm Tuyên", False),
        ("system-vi-truc-ly", "Trúc Ly", "Nữ · Bắc · Tự nhiên", "Trúc Ly", False),
        ("system-vi-doan-trang", "Đoan Trang", "Nữ · Bắc · Tự nhiên", "Đoan Trang", False),
        ("system-vi-xuan-vinh", "Xuân Vĩnh", "Nam · Nam · Tự nhiên", "Xuân Vĩnh", False),
        ("system-vi-ngoc-tran", "Ngọc Trân", "Nữ · Trung · Tự nhiên", "Ngọc Trân", False),
    ]
    op.bulk_insert(
        voices,
        [
            {
                "voice_id": voice_id,
                "display_name": name,
                "description": description,
                "language": "vi",
                "provider": "vieneu",
                "provider_voice_key": provider_key,
                "model_id": "vieneu-v3-turbo",
                "model_revision": model_revision,
                "artifact_revision": "v3.2.4",
                "artifact_checksum": _artifact_checksum(source["presets"][provider_key]),
                "speaker_embedding": source["presets"][provider_key]["speaker_emb"],
                "reference_codes": [
                    value for frame in source["presets"][provider_key]["codes"] for value in frame
                ],
                "reference_code_frames": len(source["presets"][provider_key]["codes"]),
                "reference_codebooks": len(source["presets"][provider_key]["codes"][0]),
                "enabled": True,
                "is_default": is_default,
            }
            for voice_id, name, description, provider_key, is_default in rows
        ],
    )
    op.add_column(
        "avatar_sessions",
        sa.Column(
            "voice_id",
            sa.String(100),
            sa.ForeignKey("system_voices.voice_id"),
            nullable=False,
            server_default="system-vi-minh-duc",
        ),
    )


def downgrade() -> None:
    op.drop_column("avatar_sessions", "voice_id")
    op.drop_table("system_voices")


def _artifact_checksum(preset: dict[str, object]) -> str:
    payload = json.dumps(
        {
            "speaker_emb": [
                struct.unpack("!f", struct.pack("!f", float(value)))[0]
                for value in preset["speaker_emb"]
            ],
            "codes": preset["codes"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()
