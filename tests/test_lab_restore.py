"""Restored backups are independent readable instances with historical authority."""
from __future__ import annotations

import hashlib
import json

import pytest

from personal_lab.migration import (recover_restore, relocate_state, restore_state,
                                    validate_state_ready)
from toolkit_core.artifacts import ArtifactManager
from toolkit_core.authorization import PlanManager
from toolkit_core.backups import BackupManager
from toolkit_core.contracts import ensure_instance
from toolkit_core.jobs import RunManager
from toolkit_core.storage import Store
from test_lab_migration import copied_state as copied_state


def inventory(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def backup_state(tmp_path, copied_state):
    root, saved, immutable, original = copied_state
    store = Store(root)
    old_instance = ensure_instance(store)
    plans = PlanManager(store)
    plan = plans.prepare("fixture_write", "fixture_target", "c" * 64, {}, [])
    plans.approve(plan["id"], plan["plan_digest"])
    artifact = ArtifactManager(store, old_instance).register_bytes(b"readable immutable artifact", "text/plain")
    store.put("run", {"id": "completed_run", "instance_id": old_instance, "state": "succeeded", "result": {"done": True}})
    store.put("run", {"id": "interrupted_run", "instance_id": old_instance, "state": "running", "result": None})
    store.put("lab_eu5_question", {"id": "pending_eu5", "state": "pending", "choices": ["A", "B"]})
    store.close()
    backup = tmp_path / "backup"
    BackupManager(root).create(backup)
    return root, backup, saved, immutable, original, old_instance, plan, artifact


def test_restore_preserves_history_and_backup_while_forking_all_read_ownership(tmp_path, backup_state):
    root, backup, saved, immutable, original, old_instance, plan, artifact = backup_state
    source_before, backup_before = inventory(root), inventory(backup)
    target = tmp_path / "restored"
    result = restore_state(backup, target)
    validate_state_ready(target)
    assert result["restored"] and result["instance_id"] != old_instance
    assert result["source_instance_id"] == old_instance and not result["game_launched"]
    store = Store(target)
    try:
        assert ensure_instance(store) == result["instance_id"]
        for kind, value in saved.items():
            if kind not in {"configuration", "job"}:
                assert store.get(kind, value["id"]) == value
        for name, raw in immutable.items():
            assert (target / name).read_bytes() == raw
        assert store.get("workspace", "w")["generation"] == "s"
        assert store.get("agent_session", "discussion")["text_bindings"] == saved["agent_session"]["text_bindings"]
        assert store.get("agent_question", "question")["state"] == "invalidated"
        assert store.get("lab_eu5_question", "pending_eu5")["state"] == "invalidated"
        assert store.get("agent_proposal", "proposal")["approval_id"] is None
        assert store.get("agent_isolation", "probe")["relocation_historical"]
        assert store.get("lab_plan", plan["id"])["plan"] == plan["plan"]
        assert store.get("lab_grant", "grant_" + plan["id"])["state"] == "invalidated"
        with pytest.raises(ValueError):
            PlanManager(store).approve(plan["id"], plan["plan_digest"])
        owned = store.get("lab_artifact", artifact.id)
        assert owned["origin_instance_id"] == old_instance
        assert owned["instance_id"] == result["instance_id"]
        assert ArtifactManager(store, result["instance_id"]).read(artifact.id)["artifact"]["sha256"] == artifact.sha256
        with pytest.raises(ValueError, match="another instance"):
            ArtifactManager(store, old_instance).read(artifact.id)
        assert store.get("run", "completed_run")["state"] == "succeeded"
        assert store.get("run", "interrupted_run")["state"] == "interrupted"
        assert store.get("run", "interrupted_run")["original_state"] == "running"
        assert (target / "agent/connector-key").read_bytes() != (root / "agent/connector-key").read_bytes()
        assert not (target / "agent/engine.json").exists()
        assert not (target / "session-token").exists()
    finally:
        store.close()
    assert inventory(root) == source_before and inventory(backup) == backup_before
    assert not (root / "relocation-retired.json").exists()
    assert (original / "source.txt").read_bytes() == b"never change this original"
    runtime = RunManager(target)
    try:
        assert runtime.run_get("completed_run")["state"] == "succeeded"
        assert runtime.run_get("interrupted_run")["state"] == "interrupted"
    finally:
        runtime.close()


def test_restored_unknown_probe_cannot_observe_or_control_its_parent(tmp_path, copied_state, monkeypatch):
    from backend.agent import ck3_test
    root, *_ = copied_state
    store = Store(root)
    prior = store.get("agent_isolation", "probe")
    store.put("agent_isolation", {**prior, "state": "unknown", "process_started": True, "pid": 12345})
    store.close()
    backup = tmp_path / "backup"
    BackupManager(root).create(backup)
    restore_state(backup, tmp_path / "restored")
    restored = Store(tmp_path / "restored")
    def forbidden(*args, **kwargs):
        pytest.fail("Restored history cannot inspect or signal a parent's process")
    monkeypatch.setattr(ck3_test, "process_identity", forbidden)
    monkeypatch.setattr(ck3_test.subprocess, "run", forbidden)
    try:
        for call in (lambda: ck3_test.start_probe(restored, "probe"),
                     lambda: ck3_test.probe_status(restored, "probe"),
                     lambda: ck3_test.stop_probe(restored, "probe")):
            with pytest.raises(ValueError):
                call()
    finally:
        restored.close()


@pytest.mark.parametrize("fault", ["before_publish", "after_publish"])
def test_failed_restore_never_creates_an_active_partial_instance(tmp_path, backup_state, fault):
    _, backup, *_ = backup_state
    before = inventory(backup)
    target = tmp_path / "restored"
    with pytest.raises(RuntimeError):
        restore_state(backup, target, fault_at=fault)
    assert inventory(backup) == before
    if fault == "before_publish":
        assert not target.exists()
        assert not list(tmp_path.glob(".restored.partial-*"))
    else:
        with pytest.raises(ValueError, match="explicit recovery"):
            validate_state_ready(target)
        result = recover_restore(target)
        assert result["restored"]
        validate_state_ready(target)
        assert recover_restore(target)["already_complete"]


@pytest.mark.parametrize("copy", ["backup", "destination"])
def test_restore_recovery_refuses_intervening_edits(tmp_path, backup_state, copy):
    _, backup, *_ = backup_state
    target = tmp_path / "restored"
    with pytest.raises(RuntimeError):
        restore_state(backup, target, fault_at="after_publish")
    root = backup if copy == "backup" else target
    (root / "artifacts/review.zip").write_bytes(b"manual change")
    with pytest.raises(ValueError):
        recover_restore(target)
    with pytest.raises(ValueError, match="explicit recovery"):
        validate_state_ready(target)


def test_each_restore_forks_a_distinct_instance_without_changing_the_backup(tmp_path, backup_state):
    _, backup, _, _, _, old_instance, *_ = backup_state
    first = restore_state(backup, tmp_path / "first")
    second = restore_state(backup, tmp_path / "second")
    assert len({old_instance, first["instance_id"], second["instance_id"]}) == 3


@pytest.mark.parametrize("record_kind", ["file", "lab_journal"])
def test_unresolved_external_write_recovery_is_refused_before_publication(tmp_path, copied_state, record_kind):
    root, *_ = copied_state
    if record_kind == "file":
        (root / "managed/journals").mkdir(parents=True)
        (root / "managed/journals/pending.json").write_text('{"state":"files_active"}')
    else:
        store = Store(root)
        store.put("lab_journal", {"id": "pending", "state": "recovery_required"})
        store.close()
    backup = tmp_path / "backup"
    BackupManager(root).create(backup)
    before = inventory(backup)
    with pytest.raises(ValueError, match="unresolved external-write"):
        restore_state(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists() and inventory(backup) == before


def test_prior_operational_root_metadata_is_archived_without_rewriting_bytes(tmp_path, copied_state):
    root, *_ = copied_state
    metadata = {"migration-state.json": b'{"format":"personal-lab-relocation-1","state":"complete"}',
                "relocation-retired.json": b'{"destination_root":"historical-root"}',
                "restore-state.json": b'{"schema_version":1,"kind":"legacy_restore"}'}
    for name, raw in metadata.items():
        (root / name).write_bytes(raw)
    backup = tmp_path / "backup"
    BackupManager(root).create(backup)
    target = tmp_path / "restored"
    result = restore_state(backup, target)
    validate_state_ready(target)
    for name, raw in metadata.items():
        assert (target / "history" / result["restore_id"] / name).read_bytes() == raw
        assert (backup / name).read_bytes() == raw
    assert not (target / "relocation-retired.json").exists()
    assert not (target / "migration-state.json").exists()


def test_restored_instance_can_later_be_relocated_with_its_identity_intact(tmp_path, backup_state):
    _, backup, *_ = backup_state
    first = restore_state(backup, tmp_path / "restored")
    result = relocate_state(tmp_path / "restored", tmp_path / "relocated", tmp_path / "relocation-backup")
    assert result["relocated"]
    validate_state_ready(tmp_path / "relocated")
    store = Store(tmp_path / "relocated")
    try:
        assert ensure_instance(store) == first["instance_id"]
    finally:
        store.close()


def test_real_ck3_saved_discussion_resumes_in_restored_fork_without_rescan(tmp_path):
    from backend.agent.context import AssistantContext
    from backend.app.service import Service
    from test_agent_context import finish
    from test_ck3_service import installation
    options, _ = installation(tmp_path / "sources", "macos")
    source = tmp_path / "state"
    service = Service(source)
    try:
        old_instance = service.instance_id
        flow = AssistantContext(service)
        context = flow.get(locations=options)
        checked = finish(service, flow.request_check(context["id"], "Preserve current collection"))
        ready = flow.get(selection="resume", workspace_id=checked["workspace_id"], locations=options)
        saved = flow.remember(ready["id"], notes="Read this preserved investigation", next_step="Continue review")
        bindings = service.store.get("agent_session", saved["id"])["text_bindings"]
        counts = {kind: len(service.store.list(kind)) for kind in ("scan", "agent_index", "job")}
    finally:
        service.close()
    backup = tmp_path / "backup"
    BackupManager(source).create(backup)
    result = restore_state(backup, tmp_path / "restored")
    restored = Service(tmp_path / "restored")
    try:
        assert old_instance != restored.instance_id == result["instance_id"]
        resumed = AssistantContext(restored).get(selection="resume", workspace_id=checked["workspace_id"], locations=options)
        assert resumed["conversation"]["notes"] == "Read this preserved investigation"
        assert not resumed["conversation"]["text_disclosure"]["redacted_fields"]
        assert restored.store.get("agent_session", saved["id"])["text_bindings"] == bindings
        assert {kind: len(restored.store.list(kind)) for kind in counts} == counts
    finally:
        restored.close()


def test_restore_cli_uses_independent_fork_contract(tmp_path, backup_state, capsys):
    from personal_lab.cli import main
    _, backup, *_ = backup_state
    assert main(["restore", str(backup), str(tmp_path / "restored")]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["restored"] and result["authority_rotated"] and result["instance_id"]
