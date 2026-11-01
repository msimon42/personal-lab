"""Synthetic application contracts; these tests execute no CK3 game code."""
import json
import hashlib
import os
import shutil
import time
import uuid

import pytest

from backend.app.service import Service
from backend.app.store import Conflict
from backend.engine.games import analyze_for_game
from backend.engine.safety import hash_source_file, read_source_bytes


def installation(tmp_path, platform):
    steam = tmp_path / "Steam"
    install = steam / "steamapps/common/Crusader Kings III"
    user = tmp_path / "user"
    (install / "game/common/traits").mkdir(parents=True)
    (install / "game/common/traits/base.txt").write_bytes(b"baseline = { diplomacy = 1 }\n")
    (install / "launcher").mkdir()
    (user / "mod").mkdir(parents=True)
    (install / "launcher/launcher-settings.json").write_text(json.dumps({
        "gameId": "ck3", "rawVersion": "synthetic-build", "gameDataPath": str(user), "dlcPath": "../game",
        "exePath": "../binaries/ck3.exe" if platform == "windows" else "../binaries/ck3.app/Contents/MacOS/ck3"}))
    (steam / "steamapps/appmanifest_1158310.acf").write_text(
        '"AppState" { "appid" "1158310" "installdir" "Crusader Kings III" }')
    order = []
    for name, score in (("one", 2), ("two", 3)):
        payload = user / "mod" / name
        (payload / "common/traits").mkdir(parents=True)
        (payload / "common/traits/shared.txt").write_bytes(f"clever = {{ diplomacy = {score} }}\r\n".encode())
        registry = f"mod/{name}.mod"
        (user / registry).write_text(f'name="{name}"\npath="mod/{name}"\nreplace_path="common/traits"\n')
        order.append(registry)
    (user / "dlc_load.json").write_text(json.dumps({"enabled_mods": order, "disabled_dlcs": []}))
    return {"platform": platform, "steam_roots": [str(steam)], "user_data_path": str(user)}, user


def execute(service, workspace, command, payload):
    value = service.command({"command": command, "workspace_id": workspace,
        "payload": payload, "idempotency_key": uuid.uuid4().hex})
    if "job_id" not in value:
        return value
    for _ in range(500):
        job = service.store.get("job", value["job_id"])
        if job["state"] in {"completed", "failed", "cancelled"}:
            assert job["state"] == "completed", job
            return job["result"]
        time.sleep(.01)
    pytest.fail("CK3 job did not finish")


