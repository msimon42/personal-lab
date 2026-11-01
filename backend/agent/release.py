"""One fingerprint for the complete local engine, workflow and CK3 resources."""
from __future__ import annotations

import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import sys

from backend.engine.safety import read_source_bytes, reject_link_components, validate_relative_path

ASSISTANT_VERSION = "4.0.0"
RECEIPT = ".mod-engine-release.json"
OWNER_MARKER = b"CK3 mod assistant\n"
MAX_RESOURCE_BYTES = 4 * 1024 * 1024


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def skill_source():
    checkout = Path(__file__).resolve().parents[2] / "skills/ck3-mod-agent"
    packaged = Path(sys.prefix) / "share/mod-engine/skills/ck3-mod-agent"
    for root in (checkout, packaged):
        if (root / "SKILL.md").is_file():
            return reject_link_components(root)
    raise ValueError("The packaged CK3 workflow is missing. Reinstall the assistant.")


def workflow_files(root=None):
    """Only deliberately shipped instructions, metadata and reference files."""
    root = skill_source() if root is None else reject_link_components(Path(root))
    names = ["SKILL.md", "agents/openai.yaml"]
    references = root / "references"
    if references.exists():
        reject_link_components(references)
        for path in sorted(references.rglob("*")):
            reject_link_components(path)
            if path.is_file():
                names.append(path.relative_to(root).as_posix())
    result = {}
    for name in names:
        validate_relative_path(name)
        path = root / name
        if path.stat().st_nlink != 1:
            raise ValueError("Packaged workflow files must not be shared hardlinks.")
        result[name] = read_source_bytes(root, name, max_bytes=MAX_RESOURCE_BYTES, native_paths=True)
    return result


def _package_files():
    root = resources.files("backend")
    implementation, knowledge = {}, {}

    def visit(directory, prefix):
        for entry in sorted(directory.iterdir(), key=lambda item: item.name):
            name = prefix + "/" + entry.name
            if entry.is_dir() and entry.name != "__pycache__":
                visit(entry, name)
            elif entry.is_file() and entry.name.endswith(".py"):
                implementation[name] = _digest(entry.read_bytes())

    visit(root, "backend")
    # The transport and safety core participate in the same write authority.
    # Fingerprints must match in installed wheels and editable checkouts.
    for package in ("toolkit_core", "personal_lab"):
        try:
            visit(resources.files(package), package)
        except ModuleNotFoundError:
            if package == "personal_lab":
                # Direct domain-library use does not require an MCP host. A
                # later host installation changes the fingerprint and requires
                # a matching reload before granting write authority.
                continue
            raise ValueError("The complete Personal Lab installation is missing: " + package) from None
    data = root.joinpath("agent", "data")
    if data.is_dir():
        for entry in sorted(data.iterdir(), key=lambda item: item.name):
            if entry.is_file() and entry.name.endswith(".json"):
                knowledge["data/" + entry.name] = _digest(entry.read_bytes())
    return implementation, knowledge


def _schema_files():
    # The loader resolves installed resources and editable source to the same
    # authoritative bytes. Names must not depend on their filesystem location.
    from backend.engine.ck3 import SCHEMA_ROOT
    return {"schemas/ck3/" + entry.name: _digest(entry.read_bytes())
            for entry in sorted(SCHEMA_ROOT.iterdir(), key=lambda item: item.name)
            if entry.is_file() and entry.name.endswith(".json")}


def release_info(*, workflow_root=None):
    implementation, knowledge = _package_files()
    result = {"version": ASSISTANT_VERSION,
        "workflow_files": {name: _digest(raw) for name, raw in workflow_files(workflow_root).items()},
        "knowledge_files": knowledge, "implementation_files": implementation,
        "schema_files": _schema_files()}
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode()
    return {**result, "fingerprint": _digest(raw)}


def installation_status(skill_path=None, expected_fingerprint=None):
    """Read-only diagnostics; a development checkout need not be globally installed."""
    selected = skill_path or os.environ.get("CK3_MOD_AGENT_SKILL_PATH")
    expected = expected_fingerprint or os.environ.get("CK3_MOD_AGENT_EXPECTED_RELEASE")
    current = release_info()
    result = {"version": current["version"], "fingerprint": current["fingerprint"],
              "status": "development" if not selected and not expected else "ready", "problems": []}
    if expected and expected != current["fingerprint"]:
        result["problems"].append("The running assistant differs from the registered installation.")
    if selected:
        try:
            root = reject_link_components(Path(selected).absolute())
            receipt = json.loads(read_source_bytes(root, RECEIPT, max_bytes=65536, native_paths=True))
            if (receipt.get("version") != current["version"] or
                    receipt.get("fingerprint") != current["fingerprint"]):
                result["problems"].append("The installed instructions and engine are from different releases.")
            for name, checksum in current["workflow_files"].items():
                raw = read_source_bytes(root, name, max_bytes=MAX_RESOURCE_BYTES, native_paths=True)
                if _digest(raw) != checksum:
                    result["problems"].append("An installed workflow file changed: " + name)
            if any((root.parent / name).exists() for name in (".ck3-mod-agent.install.json", ".personal-lab-install.json")):
                result["problems"].append("An assistant update needs recovery before changes can be prepared.")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["problems"].append("The installed workflow could not be verified: " + str(exc))
    if result["problems"]:
        result.update(status="mismatch", next_step="Reinstall the CK3 assistant, then reconnect its tools. Saved investigations are retained.")
    return result
