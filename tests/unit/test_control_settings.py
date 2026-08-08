import pytest

from personaforge_control.bootstrap.settings import ControlSettings


def test_advertised_redis_url_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REMOTE_WORKERS_ENABLED", "true")
    monkeypatch.setenv("CONTROL_REDIS_WORKER_URL", "redis://redis:6379/0")
    monkeypatch.setenv(
        "CONTROL_REDIS_ADVERTISED_URL",
        "redis://10.0.0.10:16379/0",
    )

    settings = ControlSettings.from_env()

    assert settings.redis_worker_url == "redis://10.0.0.10:16379/0"


def test_remote_worker_mode_rejects_docker_only_redis_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REMOTE_WORKERS_ENABLED", "true")
    monkeypatch.setenv("CONTROL_REDIS_ADVERTISED_URL", "redis://redis:6379/0")

    with pytest.raises(ValueError, match="reachable from remote workers"):
        ControlSettings.from_env()


def test_remote_worker_mode_rejects_mismatched_published_redis_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REMOTE_WORKERS_ENABLED", "true")
    monkeypatch.setenv("CONTROL_REDIS_ADVERTISED_URL", "redis://10.0.0.10:16379/0")
    monkeypatch.setenv("REDIS_PORT", "16380")

    with pytest.raises(ValueError, match="port must match"):
        ControlSettings.from_env()