@pytest.mark.parametrize("platform", ["macos", "windows"])
def test_ck3_application_discovery_import_scan_preserves_originals_and_blocks_repairs(tmp_path, platform):
    options, user = installation(tmp_path / "sources", platform)
    source_root = tmp_path / "sources"
    before = {str(path.relative_to(source_root)): path.read_bytes() for path in source_root.rglob("*") if path.is_file()}
    service = Service(tmp_path / "managed")
    try:
        bootstrap = service.bootstrap()
        assert all(entry["status"] == "proposed_not_run" for entry in bootstrap["ck3_experiments"])
        experiment = next(row for row in bootstrap["ck3_experiments"] if row.get("id") == "CK3-FILES-01")
        assert experiment["game_id"] == "ck3" and experiment["label"] == "P"
        assert bootstrap["game_capabilities"]["ck3"]["production_rules"] == 0
        workspace = execute(service, None, "create_workspace", {
            "game_id": "ck3", "target_platform": platform, "mode": "game", "name": "CK3 fixture"})["workspace_id"]
        discovery = execute(service, workspace, "discover_ck3", options)
        discovery_record = service.store.get("discovery", discovery["discovery_id"])
        assert discovery_record["game_id"] == "ck3"
        assert all("bytes_base64" not in item for item in discovery_record["metadata_inputs"])
        execute(service, workspace, "import_ck3", {"discovery_id": discovery["discovery_id"]})
        imported = service.workspace(workspace)
        assert imported["workspace"]["default_playset"] == ["ck3:mod/one.mod", "ck3:mod/two.mod"]
        result = execute(service, workspace, "scan", {})
        scan = service.store.get("scan", result["scan_id"])
        assert scan["game_id"] == "ck3" and scan["target_platform"] == platform
        assert scan["analysis"]["profile"]["game_id"] == "ck3"
        assert scan["analysis"]["profile"]["target_platform"] == platform
        assert scan["analysis"]["coverage"]["game_runs"] == 0
        assert {"overlapping_path", "duplicate_definition", "replace_path_scope"} <= {
            finding["kind"] for finding in scan["analysis"]["findings"]}
        assert all(definition["state"] == "unknown" for definition in scan["analysis"]["definitions"])
        assert not scan["analysis"]["graphs"]["operation"]
        assert len(scan["package_metadata"]) == 2
        assert all("bytes_base64" not in entry for entry in scan["package_metadata"])
        scope = service.store.get("workspace", workspace)
        for operation, payload in ((service._stage, {"scan_id": result["scan_id"]}),
                                   (service._validate, {"candidate_id": "untrusted"}),
                                   (service._export, {"candidate_id": "untrusted"})):
            with pytest.raises(ValueError, match="CK3 automatic repairs"):
                operation(scope, payload, lambda *args: None)
        runtime = execute(service, workspace, "runtime_probe", {})
        assert runtime["outcome"] == "not_run" and runtime["game_id"] == "ck3"
        assert not service.store.list("candidate", workspace)
        assert not (service.root / "managed/active-release.json").exists()
        (user / "mod/one.mod").write_bytes(b'name="changed outside application"\n')
        with pytest.raises(Conflict, match="rediscover"):
            service._current_scan(scope, result["scan_id"])
        (user / "mod/one.mod").write_bytes(before["user/mod/one.mod"])
    finally:
        service.close()
    assert {str(path.relative_to(source_root)): path.read_bytes() for path in source_root.rglob("*") if path.is_file()} == before


def test_migration_and_ck3_synthetic_cannot_invoke_eu5_contracts(tmp_path):
    service = Service(tmp_path / "managed")
    legacy = service.store.list("workspace")[0]
    legacy.pop("game_id")
    legacy.pop("target_platform")
    service.store.put("workspace", legacy)
    service.close()
    service = Service(tmp_path / "managed")
    try:
        # Legacy interpretation is a read projection; startup must not rewrite
        # historical state without an explicit backed-up migration.
        historical = service.store.get("workspace", legacy["id"])
        assert "game_id" not in historical and "target_platform" not in historical
        migrated = service.workspace(legacy["id"])["workspace"]
        assert (migrated["game_id"], migrated["target_platform"]) == ("eu5", "windows")
        ck3 = service.create_workspace({"game_id": "ck3", "mode": "synthetic", "target_platform": "macos"})
        with pytest.raises(ValueError, match="EU5 fixtures"):
            service._scan(ck3, {"fixture_id": "F03"}, lambda *args: None)
        with pytest.raises(ValueError, match="EU5 profiles"):
            service._scan(ck3, {"profile_id": "synthetic-contract-v1"}, lambda *args: None)
        with pytest.raises(ValueError, match="CK3 game workspace"):
            service._discover_ck3(migrated, {}, lambda *args: None)
    finally:
        service.close()


