from pathlib import Path

import pytest

from scripts import devops


def test_load_env_ignores_comments_and_unquotes_values(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nPLAIN=value\nDOUBLE=\"two words\"\nSINGLE='secret'\n",
        encoding="utf-8",
    )

    assert devops.load_env(env_file) == {
        "PLAIN": "value",
        "DOUBLE": "two words",
        "SINGLE": "secret",
    }


def test_replace_env_value_updates_once_and_preserves_other_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\nTOKEN=old\nB=2\n", encoding="utf-8")

    devops.replace_env_value(env_file, "TOKEN", "new")
    devops.replace_env_value(env_file, "EXTRA", "value")

    assert env_file.read_text(encoding="utf-8") == ("A=1\nTOKEN=new\nB=2\nEXTRA=value\n")


def test_public_service_urls_come_from_env() -> None:
    env = {
        "APP_PUBLIC_URL": "http://10.0.0.10:18000/",
        "CONTROL_PUBLIC_URL": "http://10.0.0.10:8090/",
    }

    assert devops.app_public_url(env) == "http://10.0.0.10:18000"
    assert devops.control_public_url(env) == "http://10.0.0.10:8090"


def test_env_check_rejects_shared_control_tokens(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CONTROL_OPERATOR_TOKEN=same\nCONTROL_CORE_TOKEN=same\n", encoding="utf-8")
    env = devops.load_env(env_file)

    result = devops.command_env_check(env, env_file)

    assert result == 1
    assert "must be different" in capsys.readouterr().out


def test_env_check_rejects_docker_dns_for_remote_workers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "REMOTE_WORKERS_ENABLED=true",
                "CONTROL_PUBLIC_URL=http://personaforge-control:8090",
                "CONTROL_REDIS_ADVERTISED_URL=redis://redis:6379/0",
                "LIVEKIT_WORKER_URL=ws://livekit:7880",
                "CONTROL_HTTP_BIND_HOST=127.0.0.1",
                "REDIS_BIND_HOST=127.0.0.1",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    result = devops.command_env_check(devops.load_env(env_file), env_file)

    output = capsys.readouterr().out
    assert result == 1
    assert "CONTROL_REDIS_ADVERTISED_URL must be reachable" in output
    assert "CONTROL_HTTP_BIND_HOST must expose Control" in output


def test_env_check_accepts_remote_worker_advertised_endpoints(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "REMOTE_WORKERS_ENABLED=true",
                "CONTROL_PUBLIC_URL=http://10.0.0.10:8090",
                "CONTROL_REDIS_ADVERTISED_URL=redis://10.0.0.10:16379/0",
                "LIVEKIT_WORKER_URL=ws://10.0.0.10:17880",
                "CONTROL_HTTP_BIND_HOST=0.0.0.0",
                "REDIS_BIND_HOST=0.0.0.0",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    assert devops.command_env_check(devops.load_env(env_file), env_file) == 0


def test_port_state_handles_restricted_socket_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny_socket() -> None:
        raise PermissionError

    monkeypatch.setattr(devops.socket, "socket", deny_socket)

    assert devops.port_state(8000) == "probe unavailable"


def test_http_json_wraps_connection_reset_as_retryable_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reset_connection(*_args: object, **_kwargs: object) -> None:
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(devops.urllib.request, "urlopen", reset_connection)

    with pytest.raises(RuntimeError, match="Unable to connect"):
        devops.http_json("http://127.0.0.1:8090/health/live")


def test_control_only_wait_does_not_probe_avatar_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes: list[str] = []
    results = iter((False, True))

    def check(name: str, _url: str, failure_label: str = "ERROR") -> bool:
        probes.append(f"{failure_label}:{name}")
        return next(results)

    monkeypatch.setattr(devops, "check_http", check)
    monkeypatch.setattr(devops.time, "sleep", lambda _seconds: None)

    assert (
        devops.command_wait_ready({}, full=False, control_only=True, worker_only=False, timeout=5)
        == 0
    )
    assert probes == ["WAIT:Control Plane", "WAIT:Control Plane"]
