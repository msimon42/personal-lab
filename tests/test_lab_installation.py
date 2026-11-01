"""Owned connection rename and settings/skill rollback in temporary directories."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tomllib

import pytest
import tomlkit

from toolkit_core.generations import generation_manifest, read_generation, remove_generation
from personal_lab import installation


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    files = {"SKILL.md": b"new ck3_ instructions\n", "agents/openai.yaml": b"metadata\n",
             "references/operating-guide.md": b"new guide\n"}
    release = {"version": "4.0.0", "fingerprint": "f" * 64,
               "workflow_files": {name: checksum(raw) for name, raw in files.items()},
               "implementation_files": {"engine.py": "e" * 64}, "knowledge_files": {"knowledge": "k" * 64}}
    marker = b"CK3 mod assistant\n"
    monkeypatch.setattr(installation, "_helpers", lambda: (
        read_generation, generation_manifest, installation._previous_owned, remove_generation,
        marker, lambda: copy.deepcopy(release), lambda: dict(files)))
    config = tmp_path / "codex/config.toml"
    target = tmp_path / "codex/skills/ck3-mod-agent"
    root = tmp_path / "state"
    return config, target, root, files, release, marker


def publish_old(setup):
    config, target, root, _, _, marker = setup
    target.mkdir(parents=True)
    files = {"SKILL.md": b"old instructions", "agents/openai.yaml": b"old metadata"}
    previous = {"name": installation.LEGACY_NAME, "transport": {"command": str(Path(sys.executable)),
                "args": ["-m", "backend.cli", "agent-tools", "--data-dir", str(root)],
                "env": {"PRESERVE_OPERATOR_ENV": "set"}, "type": "stdio"}}
    for name, raw in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    (target / installation.MARKER).write_bytes(marker)
    receipt = {"version": "old", "fingerprint": "old", "workflow_files": {name: checksum(raw) for name, raw in files.items()},
               "connector": previous}
    (target / installation.RECEIPT).write_text(json.dumps(receipt))
    (target / "personal-note.txt").write_bytes(b"keep unrelated file")
    (target / "personal-note.txt").chmod(0o700)
    text = '# preserve operator comments\nmodel = "operator-choice"\n'
    document = tomlkit.parse(text)
    servers = document.setdefault("mcp_servers", tomlkit.table())
    servers["unrelated"] = {"command": "/unrelated/server", "args": ["keep"]}
    servers[installation.LEGACY_NAME] = {**{key: value for key, value in previous["transport"].items() if key != "type"},
        "default_tools_approval_mode": "prompt", "tool_timeout_sec": 900,
        "enabled_tools": ["get_context", "read_source", "custom_internal"],
        "tools": {"get_context": {"approval_mode": "deny"}, "read_source": {"approval_mode": "prompt"},
                  "custom_internal": {"approval_mode": "deny"}}}
    config.write_text(tomlkit.dumps(document))
    return previous


def install(setup, **kwargs):
    config, target, root, *_ = setup
    return installation.install_connection(root, config_path=config, skill_destination=target,
                                          executable=Path(sys.executable), **kwargs)


def test_fresh_unified_install_has_no_blanket_approval_and_can_be_repeated(setup):
    result = install(setup)
    config, target, root, files, release, _ = setup
    value = tomllib.loads(config.read_text())
    assert set(value["mcp_servers"]) == {"personal_lab"}
    server = value["mcp_servers"]["personal_lab"]
    assert server["args"] == ["-m", "personal_lab", "mcp", "--profile", "personal", "--state-dir", str(root)]
    assert "default_tools_approval_mode" not in server and "tools" not in server
    assert result["fingerprint"] == release["fingerprint"]
    assert all((target / name).read_bytes() == raw for name, raw in files.items())
    assert install(setup)["installed"]
    assert not (target.parent / installation.JOURNAL).exists()


def test_owned_old_connection_names_and_stricter_policies_move_together(setup):
    publish_old(setup)
    config, target, _, files, _, _ = setup
    original = tomllib.loads(config.read_text())
    result = install(setup)
    value = tomllib.loads(config.read_text())
    assert "ck3_mod_agent" not in value["mcp_servers"]
    server = value["mcp_servers"]["personal_lab"]
    assert server["default_tools_approval_mode"] == "prompt"
    assert server["tool_timeout_sec"] == 900
    assert server["enabled_tools"] == ["ck3_get_context", "ck3_read_source", "custom_internal"]
    assert server["tools"] == {"ck3_get_context": {"approval_mode": "deny"},
                                "ck3_read_source": {"approval_mode": "prompt"},
                                "custom_internal": {"approval_mode": "deny"}}
    assert server["env"]["PRESERVE_OPERATOR_ENV"] == "set"
    assert value["model"] == original["model"]
    assert value["mcp_servers"]["unrelated"] == original["mcp_servers"]["unrelated"]
    assert config.read_text().startswith("# preserve operator comments")
    assert (target / "personal-note.txt").read_bytes() == b"keep unrelated file"
    assert all((target / name).read_bytes() == raw for name, raw in files.items())
    assert set(result["translated_tool_policies"]) == {"ck3_get_context", "ck3_read_source"}


@pytest.mark.parametrize("fault", ["before_publish", "after_skill", "after_config"])
def test_each_failed_publish_restores_exact_settings_and_old_skill(setup, fault):
    publish_old(setup)
    config, target, *_ = setup
    before = config.read_bytes()
    old_tree = generation_manifest(read_generation(target))
    with pytest.raises(RuntimeError):
        install(setup, fault_at=fault)
    assert config.read_bytes() == before
    assert generation_manifest(read_generation(target)) == old_tree
    assert not (target.parent / installation.JOURNAL).exists()
    assert not list(target.parent.glob(".personal-lab-stage-*"))
    assert not list(target.parent.glob(".personal-lab-backup-*"))


@pytest.mark.parametrize("fault", ["before_publish", "after_skill", "after_config"])
def test_fresh_install_failure_restores_missing_settings_and_skill(setup, fault):
    config, target, *_ = setup
    with pytest.raises(RuntimeError):
        install(setup, fault_at=fault)
    assert not config.exists() and not target.exists()


def test_unowned_existing_skill_and_connection_are_not_overwritten(setup):
    config, target, *_ = setup
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"operator-created skill")
    with pytest.raises(ValueError, match="not owned"):
        install(setup)
    assert (target / "SKILL.md").read_bytes() == b"operator-created skill"
    assert not config.exists()


def test_unowned_existing_connection_is_not_taken_over(setup):
    config, target, *_ = setup
    config.parent.mkdir()
    config.write_text('[mcp_servers.ck3_mod_agent]\ncommand="/someone/else"\nargs=[]\n')
    before = config.read_bytes()
    with pytest.raises(ValueError, match="not owned"):
        install(setup)
    assert config.read_bytes() == before and not target.exists()


def test_manual_transport_change_is_preserved(setup):
    publish_old(setup)
    config, *_ = setup
    value = tomlkit.parse(config.read_text())
    value["mcp_servers"]["ck3_mod_agent"]["command"] = "/manual/change"
    config.write_text(tomlkit.dumps(value))
    before = config.read_bytes()
    with pytest.raises(ValueError, match="not owned"):
        install(setup)
    assert config.read_bytes() == before


def test_both_connection_names_require_explicit_resolution(setup):
    publish_old(setup)
    config, *_ = setup
    config.write_text(config.read_text() + '\n[mcp_servers.personal_lab]\ncommand="/other"\nargs=[]\n')
    before = config.read_bytes()
    with pytest.raises(ValueError, match="Both"):
        install(setup)
    assert config.read_bytes() == before


def test_new_workflow_file_cannot_replace_unrelated_installed_file(setup):
    publish_old(setup)
    _, target, *_ = setup
    (target / "references").mkdir()
    (target / "references/operating-guide.md").write_bytes(b"operator unrelated file")
    with pytest.raises(ValueError, match="unrelated"):
        install(setup)
    assert (target / "references/operating-guide.md").read_bytes() == b"operator unrelated file"


def test_policy_rename_collision_fails_without_loosening_access(setup):
    publish_old(setup)
    config, *_ = setup
    value = tomlkit.parse(config.read_text())
    value["mcp_servers"]["ck3_mod_agent"]["tools"]["ck3_get_context"] = {"approval_mode": "approve"}
    config.write_text(tomlkit.dumps(value))
    before = config.read_bytes()
    with pytest.raises(ValueError, match="collide"):
        install(setup)
    assert config.read_bytes() == before


def test_intervening_settings_edits_leave_recovery_copies_instead_of_overwrite(setup, monkeypatch):
    publish_old(setup)
    config, target, *_ = setup
    original = installation._write_config

    def changed(path, before, after):
        config.write_text(config.read_text() + '\noperator_new_setting="preserve"\n')
        return original(path, before, after)

    monkeypatch.setattr(installation, "_write_config", changed)
    with pytest.raises(ValueError, match="changed after interrupted"):
        install(setup)
    assert "operator_new_setting" in config.read_text()
    assert (target.parent / installation.JOURNAL).exists()
    assert list(target.parent.glob(".personal-lab-backup-*"))


def test_retired_state_root_cannot_be_installed_as_current(setup):
    _, _, root, *_ = setup
    root.mkdir()
    (root / "relocation-retired.json").write_text('{}')
    with pytest.raises(ValueError, match="retired"):
        install(setup)


def test_multiline_comments_and_operator_transport_settings_survive_upgrade(setup):
    publish_old(setup)
    config, *_ = setup
    prefix = b'''# operator multiline settings\ncustom = """a value\n[mcp_servers.not_a_table]\n"""\n'''
    before = config.read_bytes()
    document = tomlkit.parse(before.decode())
    server = document["mcp_servers"]["ck3_mod_agent"]
    server["cwd"] = "/operator/working-directory"
    server["env_vars"] = ["CUSTOM_VARIABLE"]
    server["env"]["OPERATOR_ADDITION"] = "retained"
    server["tools"]["read_source"]["custom_setting"] = 7
    config.write_bytes(prefix + tomlkit.dumps(document).encode())
    install(setup)
    raw = config.read_bytes()
    assert raw.startswith(prefix)
    server = tomllib.loads(raw.decode())["mcp_servers"]["personal_lab"]
    assert server["cwd"] == "/operator/working-directory"
    assert server["env_vars"] == ["CUSTOM_VARIABLE"]
    assert server["env"]["OPERATOR_ADDITION"] == "retained"
    assert server["tools"]["ck3_read_source"] == {"approval_mode": "prompt", "custom_setting": 7}


@pytest.mark.parametrize("default", ["prompt", "writes", "auto"])
def test_server_approval_defaults_are_not_weakened(setup, default):
    publish_old(setup)
    config, *_ = setup
    document = tomlkit.parse(config.read_text())
    server = document["mcp_servers"]["ck3_mod_agent"]
    server["default_tools_approval_mode"] = default
    del server["tools"]
    config.write_text(tomlkit.dumps(document))
    install(setup)
    updated = tomllib.loads(config.read_text())["mcp_servers"]["personal_lab"]
    assert updated["default_tools_approval_mode"] == default
    assert "tools" not in updated


def test_user_edits_to_owned_workflow_are_preserved(setup):
    install(setup)
    _, target, *_ = setup
    (target / "SKILL.md").write_bytes(b"operator customized instructions")
    with pytest.raises(ValueError, match="changed outside setup"):
        install(setup)
    assert (target / "SKILL.md").read_bytes() == b"operator customized instructions"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_linked_workflow_files_never_touch_the_original(setup, tmp_path, kind):
    publish_old(setup)
    _, target, *_ = setup
    original = tmp_path / "original-bytes"
    original.write_bytes(b"never alter original")
    installed = target / "personal-note.txt"
    installed.unlink()
    try:
        if kind == "symlink":
            installed.symlink_to(original)
        else:
            installed.hardlink_to(original)
    except OSError:
        pytest.skip("This account cannot create the link control")
    with pytest.raises(ValueError):
        install(setup)
    assert original.read_bytes() == b"never alter original"
    assert (target / "SKILL.md").read_bytes() == b"old instructions"


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_linked_settings_are_not_replaced(setup, tmp_path, kind):
    publish_old(setup)
    config, target, *_ = setup
    original = tmp_path / "original-config"
    config.rename(original)
    before = original.read_bytes()
    try:
        if kind == "symlink":
            config.symlink_to(original)
        else:
            config.hardlink_to(original)
    except OSError:
        pytest.skip("This account cannot create the link control")
    with pytest.raises(ValueError):
        install(setup)
    assert original.read_bytes() == before
    assert (target / "SKILL.md").read_bytes() == b"old instructions"


def test_failed_directory_publication_restores_old_generation(setup, monkeypatch):
    publish_old(setup)
    config, target, *_ = setup
    before = config.read_bytes()
    original = generation_manifest(read_generation(target))
    rename = installation.os.rename
    failed = False

    def fail_stage(source, destination):
        nonlocal failed
        if "-stage-" in str(source) and not failed:
            failed = True
            raise OSError("publication failed")
        return rename(source, destination)

    monkeypatch.setattr(installation.os, "rename", fail_stage)
    with pytest.raises(OSError, match="publication"):
        install(setup)
    assert config.read_bytes() == before
    assert generation_manifest(read_generation(target)) == original
    assert not (target.parent / installation.JOURNAL).exists()


def test_keyboard_interrupt_is_recovered_before_next_install(setup, monkeypatch):
    publish_old(setup)
    _, target, _, files, *_ = setup
    original = installation._write_config

    def interrupted(*args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(installation, "_write_config", interrupted)
    with pytest.raises(KeyboardInterrupt):
        install(setup)
    assert (target.parent / installation.JOURNAL).exists()
    monkeypatch.setattr(installation, "_write_config", original)
    assert install(setup)["installed"]
    assert all((target / name).read_bytes() == raw for name, raw in files.items())
    assert (target / "personal-note.txt").read_bytes() == b"keep unrelated file"
    assert not (target.parent / installation.JOURNAL).exists()


def test_intervening_skill_edit_is_preserved_with_recovery_record(setup, monkeypatch):
    publish_old(setup)
    _, target, *_ = setup

    def edited(*args):
        (target / "SKILL.md").write_bytes(b"operator edited during publication")
        raise ValueError("simulated settings failure")

    monkeypatch.setattr(installation, "_write_config", edited)
    with pytest.raises(ValueError, match="changed outside setup"):
        install(setup)
    assert (target / "SKILL.md").read_bytes() == b"operator edited during publication"
    assert (target.parent / installation.JOURNAL).exists()
    assert list(target.parent.glob(".personal-lab-backup-*"))


def test_installed_release_detects_changed_instructions_and_engine(setup, monkeypatch):
    from backend.agent import release
    installed = install(setup)
    _, target, _, _, info, _ = setup
    monkeypatch.setattr(release, "release_info", lambda: copy.deepcopy(info))
    assert release.installation_status(target, info["fingerprint"])["status"] == "ready"
    (target / "SKILL.md").write_bytes(b"changed externally")
    status = release.installation_status(target, "wrong-release")
    assert status["status"] == "mismatch" and len(status["problems"]) == 2
    assert Path(installed["skill_path"]) == target
