import json
from pathlib import Path

import httpx
import pytest

from personaforge_worker.identity import WorkerIdentityClient
from personaforge_worker.settings import WorkerSettings


@pytest.mark.asyncio
async def test_invalid_management_token_falls_back_to_fresh_bootstrap_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / "identity"
    state_dir.mkdir()
    (state_dir / "identity.json").write_text(
        json.dumps(
            {
                "worker_id": "worker-stable",
                "management_token": "stale-management-token",
                "management_token_family_id": "old-family",
                "identity_generation": 3,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AVATAR_REGISTRATION_URL", "http://control:8090")
    monkeypatch.setenv("AVATAR_WORKER_BOOTSTRAP_TOKEN", "fresh-bootstrap-token")
    monkeypatch.setenv("AVATAR_WORKER_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AVATAR_HOST_ID", "host-a")
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("/reregister"):
            return httpx.Response(401, json={"detail": "stale token"})
        payload = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer fresh-bootstrap-token"
        assert payload["proposed_worker_id"] == "worker-stable"
        assert payload["capabilities"]["supported_runtimes"] == [
            "recorded",
            "scripted",
            "vieneu-v3-turbo",
        ]
        return httpx.Response(
            200,
            json={
                "worker_id": "worker-stable",
                "boot_id": "new-boot",
                "owner_token": "new-owner",
                "management_token": "new-management-token",
                "management_family_id": "new-family",
                "identity_generation": 4,
                "credential": {
                    "redis_url": "redis://redis:6379/0",
                    "username": "worker-user",
                    "password": "worker-password",
                },
            },
        )

    client = WorkerIdentityClient(WorkerSettings.from_env())
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        identity = await client.enroll()
    finally:
        await client.close()

    assert requests == ["/internal/workers/reregister", "/internal/workers/register"]
    assert identity.worker_id == "worker-stable"
    persisted = json.loads((state_dir / "identity.json").read_text(encoding="utf-8"))
    assert persisted["management_token"] == "new-management-token"
