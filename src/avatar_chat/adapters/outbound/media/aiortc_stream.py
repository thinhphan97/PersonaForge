import asyncio
from dataclasses import dataclass

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from avatar_chat.adapters.outbound.media.media_clock import MediaClock
from avatar_chat.adapters.outbound.media.recorded_source import ClipCatalog
from avatar_chat.adapters.outbound.media.silent_audio_track import SilentAudioTrack
from avatar_chat.adapters.outbound.media.switchable_track import SwitchableAvatarVideoTrack
from avatar_chat.application.dto import WebRtcAnswer, WebRtcOffer
from avatar_chat.domain.models import AvatarState


@dataclass(slots=True)
class PeerContext:
    peer: RTCPeerConnection
    video: SwitchableAvatarVideoTrack
    audio: SilentAudioTrack


class AiortcAvatarStreamAdapter:
    def __init__(
        self,
        catalog: ClipCatalog,
        ice_server_urls: tuple[str, ...] = (),
        ice_username: str | None = None,
        ice_credential: str | None = None,
        sync_debug_enabled: bool = False,
        sync_interval_seconds: float = 2.0,
        sync_pulse_duration_seconds: float = 0.1,
    ) -> None:
        self._catalog = catalog
        self._configuration = RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=list(ice_server_urls),
                    username=ice_username,
                    credential=ice_credential,
                )
            ]
            if ice_server_urls
            else []
        )
        self._peers: dict[str, PeerContext] = {}
        self._lock = asyncio.Lock()
        self._sync_debug_enabled = sync_debug_enabled
        self._sync_interval_seconds = sync_interval_seconds
        self._sync_pulse_duration_seconds = sync_pulse_duration_seconds

    async def start(
        self,
        session_id: str,
        character_id: str,
        room_name: str,
        publisher_token: str,
    ) -> None:
        # In-process aiortc sessions are started by SDP negotiation.
        del session_id, character_id, room_name, publisher_token

    async def negotiate(self, session_id: str, offer: WebRtcOffer) -> WebRtcAnswer:
        await self.close(session_id)
        peer = RTCPeerConnection(configuration=self._configuration)
        clock = MediaClock(
            sync_debug_enabled=self._sync_debug_enabled,
            sync_interval_seconds=self._sync_interval_seconds,
            sync_pulse_duration_seconds=self._sync_pulse_duration_seconds,
        )
        video = SwitchableAvatarVideoTrack(self._catalog, clock)
        audio = SilentAudioTrack(clock)
        await video.switch(AvatarState.IDLE)
        peer.addTrack(video)
        peer.addTrack(audio)

        context = PeerContext(peer=peer, video=video, audio=audio)
        async with self._lock:
            self._peers[session_id] = context

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            if peer.connectionState in {"failed", "closed"}:
                await self._discard_if_current(session_id, context)

        try:
            await peer.setRemoteDescription(
                RTCSessionDescription(sdp=offer.sdp, type=offer.type)
            )
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            local = peer.localDescription
            if local is None:
                raise RuntimeError("aiortc did not produce a local description")
            return WebRtcAnswer(sdp=local.sdp, type=local.type)
        except BaseException:
            await self._discard_if_current(session_id, context)
            raise

    async def change_state(self, session_id: str, state: AvatarState) -> None:
        async with self._lock:
            context = self._peers.get(session_id)
        if context is None:
            raise RuntimeError(f"No WebRTC peer exists for session '{session_id}'")
        await context.video.switch(state)

    async def close(self, session_id: str) -> None:
        async with self._lock:
            context = self._peers.pop(session_id, None)
        if context is not None:
            context.video.stop()
            context.audio.stop()
            await context.peer.close()

    async def shutdown(self) -> None:
        async with self._lock:
            session_ids = tuple(self._peers)
        await asyncio.gather(*(self.close(session_id) for session_id in session_ids))

    async def _discard_if_current(
        self,
        session_id: str,
        expected: PeerContext,
    ) -> None:
        async with self._lock:
            current = self._peers.get(session_id)
            if current is not expected:
                return
            self._peers.pop(session_id, None)
        expected.video.stop()
        expected.audio.stop()
        if expected.peer.connectionState != "closed":
            await expected.peer.close()
