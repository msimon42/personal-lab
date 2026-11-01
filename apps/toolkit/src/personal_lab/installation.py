"""Recoverable owned Codex connection cutover to the unified local toolkit.

Configuration editing is administrative. No tool operation issues approval and
unrelated configuration, skill files and stricter policies are retained.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

from toolkit_core.safety import ManagedFS, ManagedLock, read_source_bytes, reject_link_components

NAME = "personal_lab"
LEGACY_NAME = "ck3_mod_agent"
JOURNAL = ".personal-lab-install.json"
MARKER = ".mod-engine-owned"
RECEIPT = ".mod-engine-release.json"
KNOWN_CK3_TOOLS = frozenset({
    "get_context", "list_missing_mods", "check_playset", "remember_progress", "search_knowledge", "read_knowledge",
    "search_sources", "read_source", "related_definitions", "get_definition_contributions", "investigate_change",
    "read_sources", "list_concerns", "get_concern_evidence", "record_concern_review", "get_investigation_report",
    "find_missing_mod_candidates", "inspect_mod_candidate", "set_version_preference", "find_playsets", "use_playset",
    "scan_playset", "get_progress", "cancel_work", "examine_changes", "list_changes", "inspect_change",
    "prepare_call_allies_choices", "ask_about_changes", "make_selected_files", "test_selected_changes",
    "enable_selected_changes", "undo_selected_changes", "resume_conversation",
})


def _hash(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _bytes(path: Path):
    path = reject_link_components(path.absolute())
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Owned settings must be an unshared regular file")
    return read_source_bytes(path.parent, path.name, max_bytes=4 * 1024 * 1024, native_paths=True)


def _renamed(name):
    return "ck3_" + name if name in KNOWN_CK3_TOOLS else name


def _configuration(before, connector, previous):
    import tomlkit
    source = (before or b"").decode()
    document = tomlkit.parse(source)
    if tomlkit.dumps(document) != source:
        raise ValueError("Settings cannot be edited without preserving their formatting")
    parsed = tomllib.loads(source)
    servers = parsed.get("mcp_servers", {})
    old, current = servers.get(LEGACY_NAME), servers.get(NAME)
    previous_name = previous.get("name") if previous else None
    if old is not None and current is not None:
        raise ValueError("Both old and unified connections exist; resolve ownership before installing")
    active = current if current is not None else old
    active_name = NAME if current is not None else LEGACY_NAME if old is not None else None
    if active is not None and (previous is None or previous_name != active_name or any(
            active.get(key) != previous.get("transport", {}).get(key) for key in ("command", "args"))):
        raise ValueError("The existing connection is not owned by this installation; settings were preserved")
    if active is None and previous is not None:
        raise ValueError("The owned connection was removed externally; settings were preserved")
    output_servers = document.setdefault("mcp_servers", tomlkit.table())
    if active_name:
        server = output_servers.pop(active_name)
    else:
        server = tomlkit.table()
    # Preserve existing server-level and per-tool restrictions when names change.
    translated = []
    if active_name == LEGACY_NAME:
        for field in ("enabled_tools", "disabled_tools"):
            if field in server:
                server[field] = [_renamed(str(name)) for name in server[field]]
        if "tools" in server:
            old_settings = server["tools"]
            settings = tomlkit.table()
            for name, value in old_settings.items():
                replacement = _renamed(name)
                if replacement in settings:
                    raise ValueError("Translated tool policies collide; settings were preserved")
                settings[replacement] = value
                if replacement != name:
                    translated.append(replacement)
            server["tools"] = settings
    transport = connector["transport"]
    for name in ("command", "args", "env"):
        server[name] = transport[name]
    for name in ("cwd", "env_vars"):
        if transport.get(name) is not None:
            server[name] = transport[name]
        elif name in server:
            del server[name]
    output_servers[NAME] = server
    after = tomlkit.dumps(document).encode()
    expected = copy.deepcopy(parsed)
    wanted = expected.setdefault("mcp_servers", {})
    base = wanted.pop(active_name) if active_name else {}
    if active_name == LEGACY_NAME:
        for field in ("enabled_tools", "disabled_tools"):
            if field in base:
                base[field] = [_renamed(name) for name in base[field]]
        if "tools" in base:
            base["tools"] = {_renamed(name): value for name, value in base["tools"].items()}
    for key in ("command", "args", "env", "cwd", "env_vars"):
        if transport.get(key) is not None:
            base[key] = transport[key]
        else:
            base.pop(key, None)
    wanted[NAME] = base
    if tomllib.loads(after.decode()) != expected:
        raise ValueError("The proposed update changes unrelated settings")
    return after, translated


def _write_config(path, before, after):
    if _bytes(path) != before:
        raise ValueError("Settings changed during installation; intervening edits were preserved")
    if after is None:
        if path.exists():
            path.unlink()
    else:
        ManagedFS(path.parent, max_bytes=4 * 1024 * 1024).write_bytes(path.name, after)


def _previous_owned(files):
    if RECEIPT not in files:
        return {"SKILL.md": None, "agents/openai.yaml": None}
    try:
        owned = json.loads(files[RECEIPT])["workflow_files"]
        if not isinstance(owned, dict):
            raise ValueError
        for name, checksum in owned.items():
            if not (name in {"SKILL.md", "agents/openai.yaml"} or name.startswith("references/")):
                raise ValueError
            if name not in files or _hash(files[name]) != checksum:
                raise ValueError
        return owned
    except (ValueError, TypeError, KeyError):
        raise ValueError("Installed instructions changed outside setup; their edits were preserved") from None


def _helpers():
    from toolkit_core.generations import generation_manifest, read_generation, remove_generation
    # Resources remain owned by the optional mod-engine distribution.
    from backend.agent.release import OWNER_MARKER, release_info, workflow_files
    return read_generation, generation_manifest, _previous_owned, remove_generation, OWNER_MARKER, release_info, workflow_files


def _decode(record, key):
    value = record[key]
    raw = base64.b64decode(value, validate=True) if value is not None else None
    if _hash(raw) != record[key + "_sha256"]:
        raise ValueError("The installation recovery settings changed")
    return raw


def recover_installation(skill_parent: Path) -> None:
    parent = reject_link_components(Path(skill_parent).absolute())
    journal = parent / JOURNAL
    if not journal.exists():
        return
    tree, manifest, _, remove_generation, *_ = _helpers()
    record = json.loads(read_source_bytes(parent, JOURNAL, max_bytes=8 * 1024 * 1024, native_paths=True))
    if record.get("format") != "personal-lab-install-1":
        raise ValueError("Unknown installation recovery format")
    config = reject_link_components(Path(record["config_path"]))
    target = parent / "ck3-mod-agent"
    for key in ("stage", "backup"):
        name = record[key]
        if not isinstance(name, str) or Path(name).name != name or not name.startswith(".personal-lab-"):
            raise ValueError("Unsafe installation recovery path")
    stage, backup = parent / record["stage"], parent / record["backup"]
    before, after = _decode(record, "config_before"), _decode(record, "config_after")
    current = _bytes(config)
    if current not in (before, after):
        raise ValueError("Settings changed after interrupted setup; recovery copies were retained")
    if record["phase"] == "complete":
        if current != after or manifest(tree(target)) != record["new_manifest"]:
            raise ValueError("The installed generation changed; recovery copies were retained")
    else:
        if current == after:
            _write_config(config, after, before)
        if backup.exists():
            if manifest(tree(backup)) != record["old_manifest"]:
                raise ValueError("The previous skill generation changed")
            if target.exists():
                remove_generation(target, record["new_manifest"])
            os.rename(backup, target)
        elif not record["had_previous"] and target.exists():
            remove_generation(target, record["new_manifest"])
        elif record["had_previous"] and manifest(tree(target)) != record["old_manifest"]:
            raise ValueError("The prior skill generation needs manual recovery")
    remove_generation(stage, record["new_manifest"])
    remove_generation(backup, record["old_manifest"])
    journal.unlink()


def install_connection(state_dir: Path, *, config_path=None, skill_destination=None,
                       executable=None, profile="personal", fault_at=None) -> dict:
    """Install an already prepared toolkit environment; never install dependencies."""
    from .migration import validate_state_ready
    root = reject_link_components(Path(state_dir).absolute())
    validate_state_ready(root)
    config = Path(config_path) if config_path else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    config = reject_link_components(config.absolute())
    target = Path(skill_destination) if skill_destination else config.parent / "skills/ck3-mod-agent"
    target = reject_link_components(target.absolute())
    if target.name != "ck3-mod-agent":
        raise ValueError("The owned CK3 workflow destination must be named ck3-mod-agent")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    python = Path(executable or sys.executable).absolute()
    reject_link_components(python.parent)
    if not python.is_file() or not python.resolve().is_file():
        raise ValueError("The configured Python executable is missing")
    tree, manifest, previous_owned, remove_generation, marker, release_info, workflow_files = _helpers()
    with ManagedLock(parent / ".personal-lab-install.lock"):
        if (parent / ".ck3-mod-agent.install.json").exists():
            raise ValueError("The legacy installation needs recovery before unified connection setup")
        recover_installation(parent)
        before = _bytes(config)
        old_tree = tree(target)
        old_files, directories, modes = old_tree
        if target.exists() and old_files.get(MARKER) != marker:
            raise ValueError("The existing skill is not owned by this installer")
        receipt = json.loads(old_files[RECEIPT]) if RECEIPT in old_files else None
        previous = receipt.get("connector") if receipt else None
        release = release_info()
        shipped = workflow_files()
        if {name: _hash(raw) for name, raw in shipped.items()} != release["workflow_files"]:
            raise ValueError("The packaged workflow changed during installation")
        configured = tomllib.loads((before or b"").decode()).get("mcp_servers", {})
        active = configured.get(NAME, configured.get(LEGACY_NAME, {}))
        environment = dict(active.get("env") or {})
        environment.update(CK3_MOD_AGENT_EXPECTED_RELEASE=release["fingerprint"], CK3_MOD_AGENT_SKILL_PATH=str(target))
        connector = {"name": NAME, "transport": {"command": str(python),
            "args": ["-m", "personal_lab", "mcp", "--profile", profile, "--state-dir", str(root)],
            "env": environment, "type": "stdio"}}
        for key in ("cwd", "env_vars"):
            if key in active:
                connector["transport"][key] = copy.deepcopy(active[key])
        after, translated = _configuration(before, connector, previous)
        owned_files = previous_owned(old_files)
        if any(name in old_files and name not in owned_files for name in shipped):
            raise ValueError("A packaged workflow file would overwrite unrelated installed content")
        files = {name: raw for name, raw in old_files.items() if name not in owned_files}
        files.update(shipped)
        published_receipt = {**release, "data_dir": str(root), "connector": connector, "interface": NAME}
        files.update({MARKER: marker, RECEIPT: (json.dumps(published_receipt, sort_keys=True, indent=2) + "\n").encode()})
        stage = Path(tempfile.mkdtemp(prefix=".personal-lab-stage-", dir=parent))
        backup = parent / stage.name.replace("-stage-", "-backup-", 1)
        fs = ManagedFS(parent, max_bytes=8 * 1024 * 1024)
        try:
            output = ManagedFS(stage, max_bytes=4 * 1024 * 1024)
            for directory in directories:
                output.resolve(directory).mkdir(parents=True, exist_ok=True)
            for name, raw in files.items():
                output.write_bytes(name, raw, replace=False)
            for name in old_files.keys() - owned_files.keys() - {MARKER, RECEIPT}:
                os.chmod(output.resolve(name), modes[name])
            for name in sorted(directories, key=lambda value: value.count("/"), reverse=True):
                os.chmod(output.resolve(name), modes[name])
            if "." in modes:
                os.chmod(stage, modes["."])
            record = {"format": "personal-lab-install-1", "phase": "prepared", "stage": stage.name,
                "backup": backup.name, "had_previous": target.exists(), "config_path": str(config),
                "config_before": base64.b64encode(before).decode() if before is not None else None,
                "config_after": base64.b64encode(after).decode(), "config_before_sha256": _hash(before),
                "config_after_sha256": _hash(after), "old_manifest": manifest(old_tree),
                "new_manifest": manifest(tree(stage))}
            fs.write_bytes(JOURNAL, json.dumps(record).encode(), replace=False)
            if manifest(tree(target)) != record["old_manifest"] or _bytes(config) != before:
                raise ValueError("The owned setup changed during installation")
            if fault_at == "before_publish":
                raise RuntimeError("Injected interruption before installation publication")
            if target.exists():
                os.rename(target, backup)
            os.rename(stage, target)
            if fault_at == "after_skill":
                raise RuntimeError("Injected interruption after workflow publication")
            _write_config(config, before, after)
            if fault_at == "after_config":
                raise RuntimeError("Injected interruption after settings publication")
            if _bytes(config) != after or manifest(tree(target)) != record["new_manifest"]:
                raise ValueError("The installed connection failed verification")
            fs.write_bytes(JOURNAL, json.dumps({**record, "phase": "complete"}).encode())
            recover_installation(parent)
        except Exception:
            if fs.resolve(JOURNAL).exists():
                recover_installation(parent)
            elif stage.exists():
                shutil.rmtree(stage)
            raise
    return {"installed": True, "server": NAME, "state_dir": str(root), "skill_path": str(target),
            "version": release["version"], "fingerprint": release["fingerprint"],
            "translated_tool_policies": translated, "reconnect_required": True}
