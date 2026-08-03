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
        api_url: str | None = None,
    ) -> None:
        self._server_url = server_url
        self._api_url = api_url or server_url.replace("ws://", "http://", 1).replace(
            "wss://", "https://", 1
        )
        self._api_key = api_key
        self._api_secret = api_secret

    async def provision(
        self,
        session_id: str,
        viewer_user_id: str,
    ) -> MediaProvisioning:
        room_name = f"avatar-{session_id}"
        publisher_token = self._token(
            identity=f"worker-{session_id}",
            room_name=room_name,
            can_publish=True,
            can_subscribe=False,
            ttl=timedelta(hours=1),
        )
        return MediaProvisioning(
            client=await self.issue_access(session_id, viewer_user_id),
            publisher_token=publisher_token,
        )

    async def issue_access(self, session_id: str, user_id: str) -> MediaAccess:
        return self._viewer_access(session_id, f"user-{user_id}")

    async def issue_public_access(
        self,
        session_id: str,
        viewer_id: str,
    ) -> MediaAccess:
        return self._viewer_access(session_id, f"viewer-{viewer_id}")

    def _viewer_access(self, session_id: str, identity: str) -> MediaAccess:
        room_name = f"avatar-{session_id}"
        return MediaAccess(
            provider="livekit",
            server_url=self._server_url,
            access_token=self._token(
                identity=identity,
                room_name=room_name,
                can_publish=False,
                can_subscribe=True,
                ttl=timedelta(minutes=15),
            ),
            room_name=room_name,
        )

    async def revoke_participant(self, session_id: str, user_id: str) -> None:
        client = api.LiveKitAPI(
            self._api_url,
            self._api_key,
            self._api_secret,
        )
        try:
            await client.room.remove_participant(
                api.RoomParticipantIdentity(
                    room=f"avatar-{session_id}",
                    identity=f"user-{user_id}",
                )
            )
        finally:
            await client.aclose()

    async def revoke_participants_except(
        self,
        session_id: str,
        allowed_user_ids: tuple[str, ...],
    ) -> None:
        room_name = f"avatar-{session_id}"
        allowed_identities = {f"user-{user_id}" for user_id in allowed_user_ids}
        client = api.LiveKitAPI(
            self._api_url,
            self._api_key,
            self._api_secret,
        )
        try:
            response = await client.room.list_participants(
                api.ListParticipantsRequest(room=room_name)
            )
            for participant in response.participants:
                if (
                    not participant.identity.startswith("worker-")
                    and participant.identity not in allowed_identities
                ):
                    await client.room.remove_participant(
                        api.RoomParticipantIdentity(
                            room=room_name,
                            identity=participant.identity,
                        )
                    )
        finally:
            await client.aclose()

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
