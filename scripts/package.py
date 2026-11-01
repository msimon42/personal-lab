"""Build a headless source bundle, excluding runtime state and local tooling."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"__pycache__", "build", "dist", ".venv", ".git", "node_modules", ".workspace", ".cache", ".tools"}


def build(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in ["backend", "apps", "packages", "scripts", "profiles", "config", "schemas", "skills", "docs", "specs", "tests"]:
        for path in (ROOT / name).rglob("*"):
            if any(part in EXCLUDED or part.endswith(".egg-info") for part in path.relative_to(ROOT).parts):
                continue
            if path.is_symlink():
                raise ValueError("Release source cannot contain links")
            if path.is_file():
                paths.append(path)
    paths += [ROOT / name for name in ["AGENTS.md", "README.md", "pyproject.toml", "setup.py", "MANIFEST.in", "uv.lock", ".python-version", ".gitignore"]]
    manifest = []
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(set(paths)):
            if path.is_symlink():
                raise ValueError("Release source cannot contain links")
            raw = path.read_bytes()
            relative = path.relative_to(ROOT).as_posix()
            bundle.writestr(relative, raw)
            manifest.append({"path": relative, "sha256": hashlib.sha256(raw).hexdigest()})
        bundle.writestr("release-manifest.json", json.dumps({"schema_version": 2, "kind": "headless_workspace_source", "files": manifest}, indent=2))
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path, nargs="?", default=ROOT / "dist/personal-lab.zip")
    print(build(parser.parse_args().destination))
