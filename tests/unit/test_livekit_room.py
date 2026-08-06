import jwt
import pytest

from personaforge_chat.adapters.outbound.media.livekit_room import LiveKitMediaRoomAdapter


@pytest.mark.asyncio
async def test_livekit_room_issues_separate_least_privilege_tokens() -> None:
    secret = "test-secret-that-is-at-least-32-bytes"
    adapter = LiveKitMediaRoomAdapter("ws://media.test", "test-key", secret)

    provisioning = await adapter.provision("session-1", "user-1")
    public_access = await adapter.issue_public_access("session-1", "viewer-1")

    client_claims = jwt.decode(
        provisioning.client.access_token,
        secret,
        algorithms=["HS256"],
        issuer="test-key",
    )
    publisher_claims = jwt.decode(
        provisioning.publisher_token,
        secret,
        algorithms=["HS256"],
        issuer="test-key",
    )
    public_claims = jwt.decode(
        public_access.access_token,
        secret,
        algorithms=["HS256"],
        issuer="test-key",
    )

    assert provisioning.client.room_name == "avatar-session-1"
    assert provisioning.client.server_url == "ws://media.test"
    assert client_claims["video"]["room"] == "avatar-session-1"
    assert client_claims["video"]["canPublish"] is False
    assert client_claims["video"]["canSubscribe"] is True
    assert client_claims["sub"] == "user-user-1"
    assert publisher_claims["video"]["canPublish"] is True
    assert publisher_claims["video"]["canSubscribe"] is False
    assert public_claims["sub"] == "viewer-viewer-1"
    assert public_claims["video"]["canPublish"] is False
    assert public_claims["video"]["canSubscribe"] is True
