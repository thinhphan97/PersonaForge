import httpx
import pytest

from avatar_chat.adapters.inbound.fastapi_app import create_app


@pytest.mark.asyncio
async def test_auth_ownership_sharing_and_frontend() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://test") as owner,
            httpx.AsyncClient(transport=transport, base_url="http://test") as guest,
        ):
            health = await owner.get("/api/health")
            owner_auth = await owner.post(
                "/api/auth/register",
                json={
                    "email": "owner@example.com",
                    "username": "owner",
                    "password": "owner-password-123",
                },
            )
            guest_auth = await guest.post(
                "/api/auth/register",
                json={
                    "email": "guest@example.com",
                    "username": "guest",
                    "password": "guest-password-123",
                },
            )
            owner_csrf = owner_auth.json()["csrf_token"]
            guest_csrf = guest_auth.json()["csrf_token"]
            owner_headers = {
                "Authorization": f"Bearer {owner_auth.json()['access_token']}",
                "X-CSRF-Token": owner_csrf,
            }
            guest_headers = {
                "Authorization": f"Bearer {guest_auth.json()['access_token']}",
                "X-CSRF-Token": guest_csrf,
            }

            characters = await owner.get("/api/characters", headers=owner_headers)
            created = await owner.post(
                "/api/sessions",
                headers=owner_headers,
                json={"character_id": "default", "visibility": "private"},
            )
            session_id = created.json()["session_id"]
            private_public_view = await guest.get(f"/api/public/sessions/{session_id}")
            private_public_messages = await guest.get(
                f"/api/public/sessions/{session_id}/messages"
            )

            denied_history = await guest.get(
                f"/api/sessions/{session_id}/messages", headers=guest_headers
            )
            shared = await owner.post(
                f"/api/sessions/{session_id}/shares",
                headers=owner_headers,
                json={"username": "guest", "permission": "viewer"},
            )
            allowed_history = await guest.get(
                f"/api/sessions/{session_id}/messages", headers=guest_headers
            )
            upgraded_share = await owner.post(
                f"/api/sessions/{session_id}/shares",
                headers=owner_headers,
                json={"username": "guest", "permission": "interactor"},
            )
            shared_sessions = await guest.get(
                "/api/sessions/shared-with-me",
                headers=guest_headers,
            )
            denied_close = await guest.delete(
                f"/api/sessions/{session_id}",
                headers=guest_headers,
            )
            made_public = await owner.patch(
                f"/api/sessions/{session_id}/visibility",
                headers=owner_headers,
                json={"visibility": "public"},
            )
            public_sessions = await guest.get("/api/sessions/public", headers=guest_headers)
            anonymous_public_view = await guest.get(
                f"/api/public/sessions/{session_id}"
            )
            anonymous_public_messages = await guest.get(
                f"/api/public/sessions/{session_id}/messages"
            )
            public_watch = await guest.get(f"/watch/{session_id}")
            logout = await guest.post("/api/auth/logout", headers=guest_headers)
            logged_out_me = await guest.get("/api/auth/me", headers=guest_headers)
            frontend = await owner.get("/")

    assert health.json() == {"status": "ok"}
    assert owner_auth.status_code == guest_auth.status_code == 201
    assert {item["character_id"] for item in characters.json()} >= {
        "default",
        "violet",
        "coral",
    }
    assert created.status_code == 201
    assert created.json()["public_url"] is None
    assert private_public_view.status_code == 404
    assert private_public_messages.status_code == 404
    assert denied_history.status_code == 403
    assert shared.json()["permission"] == "viewer"
    assert allowed_history.status_code == 200
    assert upgraded_share.json()["permission"] == "interactor"
    assert shared_sessions.json()[0]["can_chat"] is True
    assert denied_close.status_code == 403
    assert made_public.json()["visibility"] == "public"
    assert made_public.json()["public_url"] == f"/watch/{session_id}"
    assert anonymous_public_view.status_code == 200
    assert anonymous_public_messages.status_code == 200
    assert anonymous_public_messages.json() == []
    assert public_watch.status_code == 200
    assert "không cần đăng nhập" in public_watch.text
    assert 'href="/?login=1"' in public_watch.text
    assert any(item["session_id"] == session_id for item in public_sessions.json())
    assert next(
        item for item in public_sessions.json() if item["session_id"] == session_id
    )["can_chat"] is True
    assert logout.status_code == 204
    assert logged_out_me.status_code == 401
    assert "Avatar WebRTC Chat" in frontend.text


@pytest.mark.asyncio
async def test_mutation_requires_csrf() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            auth = await client.post(
                "/api/auth/register",
                json={
                    "email": "csrf@example.com",
                    "username": "csrf-user",
                    "password": "csrf-password-123",
                },
            )
            response = await client.post(
                "/api/sessions",
                headers={
                    "Authorization": f"Bearer {auth.json()['access_token']}",
                },
                json={"character_id": "default"},
            )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rotates_cookie_and_invalidates_previous_access_token() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            registered = await client.post(
                "/api/auth/register",
                json={
                    "email": "refresh@example.com",
                    "username": "refresh-user",
                    "password": "refresh-password-123",
                },
            )
            original = registered.json()
            refreshed = await client.post(
                "/api/auth/refresh",
                headers={"X-CSRF-Token": original["csrf_token"]},
            )
            old_access = await client.get(
                "/api/auth/me",
                headers={"Authorization": f"Bearer {original['access_token']}"},
            )
            new_access = await client.get(
                "/api/auth/me",
                headers={
                    "Authorization": f"Bearer {refreshed.json()['access_token']}"
                },
            )

    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != original["access_token"]
    assert old_access.status_code == 401
    assert new_access.status_code == 200
