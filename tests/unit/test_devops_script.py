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
