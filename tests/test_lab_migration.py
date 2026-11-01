"""Copied synthetic state relocation; no game installation or process is touched."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import pytest

from personal_lab.migration import (default_state_dir, recover_relocation, relocate_state,
                                    validate_state_ready)
from toolkit_core.safety import ManagedLock
from toolkit_core.storage import Store


@pytest.fixture
def copied_state(tmp_path):
    root = tmp_path / "original"
    store = Store(root)
    original = tmp_path / "original-mod"
    original.mkdir()
    (original / "source.txt").write_bytes(b"never change this original")
    store.blob(b"captured immutable bytes\xff")
    keep = {
        "workspace": {"id": "w", "game_id": "ck3", "target_platform": "macos", "generation": "s",
                      "sources": [{"path": str(original), "owner": "A"}]},
        "scan": {"id": "s", "workspace_id": "w", "manifest_hash": "a" * 64, "game_id": "ck3"},
        "agent_index": {"id": "i", "workspace_id": "w", "scan_id": "s", "manifest_hash": "a" * 64},
        "investigation_projection": {"id": "ip", "workspace_id": "w", "projection": {"exact": True}},
        "agent_session": {"id": "discussion", "workspace_id": "w", "notes": "saved discussion",
                          "text_bindings": {"notes": {"value_hash": "bound", "scan": {"id": "s"}}}},
        "agent_context": {"id": "context", "workspace_id": "w", "scan_id": "s"},
        "agent_concern_review": {"id": "review", "workspace_id": "w", "explanation": "evidence-backed review"},
        "agent_concern_review_history": {"id": "rh", "workspace_id": "w", "explanation": "historical"},
        "agent_review_summary": {"id": "rs", "workspace_id": "w", "complete": 1},
        "agent_preference": {"id": "preference", "workspace_id": "w", "preference": "preserve_current"},
        "agent_mod_candidate": {"id": "mc", "workspace_id": "w", "path": str(original)},
        "agent_candidate_search": {"id": "cs", "workspace_id": "w", "candidates": ["mc"]},
        "discovery": {"id": "discovery", "workspace_id": "w", "installation": {"content_root": str(original)}},
        "job": {"id": "j", "workspace_id": "w", "state": "completed", "result": {"index_id": "i"}},
        "configuration": {"id": "lab-instance", "instance_id": "persistent-instance"},
    }
    saved = {kind: store.put(kind, data) for kind, data in keep.items()}
    plan = {"id": "probe", "prefix": "agent/tests/probe", "profile": str(root / "agent/tests/probe/profile"),
            "argv": ["/never/run/ck3", "-userdir=" + str(root / "agent/tests/probe/profile")],
            "normal_data": str(original), "state": "prepared", "process_started": False}
    immutable = {"agent/tests/probe/plan.json": json.dumps(plan, indent=2).encode(),
                 "agent/tests/probe/isolation.sb": ("deny writes except " + plan["profile"]).encode(),
                 "agent/tests/probe/launch-review.json": b'{"approved":false}',
                 "agent/changes/proposal/hash/fix.txt": b"inactive prepared fix",
                 "artifacts/review.zip": b"immutable output",
                 "evidence-registry/artifacts/raw.txt": b"recorded observation is not a game run"}
    for name, raw in immutable.items():
        store.fs.write_bytes(name, raw)
    store.put("agent_isolation", plan)
    store.put("artifact", {"id": "artifact", "path": str(root / "artifacts/review.zip")})
    store.put("agent_question", {"id": "question", "state": "pending", "nonce": "old-private-authority"})
    store.put("agent_proposal", {"id": "proposal", "state": "approved", "approval_id": "question",
                                 "content_hash": "fixed", "applied_path": "agent/changes/proposal/hash"})
    store.put("grant", {"id": "grant", "state": "available", "digest": "old-plan"})
    store.put("deployment_plan", {"id": "dp", "digest": "immutable-plan-digest", "plan": {"target": "old"}})
    store.fs.write_bytes("agent/connector-key", b"old-authority" * 3)
    store.fs.write_bytes("agent/engine.json", b'{"url":"http://127.0.0.1:1"}')
    store.fs.write_bytes("session-token", b"session-must-not-transfer")
    store.close()
    return root, saved, immutable, original


def relocate(tmp_path, source, **kwargs):
    return relocate_state(source, tmp_path / "relocated", tmp_path / "backup", **kwargs)


def test_complete_copy_preserves_saved_investigation_and_invalidates_old_authority(tmp_path, copied_state):
    root, saved, immutable, original = copied_state
    database_before = (root / "workspace.sqlite3").read_bytes()
    result = relocate(tmp_path, root)
    assert result["relocated"] and not result["game_launched"]
    target = tmp_path / "relocated"
    validate_state_ready(target)
    with pytest.raises(ValueError, match="retired"):
        validate_state_ready(root)
    moved = Store(target)
    try:
        for kind, value in saved.items():
            assert moved.get(kind, value["id"]) == value
        for name, raw in immutable.items():
            assert (target / name).read_bytes() == raw
            assert (root / name).read_bytes() == raw
        probe = moved.get("agent_isolation", "probe")
        assert probe["state"] == "historical" and probe["relocation_historical"]
        assert probe["argv"][1].startswith("-userdir=" + str(root))
        assert not probe["execution_eligible"]
        assert moved.get("artifact", "artifact")["path"] == str(target / "artifacts/review.zip")
        assert moved.get("agent_question", "question")["state"] == "invalidated"
        assert moved.get("grant", "grant")["state"] == "invalidated"
        proposal = moved.get("agent_proposal", "proposal")
        assert proposal["approval_id"] is None and proposal["state"] == "requires_fresh_review"
        assert proposal["applied_path"] == "agent/changes/proposal/hash"
        assert moved.get("deployment_plan", "dp")["migration_ineligible"]
        assert moved.get("deployment_plan", "dp")["digest"] == "immutable-plan-digest"
        assert (target / "agent/connector-key").read_bytes() != b"old-authority" * 3
        assert not (target / "agent/engine.json").exists()
        assert not (target / "session-token").exists()
    finally:
        moved.close()
    assert (root / "workspace.sqlite3").read_bytes() == database_before
    assert (original / "source.txt").read_bytes() == b"never change this original"
    backup_manifest = json.loads((tmp_path / "backup/backup-manifest.json").read_bytes())
    by_path = {row["path"]: row for row in backup_manifest["files"]}
    for name, raw in immutable.items():
        assert by_path[name]["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("kind,state", [("job", "queued"), ("job", "running"), ("run", "cancelling"),
                                         ("agent_isolation", "unknown"), ("agent_isolation", "starting"),
                                         ("agent_isolation", "stopping"), ("deployment", "recovery_required")])
def test_active_work_must_be_resolved_before_backup(tmp_path, copied_state, kind, state):
    root, *_ = copied_state
    store = Store(root)
    store.put(kind, {"id": "active", "state": state})
    store.close()
    with pytest.raises(ValueError):
        relocate(tmp_path, root)
    assert not (tmp_path / "backup").exists()
    assert not (tmp_path / "relocated").exists()


def test_unknown_probe_handle_is_not_migrated_even_with_nonactive_label(tmp_path, copied_state):
    root, *_ = copied_state
    store = Store(root)
    store.put("agent_isolation", {"id": "active", "state": "failed", "process_started": True, "pid": 123})
    store.close()
    with pytest.raises(ValueError, match="terminal process"):
        relocate(tmp_path, root)


def test_concurrent_execution_owner_is_not_interrupted(tmp_path, copied_state):
    root, *_ = copied_state
    with ManagedLock(root / "coordinator.lock"):
        with pytest.raises(ValueError, match="locked"):
            relocate(tmp_path, root)
    assert not (tmp_path / "backup").exists()


def test_external_journal_blocks_relocation_without_recovery(tmp_path, copied_state):
    root, *_ = copied_state
    (root / "managed/journals").mkdir(parents=True)
    (root / "managed/journals/write.json").write_text('{"state":"published"}')
    with pytest.raises(ValueError, match="journals"):
        relocate(tmp_path, root)


@pytest.mark.parametrize("fault", ["after_backup", "before_publish"])
def test_prepublication_failure_keeps_original_and_complete_backup(tmp_path, copied_state, fault):
    root, _, immutable, _ = copied_state
    with pytest.raises(RuntimeError):
        relocate(tmp_path, root, fault_at=fault)
    assert (tmp_path / "backup/backup-manifest.json").is_file()
    assert not (tmp_path / "relocated").exists()
    assert not list(tmp_path.glob(".relocated.partial-*"))
    validate_state_ready(root)
    for name, raw in immutable.items():
        assert (root / name).read_bytes() == raw


def test_published_interruption_requires_verified_explicit_recovery(tmp_path, copied_state):
    root, *_ = copied_state
    with pytest.raises(RuntimeError):
        relocate(tmp_path, root, fault_at="after_publish")
    with pytest.raises(ValueError, match="explicit recovery"):
        validate_state_ready(tmp_path / "relocated")
    assert recover_relocation(tmp_path / "relocated")["relocated"]
    validate_state_ready(tmp_path / "relocated")
    assert recover_relocation(tmp_path / "relocated")["already_complete"]


@pytest.mark.parametrize("tamper", ["source", "destination", "backup"])
def test_recovery_rejects_changed_copies(tmp_path, copied_state, tamper):
    root, *_ = copied_state
    with pytest.raises(RuntimeError):
        relocate(tmp_path, root, fault_at="after_publish")
    selected = {"source": root, "destination": tmp_path / "relocated", "backup": tmp_path / "backup"}[tamper]
    (selected / "artifacts/review.zip").write_bytes(b"external edit")
    with pytest.raises(ValueError):
        recover_relocation(tmp_path / "relocated")
    assert not (root / "relocation-retired.json").exists()


def test_artifact_path_outside_root_fails_closed(tmp_path, copied_state):
    root, *_ = copied_state
    store = Store(root)
    store.put("artifact", {"id": "foreign", "path": str(tmp_path / "private-file")})
    store.close()
    with pytest.raises(ValueError, match="outside"):
        relocate(tmp_path, root)
    assert not (tmp_path / "relocated").exists()


def test_existing_destination_and_overlapping_roots_are_never_overwritten(tmp_path, copied_state):
    root, *_ = copied_state
    (tmp_path / "relocated").mkdir()
    (tmp_path / "relocated/sentinel").write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        relocate(tmp_path, root)
    assert (tmp_path / "relocated/sentinel").read_bytes() == b"keep"
    with pytest.raises(ValueError, match="separate"):
        relocate_state(root, root / "child", tmp_path / "other-backup")


@pytest.mark.parametrize("name", ["", "../escape", "a/b", ".", "a b", "x" * 81])
def test_default_state_name_is_bounded_and_path_free(name):
    with pytest.raises(ValueError):
        default_state_dir(name)


def test_state_default_is_outside_checkout():
    assert default_state_dir("personal").is_absolute()
    assert default_state_dir("personal").name == "personal"
    assert "personal-lab" in str(default_state_dir("personal"))


def test_relocated_probe_controls_refuse_before_any_process_or_source_access(tmp_path, copied_state, monkeypatch):
    from backend.agent import ck3_test

    root, *_ = copied_state
    relocate(tmp_path, root)
    moved = Store(tmp_path / "relocated")

    def forbidden(*args, **kwargs):
        pytest.fail("Historical probes must not touch process controls")

    monkeypatch.setattr(ck3_test.subprocess, "run", forbidden)
    monkeypatch.setattr(ck3_test.subprocess, "Popen", forbidden)
    monkeypatch.setattr(ck3_test, "normal_inventory", forbidden)
    try:
        record = moved.get("agent_isolation", "probe")
        for call in (lambda: ck3_test._preconditions(moved, record),
                     lambda: ck3_test.check_os_boundary(moved, record),
                     lambda: ck3_test.probe_status(moved, "probe"),
                     lambda: ck3_test.stop_probe(moved, "probe"),
                     lambda: ck3_test.start_probe(moved, "probe")):
            with pytest.raises(ValueError):
                call()
    finally:
        moved.close()


def test_a_later_explicit_relocation_preserves_completed_migration_history(tmp_path, copied_state):
    root, *_ = copied_state
    relocate(tmp_path, root)
    first_manifest = (tmp_path / "relocated/migration-state.json").read_bytes()
    result = relocate_state(tmp_path / "relocated", tmp_path / "second", tmp_path / "second-backup")
    assert result["relocated"]
    validate_state_ready(tmp_path / "second")
    assert (tmp_path / "second-backup/migration-state.json").read_bytes() == first_manifest


def test_real_ck3_service_saved_context_resumes_after_relocation_without_rescan(tmp_path):
    from backend.agent.context import AssistantContext
    from backend.app.service import Service
    from test_agent_context import finish
    from test_ck3_service import installation

    options, _ = installation(tmp_path / "sources", "macos")
    root = tmp_path / "service-state"
    service = Service(root)
    try:
        flow = AssistantContext(service)
        context = flow.get(locations=options)
        checked = finish(service, flow.request_check(context["id"], "Preserve this collection"))
        ready = flow.get(selection="resume", workspace_id=checked["workspace_id"], locations=options)
        saved = flow.remember(ready["id"], notes="Saved bounded review", next_step="Continue saved investigation")
        bindings = service.store.get("agent_session", saved["id"])["text_bindings"]
        counts = {kind: len(service.store.list(kind)) for kind in ("scan", "agent_index", "job")}
    finally:
        service.close()
    relocate_state(root, tmp_path / "service-relocated", tmp_path / "service-backup")
    resumed = Service(tmp_path / "service-relocated")
    try:
        public = AssistantContext(resumed).get(selection="resume", workspace_id=checked["workspace_id"], locations=options)
        assert public["conversation"]["notes"] == "Saved bounded review"
        assert public["conversation"]["next_step"] == "Continue saved investigation"
        assert not public["conversation"]["text_disclosure"]["redacted_fields"]
        assert resumed.store.get("agent_session", saved["id"])["text_bindings"] == bindings
        assert {kind: len(resumed.store.list(kind)) for kind in counts} == counts
    finally:
        resumed.close()


def test_core_plan_and_grant_cannot_be_reauthorized_after_relocation(tmp_path, copied_state):
    from toolkit_core.authorization import PlanManager

    root, *_ = copied_state
    original = Store(root)
    manager = PlanManager(original)
    plan = manager.prepare("fixture_write", "managed_target", "a" * 64, {}, [])
    manager.approve(plan["id"], plan["plan_digest"])
    original.close()
    relocate(tmp_path, root)
    moved = Store(tmp_path / "relocated")
    try:
        assert moved.get("lab_plan", plan["id"])["plan"] == plan["plan"]
        assert moved.get("lab_grant", "grant_" + plan["id"])["state"] == "invalidated"
        with pytest.raises(ValueError, match="another runtime instance"):
            PlanManager(moved).approve(plan["id"], plan["plan_digest"])
    finally:
        moved.close()


def test_unresolved_core_write_journal_blocks_relocation(tmp_path, copied_state):
    root, *_ = copied_state
    store = Store(root)
    store.put("lab_journal", {"id": "write_intent", "state": "intent"})
    store.close()
    with pytest.raises(ValueError, match="operation journal"):
        relocate(tmp_path, root)


def test_interruption_after_original_retirement_can_finish_only_by_verified_recovery(tmp_path, copied_state, monkeypatch):
    from personal_lab import migration

    root, *_ = copied_state
    destination = tmp_path / "relocated"
    original = migration.ManagedFS.write_bytes
    failed = False

    def interrupted(self, relative, raw, *args, **kwargs):
        nonlocal failed
        if self.root == destination and relative == "migration-state.json" and not failed:
            failed = True
            raise OSError("completion interrupted after retirement")
        return original(self, relative, raw, *args, **kwargs)

    monkeypatch.setattr(migration.ManagedFS, "write_bytes", interrupted)
    with pytest.raises(OSError, match="after retirement"):
        relocate(tmp_path, root)
    with pytest.raises(ValueError, match="retired"):
        validate_state_ready(root)
    with pytest.raises(ValueError, match="explicit recovery"):
        validate_state_ready(destination)
    assert recover_relocation(destination)["relocated"]
    validate_state_ready(destination)


def test_cli_relocation_maps_absent_previous_root_without_rewriting_evidence(tmp_path, copied_state, capsys):
    from personal_lab.cli import main

    root, _, immutable, _ = copied_state
    previous = tmp_path / "before-checkout-rename"
    old_path = str(previous / "artifacts/review.zip")
    store = Store(root)
    store.put("artifact", {"id": "artifact", "path": old_path})
    store.put("artifact", {"id": "current-root", "path": str(root / "artifacts/review.zip")})
    store.close()
    original_database = (root / "workspace.sqlite3").read_bytes()
    destination, backup = tmp_path / "relocated", tmp_path / "backup"
    assert main(["migrate", str(root), str(destination), "--backup", str(backup),
                 "--previous-source-root", str(previous)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["previous_source_root"] == str(previous)
    assert not receipt["game_launched"]
    state = json.loads((destination / "migration-state.json").read_bytes())
    assert state["state"] == "complete" and state["previous_source_root"] == str(previous)
    assert not previous.exists()
    assert (root / "workspace.sqlite3").read_bytes() == original_database
    for selected in (root, backup):
        with sqlite3.connect((selected / "workspace.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
            payload = json.loads(connection.execute("SELECT payload FROM records WHERE id='artifact'").fetchone()[0])
            assert payload["path"] == old_path
    moved = Store(destination)
    try:
        for identifier in ("artifact", "current-root"):
            assert moved.get("artifact", identifier)["path"] == str(destination / "artifacts/review.zip")
    finally:
        moved.close()
    for name, raw in immutable.items():
        assert (root / name).read_bytes() == (backup / name).read_bytes() == (destination / name).read_bytes() == raw


@pytest.mark.parametrize("invalid", ["existing-directory", "existing-file", "relative", "overlap", "parent-traversal"])
def test_invalid_previous_root_is_refused_before_backup(tmp_path, copied_state, invalid):
    root, *_ = copied_state
    if invalid == "existing-directory":
        previous = tmp_path / "existing"
        previous.mkdir()
    elif invalid == "existing-file":
        previous = tmp_path / "existing-file"
        previous.write_bytes(b"keep")
    elif invalid == "relative":
        previous = "relative-former-root"
    elif invalid == "parent-traversal":
        previous = tmp_path / "absent" / ".." / "old"
    else:
        previous = root / "absent-child"
    before = (root / "workspace.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="previous source root"):
        relocate(tmp_path, root, previous_source_root=previous)
    assert not (tmp_path / "backup").exists()
    assert not (tmp_path / "relocated").exists()
    assert (root / "workspace.sqlite3").read_bytes() == before


def test_previous_source_root_cannot_be_a_dangling_link(tmp_path, copied_state):
    root, *_ = copied_state
    previous = tmp_path / "old-link"
    try:
        previous.symlink_to(tmp_path / "missing", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Symlink creation is unavailable: {exc}")
    with pytest.raises(ValueError, match="link/reparse"):
        relocate(tmp_path, root, previous_source_root=previous)
    assert not (tmp_path / "backup").exists()
    assert previous.is_symlink()


@pytest.mark.parametrize("invalid", ["wrong-root", "missing-relative-file"])
def test_previous_root_mapping_still_requires_verified_artifact(tmp_path, copied_state, invalid):
    root, *_ = copied_state
    previous = tmp_path / "before-rename"
    artifact_path = ((tmp_path / "unrelated" / "artifacts/review.zip") if invalid == "wrong-root"
                     else (previous / "artifacts/absent.zip"))
    store = Store(root)
    store.put("artifact", {"id": "artifact", "path": str(artifact_path)})
    store.close()
    before = (root / "workspace.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="outside|absent"):
        relocate(tmp_path, root, previous_source_root=previous)
    assert not (tmp_path / "relocated").exists()
    assert not (root / "relocation-retired.json").exists()
    assert (root / "workspace.sqlite3").read_bytes() == before
    with sqlite3.connect((tmp_path / "backup/workspace.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
        assert json.loads(connection.execute("SELECT payload FROM records WHERE id='artifact'").fetchone()[0])["path"] == str(artifact_path)