def test_game_scope_and_schema_revisions_bind_scan(tmp_path, monkeypatch):
    from backend.engine import ck3 as ck3_adapter
    source = tmp_path / "source"
    source.mkdir()
    (source / "descriptor.mod").write_bytes(b'name="fixture"\n')
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos",
            "sources": [{"owner": "one", "path": str(source)}]})
        scan = service._scan(workspace, {}, lambda *args: None)
        with pytest.raises(ValueError, match="different game"):
            analyze_for_game([], [], game_id="ck3", profile={"game_id": "eu5"})
        with pytest.raises(ValueError, match="different target platform"):
            analyze_for_game([], [], game_id="ck3", target_platform="macos",
                             profile={"game_id": "ck3", "target_platform": "windows"})
        from backend.engine.loading import normalized_profile
        from backend.engine.planner import plan_repair
        from backend.engine.runtime import RuntimeWorker
        with pytest.raises(ValueError, match="game adapter"):
            normalized_profile({"game_id": "ck3", "kind": "synthetic"})
        with pytest.raises(ValueError, match="game adapter"):
            plan_repair(service.store.get("scan", scan["scan_id"])["analysis"], [])
        with pytest.raises(ValueError, match="CK3 runtime is unqualified"):
            RuntimeWorker(tmp_path / "runtime", target={"game_id": "ck3"})
        assert not (tmp_path / "runtime").exists()
        original = ck3_adapter.schema_manifest
        monkeypatch.setattr(ck3_adapter, "schema_manifest", lambda: {**original(), "revision": "changed"})
        with pytest.raises(Conflict, match="schema changed"):
            service._current_scan(workspace, scan["scan_id"])
    finally:
        service.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX source names; Windows output rejection is tested separately")
def test_ck3_source_capture_retains_posix_names_that_are_not_portable(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    raw = b"\xef\xbb\xbfunknown=1\r\n"
    (source / "NUL.txt").write_bytes(raw)
    assert read_source_bytes(source, "NUL.txt", native_paths=True) == raw
    assert hash_source_file(source, "NUL.txt", native_paths=True)["size"] == len(raw)
    with pytest.raises(ValueError):
        read_source_bytes(source, "NUL.txt")
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos",
            "sources": [{"owner": "one", "path": str(source)}]})
        result = service._scan(workspace, {}, lambda *args: None)
        record = service.store.get("scan", result["scan_id"])
        captured = next(item for item in record["inputs"] if item["path"] == "NUL.txt")
        assert service.store.fs.read_bytes(f"blobs/sha256/{captured['sha256'][:2]}/{captured['sha256']}") == raw
        assert any(finding["kind"] == "portability" for finding in record["analysis"]["findings"])
    finally:
        service.close()


def test_cross_workspace_discovery_cannot_be_imported(tmp_path):
    options, _ = installation(tmp_path / "sources", "macos")
    service = Service(tmp_path / "managed")
    try:
        one = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos"})
        two = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos"})
        discovery = service._discover_ck3(one, options, lambda *args: None)
        with pytest.raises(ValueError, match="does not belong"):
            service._import_ck3(two, {"discovery_id": discovery["discovery_id"]})
    finally:
        service.close()


def test_unstable_discovery_and_managed_source_overlap_cannot_import(tmp_path):
    options, _ = installation(tmp_path / "sources", "macos")
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos"})
        found = service._discover_ck3(workspace, options, lambda *args: None)
        discovery = service.store.get("discovery", found["discovery_id"])
        service.store.put("discovery", {**discovery, "consistent": False})
        with pytest.raises(Conflict, match="stable selection"):
            service._import_ck3(workspace, {"discovery_id": discovery["id"]})
        with pytest.raises(ValueError, match="must not overlap"):
            service._discover_ck3(workspace, {**options, "user_data_path": str(service.root)}, lambda *args: None)
        alias = service.root.with_name(service.root.name.upper())
        if alias.exists() and alias.samefile(service.root):
            with pytest.raises(ValueError, match="must not overlap"):
                service._guard_discovery_roots({"user_data_path": str(alias)})
            with pytest.raises(ValueError, match="must not overlap"):
                service.create_workspace({"game_id": "ck3", "mode": "game", "sources": [{"owner": "A", "path": str(alias)}]})
        assert not service.store.get("workspace", workspace["id"])["sources"]
    finally:
        service.close()


