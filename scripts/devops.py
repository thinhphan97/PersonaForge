"""Safe developer and Compose operations used by the project Makefile."""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEVELOPMENT_VALUES = {
    "CONTROL_OPERATOR_TOKEN": "development-operator-token-change-me",
    "CONTROL_CORE_TOKEN": "development-core-control-token-change-me",
    "CONTROL_ENROLLMENT_JWT_SECRET": "development-control-enrollment-secret-change-me",
    "JWT_SECRET": "development-only-jwt-secret-change-me-32-bytes",
    "AGENT_DELEGATION_SECRET": "development-agent-delegation-secret-change-me",
    "REDIS_ADMIN_PASSWORD": "development-redis-admin-change-me",
}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(env: dict[str, str], key: str, default: str = "") -> str:
    return os.getenv(key, env.get(key, default))


def control_public_url(env: dict[str, str]) -> str:
    configured = setting(env, "CONTROL_PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    return f"http://127.0.0.1:{setting(env, 'CONTROL_HTTP_PORT', '8090')}"


def app_public_url(env: dict[str, str]) -> str:
    configured = setting(env, "APP_PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    return f"http://127.0.0.1:{setting(env, 'APP_HTTP_PORT', '8000')}"


def http_json(url: str, token: str | None = None, payload: object | None = None) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        reason = getattr(exc, "reason", str(exc))
        raise RuntimeError(f"Unable to connect to {url}: {reason}") from exc
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response from {url}") from exc


def replace_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    replacement = f"{key}={value}"
    found = False
    result: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            result.append(replacement)
            found = True
        else:
            result.append(line)
    if not found:
        result.append(replacement)
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


def command_env_check(env: dict[str, str], env_file: Path) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    if not env_file.exists():
        errors.append(f"Missing {env_file}; copy it from .env.example")
    media = setting(env, "MEDIA_PROVIDER", "aiortc")
    agent = setting(env, "AGENT_RUNTIME_PROVIDER", "local")
    if media not in {"aiortc", "livekit"}:
        errors.append("MEDIA_PROVIDER must be either aiortc or livekit")
    if agent not in {"local", "grpc"}:
        errors.append("AGENT_RUNTIME_PROVIDER must be either local or grpc")
    if media == "livekit":
        for key in (
            "LIVEKIT_URL",
            "LIVEKIT_API_KEY",
            "LIVEKIT_API_SECRET",
            "CONTROL_PLANE_URL",
            "AVATAR_REGISTRATION_URL",
            "AVATAR_HOST_ID",
        ):
            if not setting(env, key):
                errors.append(f"LiveKit mode requires {key}")
    remote_workers = setting(env, "REMOTE_WORKERS_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if remote_workers:
        for key in (
            "CONTROL_PUBLIC_URL",
            "CONTROL_REDIS_ADVERTISED_URL",
            "LIVEKIT_WORKER_URL",
        ):
            if not setting(env, key):
                errors.append(f"Remote Worker mode requires {key}")
        for key in (
            "CONTROL_PUBLIC_URL",
            "CONTROL_REDIS_ADVERTISED_URL",
            "LIVEKIT_WORKER_URL",
        ):
            value = setting(env, key)
            if value and urlsplit(value).hostname in {
                "redis",
                "livekit",
                "personaforge-control",
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                errors.append(f"{key} must be reachable outside the Docker network")
        if setting(env, "CONTROL_HTTP_BIND_HOST", "127.0.0.1") in {"127.0.0.1", "::1"}:
            errors.append("CONTROL_HTTP_BIND_HOST must expose Control to remote workers")
        if setting(env, "REDIS_BIND_HOST", "127.0.0.1") in {"127.0.0.1", "::1"}:
            errors.append("REDIS_BIND_HOST must expose Redis to remote workers")
    operator = setting(env, "CONTROL_OPERATOR_TOKEN")
    core = setting(env, "CONTROL_CORE_TOKEN")
    if operator and operator == core:
        errors.append("CONTROL_OPERATOR_TOKEN and CONTROL_CORE_TOKEN must be different")
    for key, unsafe in DEVELOPMENT_VALUES.items():
        if setting(env, key) == unsafe:
            warnings.append(f"{key} still uses the development secret")
    try:
        max_gpu_count = int(setting(env, "AVATAR_WORKER_MAX_GPU_COUNT", "1"))
        max_slots = int(setting(env, "AVATAR_WORKER_MAX_SLOTS", "1"))
        if max_gpu_count < 1 or max_slots < 1:
            errors.append("AVATAR_WORKER_MAX_GPU_COUNT/MAX_SLOTS must be >= 1")
    except ValueError:
        errors.append("AVATAR_WORKER_MAX_GPU_COUNT/MAX_SLOTS must be integers")
    for warning in warnings:
        print(f"WARN  {warning}")
    for error in errors:
        print(f"ERROR {error}")
    if errors:
        return 1
    print(f"OK    {env_file}: media={media}, agent={agent}")
    return 0


def tool_version(command: list[str]) -> tuple[bool, str]:
    if shutil.which(command[0]) is None:
        return False, "not found"
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return completed.returncode == 0, output[0] if output else f"exit={completed.returncode}"


def port_state(port: int) -> str:
    sock: socket.socket | None = None
    try:
        sock = socket.socket()
        sock.bind(("127.0.0.1", port))
    except PermissionError:
        return "probe unavailable"
    except OSError:
        return "in use"
    finally:
        if sock is not None:
            sock.close()
    return "available"


def command_doctor(env: dict[str, str], env_file: Path) -> int:
    failed = False
    for label, command in (
        ("Python", [sys.executable, "--version"]),
        ("Docker", ["docker", "--version"]),
        ("Compose", ["docker", "compose", "version"]),
        ("FFmpeg", ["ffmpeg", "-version"]),
    ):
        ok, version = tool_version(command)
        print(f"{'OK' if ok else 'ERROR':5} {label}: {version}")
        failed |= not ok
    docker_ok, docker_info = tool_version(["docker", "info", "--format", "{{.ServerVersion}}"])
    print(f"{'OK' if docker_ok else 'ERROR':5} Docker daemon: {docker_info}")
    failed |= not docker_ok
    gpu_ok, gpu_info = tool_version(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    print(f"{'OK' if gpu_ok else 'WARN':5} NVIDIA/NVML: {gpu_info}")
    for key, default in (
        ("APP_HTTP_PORT", "8000"),
        ("CONTROL_HTTP_PORT", "8090"),
        ("POSTGRES_PORT", "5432"),
        ("LIVEKIT_HTTP_PORT", "7880"),
    ):
        raw = setting(env, key, default)
        try:
            state = port_state(int(raw))
            print(f"INFO  {key}={raw}: {state}")
        except ValueError:
            print(f"ERROR {key} is not a valid port: {raw}")
            failed = True
    failed |= command_env_check(env, env_file) != 0
    return int(failed)


def compose_ps(env_file: Path) -> list[dict[str, Any]]:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "--profile",
            "livekit",
            "--profile",
            "agent",
            "ps",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "docker compose ps failed")
    raw = completed.stdout.strip()
    if not raw:
        return []
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, list) else [parsed]


def get_inventory(env: dict[str, str]) -> list[dict[str, Any]]:
    token = setting(env, "CONTROL_OPERATOR_TOKEN")
    data = http_json(f"{control_public_url(env)}/internal/workers", token=token)
    if not isinstance(data, list):
        raise RuntimeError("Control Plane returned an invalid worker inventory")
    return data


def check_http(name: str, url: str, failure_label: str = "ERROR") -> bool:
    try:
        payload = http_json(url)
        print(f"OK    {name}: {payload}")
        return True
    except RuntimeError as exc:
        print(f"{failure_label:5} {name}: {exc}")
        return False


def command_health(env: dict[str, str], env_file: Path, full: bool) -> int:
    ok = check_http("Avatar Chat", f"{app_public_url(env)}/api/health")
    try:
        services = compose_ps(env_file)
        observed_services: set[str] = set()
        for item in services:
            name = item.get("Service", item.get("Name", "unknown"))
            observed_services.add(name)
            state = item.get("State", "unknown")
            health = item.get("Health", "")
            good = state == "running" and health not in {"unhealthy", "starting"}
            print(f"{'OK' if good else 'ERROR':5} {name}: state={state} health={health or '-'}")
            ok &= good
        expected_services = {"postgres", "redis", "avatar-chat"}
        if full:
            expected_services.update({"livekit", "personaforge-control", "avatar-worker"})
            if setting(env, "AGENT_RUNTIME_PROVIDER", "local") == "grpc":
                expected_services.add("personaforge-agent")
        for missing in sorted(expected_services - observed_services):
            print(f"ERROR {missing}: service is not running")
            ok = False
    except RuntimeError as exc:
        print(f"ERROR Compose: {exc}")
        ok = False
    if full:
        ok &= check_http(
            "Control Plane",
            f"{control_public_url(env)}/health/live",
        )
        try:
            inventory = get_inventory(env)
            ready = [
                item
                for item in inventory
                if item.get("status") == "ready" and item.get("heartbeat")
            ]
            worker_summary = f"{len(ready)} ready / {len(inventory)} registered"
            print(f"{'OK' if ready else 'ERROR':5} Worker: {worker_summary}")
            ok &= bool(ready)
        except RuntimeError as exc:
            print(f"ERROR Worker inventory: {exc}")
            ok = False
    return 0 if ok else 1


def command_wait_ready(
    env: dict[str, str],
    full: bool,
    control_only: bool,
    worker_only: bool,
    timeout: int,
) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        control_ok = True
        if not worker_only:
            control_ok = check_http(
                "Control Plane",
                f"{control_public_url(env)}/health/live",
                failure_label="WAIT",
            )
        if control_only:
            if control_ok:
                return 0
            time.sleep(2)
            continue
        app_ok = (
            check_http(
                "Avatar Chat",
                f"{app_public_url(env)}/api/health",
                failure_label="WAIT",
            )
            if not worker_only
            else True
        )
        worker_ok = not (full or worker_only)
        if full or worker_only:
            try:
                worker_ok = any(
                    item.get("status") == "ready" and item.get("heartbeat")
                    for item in get_inventory(env)
                )
            except RuntimeError:
                worker_ok = False
        if control_ok and app_ok and worker_ok:
            print("OK    worker ready" if worker_only else "OK    pipeline ready")
            return 0
        time.sleep(2)
    print(f"ERROR pipeline was not ready after {timeout}s")
    return 1


def command_enroll(env: dict[str, str], env_file: Path) -> int:
    token = setting(env, "CONTROL_OPERATOR_TOKEN")
    if not token:
        print("ERROR CONTROL_OPERATOR_TOKEN is required", file=sys.stderr)
        return 1
    payload = {
        "worker_type": "persona-render",
        "cluster_id": setting(env, "AVATAR_CLUSTER_ID", "development"),
        "host_id": setting(env, "AVATAR_HOST_ID", "demo-host"),
        "max_gpu_count": int(setting(env, "AVATAR_WORKER_MAX_GPU_COUNT", "1")),
        "ttl_seconds": int(setting(env, "AVATAR_WORKER_ENROLLMENT_TTL_SECONDS", "600")),
    }
    try:
        response = http_json(
            f"{control_public_url(env)}/internal/worker-enrollment-tokens",
            token=token,
            payload=payload,
        )
        bootstrap = response["bootstrap_token"]
    except (RuntimeError, KeyError, TypeError, ValueError) as exc:
        print(f"ERROR failed to issue the enrollment token: {exc}", file=sys.stderr)
        return 1
    replace_env_value(env_file, "AVATAR_WORKER_BOOTSTRAP_TOKEN", bootstrap)
    print(
        f"OK    updated the one-time token in {env_file} for host={payload['host_id']} "
        f"max_gpu_count={payload['max_gpu_count']}; the raw token was not logged"
    )
    return 0


def fmt(value: Any, suffix: str = "") -> str:
    return "-" if value is None else f"{value}{suffix}"


def command_worker_status(env: dict[str, str], capacity: bool, children: bool) -> int:
    try:
        inventory = get_inventory(env)
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    if not inventory:
        print(
            "WARN  no registered workers found; inspect `make logs-worker`. "
            "If re-registration returns 401, run `make recover-worker-gpu`."
        )
        return 1
    for item in inventory:
        heartbeat = item.get("heartbeat") or {}
        caps = item.get("capabilities") or {}
        source = heartbeat.get("hardware_source", "-")
        print(
            f"worker={item.get('worker_id')} status={item.get('status')} "
            f"active={heartbeat.get('active_slots', 0)} queue={heartbeat.get('queue_depth', 0)} "
            f"fps={fmt(heartbeat.get('observed_fps'))} source={source}"
        )
        if capacity:
            gpu_caps = {gpu.get("gpu_uuid"): gpu for gpu in caps.get("gpus", [])}
            used_slots_by_gpu: dict[str, int] = {}
            for child in heartbeat.get("child_resources", []):
                assigned_gpu = child.get("assigned_gpu_uuid") or child.get("gpu_uuid")
                if assigned_gpu:
                    used_slots_by_gpu[str(assigned_gpu)] = (
                        used_slots_by_gpu.get(str(assigned_gpu), 0) + 1
                    )
            for gpu in heartbeat.get("gpu_resources", []):
                gpu_uuid = gpu.get("gpu_uuid")
                cap = gpu_caps.get(gpu_uuid, {})
                maximum_slots = cap.get("max_slots")
                used_slots = used_slots_by_gpu.get(str(gpu_uuid), 0)
                free_slots = (
                    max(0, int(maximum_slots) - used_slots) if maximum_slots is not None else None
                )
                used_vram = fmt(gpu.get("used_vram_mb"), "MiB")
                total_vram = fmt(gpu.get("total_vram_mb"), "MiB")
                free_vram = fmt(gpu.get("free_vram_mb"), "MiB")
                utilization = fmt(gpu.get("gpu_utilization"), "%")
                print(
                    f"  gpu={gpu_uuid} slots={fmt(maximum_slots)} "
                    f"used_slots={used_slots} free_slots={fmt(free_slots)} "
                    f"vram={used_vram}/{total_vram} free={free_vram} util={utilization} "
                    f"temp={fmt(gpu.get('temperature_c'), 'C')} healthy={gpu.get('healthy', True)}"
                )
        if children:
            rows = heartbeat.get("child_resources", [])
            if not rows:
                print("  children: none")
            for child in rows:
                session_id = child.get("session_id")
                gpu_uuid = child.get("gpu_uuid") or child.get("assigned_gpu_uuid")
                cpu = fmt(child.get("cpu_utilization"), "%")
                gpu_memory_mb = child.get("gpu_memory_used_mb")
                if gpu_memory_mb is None:
                    gpu_memory_mb = bytes_to_mib(child.get("gpu_memory_bytes"))
                gpu_utilization = child.get("gpu_utilization")
                if gpu_utilization is None:
                    gpu_utilization = child.get("gpu_sm_utilization")
                ram_mb = child.get("ram_used_mb")
                if ram_mb is None:
                    ram_bytes = child.get("host_rss_bytes")
                    if ram_bytes is None:
                        ram_bytes = child.get("ram_used_bytes")
                    ram_mb = bytes_to_mib(ram_bytes)
                print(
                    f"  session={session_id} pid={child.get('pid')} gpu={gpu_uuid} "
                    f"vram={fmt(gpu_memory_mb, 'MiB')} "
                    f"gpu_util={fmt(gpu_utilization, '%')} "
                    f"cpu={cpu} ram={fmt(ram_mb, 'MiB')} "
                    f"identity_ok={child.get('process_identity_matches')}"
                )
    return 0


def bytes_to_mib(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value) / (1024 * 1024), 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("env-check")
    sub.add_parser("doctor")
    value = sub.add_parser("value")
    value.add_argument("key")
    value.add_argument("default", nargs="?", default="")
    health = sub.add_parser("health")
    health.add_argument("--full", action="store_true")
    wait = sub.add_parser("wait-ready")
    wait.add_argument("--full", action="store_true")
    wait.add_argument("--control-only", action="store_true")
    wait.add_argument("--worker-only", action="store_true")
    wait.add_argument("--timeout", type=int, default=120)
    sub.add_parser("enroll-worker")
    status = sub.add_parser("worker-status")
    status.add_argument("--capacity", action="store_true")
    status.add_argument("--children", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env = load_env(args.env_file)
    if args.command == "env-check":
        return command_env_check(env, args.env_file)
    if args.command == "doctor":
        return command_doctor(env, args.env_file)
    if args.command == "value":
        print(setting(env, args.key, args.default))
        return 0
    if args.command == "health":
        return command_health(env, args.env_file, args.full)
    if args.command == "wait-ready":
        return command_wait_ready(
            env,
            args.full,
            args.control_only,
            args.worker_only,
            args.timeout,
        )
    if args.command == "enroll-worker":
        return command_enroll(env, args.env_file)
    if args.command == "worker-status":
        return command_worker_status(env, args.capacity, args.children)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
