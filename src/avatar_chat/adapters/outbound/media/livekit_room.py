from datetime import timedelta

from livekit import api

from avatar_chat.application.dto import MediaAccess, MediaProvisioning


class LiveKitMediaRoomAdapter:
    """Issue least-privilege publisher/subscriber access for one avatar room."""

    def __init__(
        self,
        server_url: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._server_url = server_url
        self._api_key = api_key
        self._api_secret = api_secret

    async def provision(self, session_id: str) -> MediaProvisioning:
        room_name = f"avatar-{session_id}"
        publisher_token = self._token(
            identity=f"worker-{session_id}",
            room_name=room_name,
            can_publish=True,
            can_subscribe=False,
            ttl=timedelta(hours=1),
        )
        client_token = self._token(
            identity=f"client-{session_id}",
            room_name=room_name,
            can_publish=False,
            can_subscribe=True,
            ttl=timedelta(minutes=30),
        )
        return MediaProvisioning(
            client=MediaAccess(
                provider="livekit",
                server_url=self._server_url,
                access_token=client_token,
                room_name=room_name,
            ),
            publisher_token=publisher_token,
        )

    async def close(self, session_id: str) -> None:
        # A room disappears automatically after its last participant leaves.
        # Explicit room deletion can be added later through LiveKit RoomService.
        del session_id

    def _token(
        self,
        identity: str,
        room_name: str,
        can_publish: bool,
        can_subscribe: bool,
        ttl: timedelta,
    ) -> str:
        return (
            api.AccessToken(self._api_key, self._api_secret)
            .with_identity(identity)
            .with_ttl(ttl)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=can_publish,
                    can_subscribe=can_subscribe,
                )
            )
            .to_jwt()
        )
