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