def test_missing_enabled_mod_keeps_partial_scan_coverage_explicit(tmp_path):
    options, user = installation(tmp_path / "sources", "macos")
    (user / "mod/one/common/traits/shared.txt").unlink()
    (user / "mod/one/common/traits").rmdir()
    (user / "mod/one/common").rmdir()
    (user / "mod/one").rmdir()
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos"})
        found = service._discover_ck3(workspace, options, lambda *args: None)
        assert found["discovery"]["consistent"] and not found["discovery"]["complete"]
        service._import_ck3(workspace, {"discovery_id": found["discovery_id"]})
        workspace = service.store.get("workspace", workspace["id"])
        scanned = service._scan(workspace, {}, lambda *args: None)
        analysis = service.store.get("scan", scanned["scan_id"])["analysis"]
        assert analysis["summary"]["state"] == "needs_attention"
        assert analysis["coverage"]["source_inventory_complete"] is False
        assert analysis["coverage"]["unavailable_enabled_sources"] == ["ck3:mod/one.mod"]
        assert analysis["coverage"]["conformed_rules"] == 0
    finally:
        service.close()


def test_missing_mod_findings_keep_distinct_registry_identity_names_and_provenance(tmp_path):
    options, user = installation(tmp_path / "sources", "macos")
    for name in ("one", "two"):
        shutil.rmtree(user / "mod" / name)
    source_root = tmp_path / "sources"
    before = {str(path.relative_to(source_root)): path.read_bytes() for path in source_root.rglob("*") if path.is_file()}
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos"})
        found = service._discover_ck3(workspace, options, lambda *args: None)
        service._import_ck3(workspace, {"discovery_id": found["discovery_id"]})
        workspace = service.store.get("workspace", workspace["id"])
        scanned = service._scan(workspace, {}, lambda *args: None)
        analysis = service.store.get("scan", scanned["scan_id"])["analysis"]
        missing = [row for row in analysis["findings"] if row["kind"] == "mod_payload_unavailable"]
        assert len(missing) == 2 and len({row["id"] for row in missing}) == 2
        assert {row["target"] for row in missing} == {"mod/one.mod", "mod/two.mod"}
        assert {row["title"] for row in missing} == {"one · Mod payload unavailable", "two · Mod payload unavailable"}
        for row in missing:
            registry = row["target"]
            assert row["message"].startswith(registry + ": ")
            assert row["severity"] == "error"
            assert row["diagnostic"]["registry_path"] == registry
            assert row["diagnostic_provenance"]["discovery_id"] == found["discovery_id"]
            assert row["diagnostic_provenance"]["registry_path"] == registry
            assert row["diagnostic_provenance"]["descriptor_sha256"] == hashlib.sha256((user / registry).read_bytes()).hexdigest()
            assert row["source_occurrences"][0]["path"] == registry
            assert row["coverage"] == "discovery" and row["repairability"] == "blocked_unknown"
        assert {str(path.relative_to(source_root)): path.read_bytes() for path in source_root.rglob("*") if path.is_file()} == before
    finally:
        service.close()


@pytest.mark.parametrize("change", ["descriptor", "schema"])
def test_metadata_or_schema_change_during_scan_never_publishes(tmp_path, monkeypatch, change):
    from backend.engine import ck3 as ck3_adapter
    options, user = installation(tmp_path / "sources", "macos")
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos"})
        found = service._discover_ck3(workspace, options, lambda *args: None)
        service._import_ck3(workspace, {"discovery_id": found["discovery_id"]})
        workspace = service.store.get("workspace", workspace["id"])
        original = ck3_adapter.schema_manifest

        def checkpoint(phase, *args):
            if phase == "analyzing frozen snapshot":
                if change == "descriptor":
                    (user / "mod/one.mod").write_bytes(b'name="changed externally"\n')
                else:
                    monkeypatch.setattr(ck3_adapter, "schema_manifest", lambda: {**original(), "revision": "new"})
        with pytest.raises(Conflict):
            service._scan(workspace, {}, checkpoint)
        assert not service.store.list("scan", workspace["id"])
        assert service.store.get("workspace", workspace["id"])["generation"] is None
    finally:
        service.close()
