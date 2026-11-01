"""Import boundaries hold independently of source-tree workspace resolution."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def test_shared_core_has_no_game_or_transport_imports():
    for path in (ROOT / "packages/toolkit_core/src/toolkit_core").glob("*.py"):
        assert not any(name.split(".")[0] in {"backend", "personal_lab", "mcp", "fastapi", "uvicorn"}
                       for name in imports(path)), path


def test_existing_domain_is_transport_independent():
    for path in (ROOT / "backend").rglob("*.py"):
        assert not any(name.split(".")[0] in {"personal_lab", "mcp", "fastapi", "uvicorn"}
                       for name in imports(path)), path


def test_pure_engine_does_not_depend_on_application_services():
    for path in (ROOT / "backend/engine").glob("*.py"):
        assert not any(name.startswith(("backend.app", "backend.agent")) for name in imports(path)), path
