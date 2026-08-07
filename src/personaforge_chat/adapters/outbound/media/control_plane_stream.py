import json
import time
from typing import Any

import httpx
from redis.asyncio import Redis

from personaforge_chat.application.dto import VoiceSummary, WebRtcAnswer, WebRtcOffer
from personaforge_chat.domain.errors import MediaCapacityUnavailable
from personaforge_chat.domain.models import AvatarState
from personaforge_observability import http_headers


class ControlPlaneAvatarStreamAdapter:
    """Route sessions through durable Control Plane assignments without local mapping."""

    def __init__(
        self,
        control_url: str,
        core_token: str,
        redis_url: str,
        livekit_url: str,
        resolution: str,
        fps: float,
        peak_vram_mb: int,
        gpu_duty_cycle: float,
        start_timeout_seconds: float,
    ) -> None:
        self._base_url = control_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {core_token}"}
        self._http = httpx.AsyncClient(timeout=15)
        self._redis: Redis = Redis.from_url(redis_url, decode_responses=True)
        self._livekit_url = livekit_url
        self._resolution = resolution
        self._fps = fps
        self._peak_vram_mb = peak_vram_mb
        self._gpu_duty_cycle = gpu_duty_cycle
        self._start_timeout_seconds = start_timeout_seconds

    async def start(
        self,
        session_id: str,
        character_id: str,
        voice: VoiceSummary,
        room_name: str,
        publisher_token: str,
    ) -> None:
        response = await self._http.post(
            f"{self._base_url}/internal/render-sessions/{session_id}/provision",
            headers=http_headers(self._headers),
            json={
                "character_id": character_id,
                "avatar_model_id": f"recorded-{character_id}",
                "tts_model_id": voice.model_id,
                "tts_revision": voice.model_revision,
                "voice_id": voice.voice_id,
                "runtime": "vieneu-v3-turbo",
                "runtime_version": voice.artifact_revision,
                "minimum_compute_capability": "0.0",
                "resolution": self._resolution,
                "fps": self._fps,
                "peak_vram_mb": self._peak_vram_mb,
                "gpu_duty_cycle": self._gpu_duty_cycle,
                "media_payload": {
                    "room_name": room_name,
                    "livekit_url": self._livekit_url,
                    "publisher_token": publisher_token,
                    "voice_provider": voice.provider,
                    "provider_voice_key": voice.provider_voice_key,
                    "voice_artifact_revision": voice.artifact_revision,
                    "voice_artifact_checksum": voice.artifact_checksum,
                    "speaker_embedding": list(voice.speaker_embedding),
                    "reference_codes": [list(frame) for frame in voice.reference_codes],
                },
            },
        )
        if response.status_code == 503:
            raise MediaCapacityUnavailable() from None
        response.raise_for_status()
        lease = response.json()
        await self._wait_event(
            lease["worker_id"],
            session_id,
            int(lease["generation"]),
            "avatar.session.ready",
            timeout_seconds=self._start_timeout_seconds,
            utterance_id=None,
        )

    async def negotiate(
        self,
        session_id: str,
        character_id: str,
        offer: WebRtcOffer,
    ) -> WebRtcAnswer:
        del session_id, character_id, offer
        raise RuntimeError("SDP negotiation is handled by LiveKit in livekit media mode")

    async def change_state(
        self,
        session_id: str,
        state: AvatarState,
        state_version: int,
    ) -> None:
        response = await self._http.post(
            f"{self._base_url}/internal/render-sessions/{session_id}/state",
            headers=http_headers(self._headers),
            json={"state": state.value, "state_version": state_version},
        )
        response.raise_for_status()

    async def speak(
        self,
        session_id: str,
        turn_id: str,
        utterance_id: str,
        text: str,
    ) -> None:
        response = await self._http.post(
            f"{self._base_url}/internal/render-sessions/{session_id}/speech",
            headers=http_headers(self._headers),
            json={"turn_id": turn_id, "utterance_id": utterance_id, "text": text},
        )
        response.raise_for_status()
        lease = response.json()
        await self._wait_event(
            lease["worker_id"],
            session_id,
            int(lease["generation"]),
            "avatar.speech.completed",
            timeout_seconds=max(30, len(text) * 0.08),
            utterance_id=utterance_id,
        )

    async def close(self, session_id: str) -> None:
        response = await self._http.delete(
            f"{self._base_url}/internal/render-sessions/{session_id}",
            headers=http_headers(self._headers),
        )
        response.raise_for_status()

    async def shutdown(self) -> None:
        await self._http.aclose()
        await self._redis.aclose()

    async def _wait_event(
        self,
        worker_id: str,
        session_id: str,
        generation: int,
        success_event: str,
        timeout_seconds: float,
        utterance_id: str | None,
    ) -> None:
        stream = f"avatar:events:{worker_id}:{session_id}"
        deadline = time.monotonic() + timeout_seconds
        cursor = "0-0"
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Worker event timed out session={session_id} event={success_event}"
                )
            batches = await self._redis.xread(
                streams={stream: cursor},
                count=20,
                block=max(1, min(round(remaining * 1_000), 1_000)),
            )
            for _stream, messages in batches:
                for event_id, fields in messages:
                    cursor = event_id
                    event = _decode_event(fields)
                    if event.get("generation") != generation:
                        continue
                    if utterance_id is not None and event.get("utterance_id") != utterance_id:
                        continue
                    if event.get("type") == success_event:
                        return
                    if event.get("type") == "avatar.session.failed":
                        raise RuntimeError(str(event.get("detail", "Render worker failed")))


def _decode_event(fields: dict[str, Any]) -> dict[str, Any]:
    raw = fields.get("data")
    if not isinstance(raw, str):
        return {}
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else {}
