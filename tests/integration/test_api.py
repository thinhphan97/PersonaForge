import httpx
import pytest

from avatar_chat.adapters.inbound.fastapi_app import create_app


@pytest.mark.asyncio
async def test_health_session_and_frontend() -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/api/health")
            session = await client.post(
                "/api/sessions",
                json={"character_id": "default"},
            )
            frontend = await client.get("/")

    assert health.json() == {"status": "ok"}
    assert session.status_code == 201
    assert session.json()["session_id"]
    assert "Avatar WebRTC Chat" in frontend.text

