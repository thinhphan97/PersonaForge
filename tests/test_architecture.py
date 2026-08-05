import ast
from pathlib import Path

FORBIDDEN_ROOTS = {"aiortc", "av", "fastapi", "httpx", "pydantic", "sqlalchemy"}


def test_core_does_not_import_frameworks_or_infrastructure() -> None:
    core = Path("src/avatar_chat")
    files = [
        *core.joinpath("domain").rglob("*.py"),
        *core.joinpath("application").rglob("*.py"),
    ]
    violations: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".", 1)[0]
                if root in FORBIDDEN_ROOTS or name.startswith("avatar_chat.adapters"):
                    violations.append(f"{path}: {name}")

    assert not violations, "Core dependency violations:\n" + "\n".join(violations)


def test_control_and_worker_application_cores_do_not_import_infrastructure() -> None:
    roots = (
        Path("src/personaforge_control/domain"),
        Path("src/personaforge_control/application"),
        Path("src/avatar_worker/domain"),
        Path("src/avatar_worker/application"),
    )
    forbidden = FORBIDDEN_ROOTS | {"redis", "livekit", "httpx"}
    violations: list[str] = []
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".", 1)[0] in forbidden or ".adapters" in name:
                        violations.append(f"{path}: {name}")

    assert not violations, "Worker/control dependency violations:\n" + "\n".join(violations)


def test_control_plane_has_no_production_memory_repository() -> None:
    control_source = Path("src/personaforge_control")

    assert not list(control_source.rglob("memory.py"))
    assert not any(
        "InMemory" in path.read_text()
        for path in control_source.rglob("*.py")
    )
