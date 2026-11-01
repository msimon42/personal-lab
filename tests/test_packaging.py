"""The headless source bundle contains workspace closure and no private state."""
import hashlib
import json
from pathlib import Path
import zipfile

from scripts import package


def test_release_archive_is_headless_and_preserves_dependency_closure(tmp_path, monkeypatch):
    source = tmp_path / "source"
    required = {"AGENTS.md", "README.md", "pyproject.toml", "setup.py", "MANIFEST.in", "uv.lock", ".python-version", ".gitignore",
        "backend/app/service.py", "packages/toolkit_core/pyproject.toml", "packages/toolkit_core/src/toolkit_core/storage.py",
        "apps/toolkit/pyproject.toml", "apps/toolkit/src/personal_lab/mcp_server.py", "apps/toolkit/src/personal_lab/cli.py",
        "schemas/ck3/_manifest.json", "skills/ck3-mod-agent/SKILL.md", "config/profiles.example.toml"}
    private = {"apps/toolkit/.venv/private", "packages/toolkit_core/build/private", ".workspace/private", "backend/__pycache__/private.pyc"}
    for name in required | private:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    monkeypatch.setattr(package, "ROOT", source)
    with zipfile.ZipFile(package.build(tmp_path / "source.zip")) as bundle:
        names = set(bundle.namelist())
        assert required <= names and not names & private
        assert not any(part in package.EXCLUDED for name in names for part in Path(name).parts)
        manifest = json.loads(bundle.read("release-manifest.json"))
        assert manifest["kind"] == "headless_workspace_source"
        assert {row["path"] for row in manifest["files"]} == names - {"release-manifest.json"}
        assert all(hashlib.sha256(bundle.read(row["path"])).hexdigest() == row["sha256"] for row in manifest["files"])
