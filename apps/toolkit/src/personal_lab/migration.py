"""Explicit, backed-up relocation of a quiescent toolkit instance.

Immutable evidence is copied exactly. This administrative operation never launches
processes, activates a playset, or treats historical authorization as current.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import sys
import time
import uuid

from toolkit_core import backups
from toolkit_core.safety import ManagedFS, ManagedLock, paths_overlap, reject_link_components

FORMAT = "personal-lab-relocation-1"
STATE = "migration-state.json"
RETIRED = "relocation-retired.json"
RESTORE_FORMAT = "personal-lab-restore-1"
RESTORE_STATE = "restore-state.json"
ACTIVE_RUNS = {"queued", "running", "cancelling", "awaiting_user"}
ACTIVE_PROBES = {"starting", "running", "stopping", "unknown"}
FINAL_JOURNALS = {"verified", "rolled_back", "complete", "completed", "failed_to_start"}


def default_state_dir(instance_id: str = "personal") -> Path:
    if (not isinstance(instance_id, str) or not instance_id or len(instance_id) > 80
            or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in instance_id)):
        raise ValueError("Instance names must contain only letters, numbers, underscores and dashes")
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support/personal-lab"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "personal-lab"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "personal-lab"
    return base / "instances" / instance_id


def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def validate_state_ready(root: Path) -> None:
    """Check before acquiring an execution owner; never auto-replay relocation."""
    root = reject_link_components(Path(root).absolute())
    current_relocation = False
    if (root / RETIRED).exists():
        raise ValueError("This state directory was relocated and is retired. Use its recorded destination.")
    if (root / STATE).exists():
        state = json.loads(backups.read_source_bytes(root, STATE, max_bytes=32 * 1024 * 1024))
        if state.get("format") != FORMAT or state.get("state") != "complete":
            raise ValueError("State relocation requires explicit recovery before the toolkit can start")
        if state.get("destination_root") != str(root):
            raise ValueError("The relocated instance was moved again without a verified migration")
        current_relocation = True
    if (root / RESTORE_STATE).exists():
        state = json.loads(backups.read_source_bytes(root, RESTORE_STATE, max_bytes=32 * 1024 * 1024))
        if state.get("format") != RESTORE_FORMAT or state.get("state") != "complete":
            raise ValueError("State restore requires explicit recovery before the toolkit can start")
        if state.get("destination_root") != str(root) and not current_relocation:
            raise ValueError("The restored instance was moved again without a verified migration")


@contextmanager
def _database(root: Path):
    identity = (root / backups.DATABASE).lstat()
    if not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1:
        raise ValueError("The managed SQLite database must be an unshared regular file")
    backups.hash_source_file(root, backups.DATABASE, max_bytes=backups.MAX_FILE_BYTES)
    connection = sqlite3.connect((root / backups.DATABASE).as_uri() + "?mode=ro", uri=True)
    try:
        current = (root / backups.DATABASE).lstat()
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError("The managed SQLite database identity changed before inspection")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("The instance database failed integrity verification")
        yield connection
    finally:
        connection.close()


def _logical_digest(connection) -> str:
    checksum = hashlib.sha256()
    for table in ("migrations", "records", "events", "requests"):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            continue
        checksum.update(table.encode())
        for row in connection.execute(f'SELECT * FROM "{table}" ORDER BY 1'):
            # Scan payloads may be large. Hash exact SQLite text incrementally
            # without decoding saved discussion or duplicating whole JSON rows.
            for value in row:
                if isinstance(value, str):
                    checksum.update(b"s" + str(len(value)).encode() + b":")
                    for offset in range(0, len(value), 65536):
                        checksum.update(value[offset:offset + 65536].encode("utf-8"))
                else:
                    raw = _canonical(value)
                    checksum.update(b"j" + str(len(raw)).encode() + b":" + raw)
    return checksum.hexdigest()


def _quiescent(root: Path, connection) -> None:
    kinds = ("job", "run", "lab_run", "agent_isolation", "deployment", "lab_deployment", "lab_journal")
    selected = ",".join("?" for _ in kinds)
    for _, kind, encoded in connection.execute(
            "SELECT id,kind,payload FROM records WHERE kind IN (" + selected + ")", kinds):
        record = json.loads(encoded)
        if kind in {"job", "run", "lab_run"} and record.get("state") in ACTIVE_RUNS:
            raise ValueError("Finish or safely interrupt all pending runs before relocating state")
        if kind == "agent_isolation" and (record.get("state") in ACTIVE_PROBES or (
                record.get("process_started") and record.get("state") not in {
                    "exited", "stopped", "failed_to_start", "historical"})):
            raise ValueError("An isolation probe needs an observed terminal process state before relocation")
        if kind in {"deployment", "lab_deployment"} and record.get("state") in {
                "starting", "publishing", "activating", "recovery_required"}:
            raise ValueError("Resolve external-write recovery before relocating state")
        if kind == "lab_journal" and record.get("state") != "committed":
            raise ValueError("Resolve the external-write operation journal before relocating state")
    for current, directories, names in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d != "backups"]
        for name in names:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if "/journals/" not in "/" + relative and not name.endswith(".install.json"):
                continue
            if not name.endswith(".json"):
                raise ValueError("An unrecognized external-write journal requires review before relocation")
            value = json.loads(backups.read_source_bytes(root, relative, max_bytes=4 * 1024 * 1024))
            if value.get("state", value.get("phase")) not in FINAL_JOURNALS:
                raise ValueError("Resolve external-write journals before relocating state")


def _file_rows(root: Path):
    rows, _ = backups._inventory(root)
    return sorted(({key: row[key] for key in ("path", "size", "sha256")} for row in rows
                   if row["path"] != RETIRED), key=lambda row: row["path"])


def _verify_source(root: Path, manifest: dict, logical_digest: str) -> None:
    expected = [row for row in manifest["files"] if row["path"] != backups.DATABASE]
    if _file_rows(root) != expected:
        raise ValueError("The original instance changed after backup; relocation was not committed")
    with _database(root) as connection:
        _quiescent(root, connection)
        if _logical_digest(connection) != logical_digest:
            raise ValueError("The original instance database changed after backup")


def _rebase_path(value, old_root: Path, new_root: Path, files: set[str],
                 previous_source_root: Path | None = None):
    if not isinstance(value, str) or not Path(value).is_absolute():
        return value
    for root in (old_root, previous_source_root):
        if root is None:
            continue
        try:
            relative = Path(value).relative_to(root).as_posix()
            break
        except ValueError:
            continue
    else:
        raise ValueError("A registered artifact is outside the managed root; explicit repair is required")
    if relative not in files:
        raise ValueError("A registered artifact is absent from the complete backup")
    return str(new_root / relative)


def _transform(database: Path, old_root: Path, new_root: Path, files: set[str], migration_id: str,
               previous_source_root: Path | None = None):
    connection = sqlite3.connect(database)
    counts = {"records_preserved": 0, "records_updated": 0, "historical_probes": 0,
              "invalidated_authorizations": 0, "rebased_artifacts": 0}
    try:
        mutable_kinds = ("artifact", "lab_artifact", "agent_isolation", "agent_question", "lab_eu5_question",
                         "grant", "lab_grant", "approval_grant", "agent_proposal", "deployment_plan",
                         "rollback_plan", "lab_plan", "candidate", "decision")
        selected = ",".join("?" for _ in mutable_kinds)
        metadata = connection.execute("SELECT id,kind,revision FROM records WHERE kind IN (" + selected + ")",
                                      mutable_kinds).fetchall()
        counts["records_preserved"] = connection.execute("SELECT count(*) FROM records").fetchone()[0] - len(metadata)
        for identifier, kind, revision in metadata:
            encoded = connection.execute("SELECT payload FROM records WHERE id=?", (identifier,)).fetchone()[0]
            record = json.loads(encoded)
            before = _canonical(record)
            if kind in {"artifact", "lab_artifact"} and record.get("path"):
                record["path"] = _rebase_path(record["path"], old_root, new_root, files, previous_source_root)
                counts["rebased_artifacts"] += 1
            if kind == "agent_isolation":
                record.update(relocation_historical=True, execution_eligible=False,
                              original_state=record.get("state"), state="historical", migration_id=migration_id)
                counts["historical_probes"] += 1
            elif kind in {"agent_question", "lab_eu5_question"} and record.get("state") == "pending":
                record.update(state="invalidated", invalidation_reason="Instance relocated; obtain a fresh operator answer")
                counts["invalidated_authorizations"] += 1
            elif kind in {"grant", "lab_grant", "approval_grant"}:
                record.update(state="invalidated", invalidation_reason="Instance relocated")
                counts["invalidated_authorizations"] += 1
            elif kind == "agent_proposal":
                for key in ("approval_id", "test_approval_id", "enable_approval_id", "undo_approval_id"):
                    if record.get(key):
                        record[key] = None
                        counts["invalidated_authorizations"] += 1
                if record.get("state") == "approved":
                    record["state"] = "requires_fresh_review"
                record["relocation_requires_fresh_review"] = True
            elif kind in {"deployment_plan", "rollback_plan", "lab_plan"}:
                record.update(migration_ineligible=True, relocation_historical=True, migration_id=migration_id)
            elif kind == "candidate":
                record.update(status="stale", stale_reason="Relocated state requires source and decision revalidation")
                if isinstance(record.get("validation"), dict):
                    record["validation"].update(experimental_eligible=False, validated_promotion_eligible=False)
            elif kind == "decision":
                record["state"] = "stale"
            if _canonical(record) != before:
                record["revision"] = revision + 1
                connection.execute("UPDATE records SET payload=?,revision=? WHERE id=?",
                                   (_canonical(record).decode(), revision + 1, identifier))
                counts["records_updated"] += 1
            else:
                counts["records_preserved"] += 1
        # Cached write receipts must never replay authority from the former root.
        connection.execute("DELETE FROM requests")
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Relocated SQLite failed integrity verification")
    finally:
        connection.close()
    return counts


def _verify_published(root: Path, state: dict) -> None:
    actual_files = _file_rows(root)
    actual_files = [row for row in actual_files if row["path"] != STATE]
    actual_files.append(backups.hash_source_file(root, backups.DATABASE, max_bytes=backups.MAX_FILE_BYTES))
    if sorted(actual_files, key=lambda row: row["path"]) != sorted(state["published_files"], key=lambda row: row["path"]):
        raise ValueError("Relocated inventory changed before recovery; preserve both copies for review")


def relocate_state(source: Path, destination: Path, backup_destination: Path, *,
                   previous_source_root: Path | None = None, fault_at=None) -> dict:
    """Relocate one stopped instance, retaining its complete verified backup.

    An interrupted published copy is deliberately unusable until recover_relocation
    verifies it. The original database and immutable evidence are never rewritten.
    A trusted administrator may identify an absent former source root after a
    checkout rename; only artifact paths with verified relative files are mapped.
    """
    source = reject_link_components(Path(source).absolute())
    destination = reject_link_components(Path(destination).absolute())
    backup_destination = reject_link_components(Path(backup_destination).absolute())
    for first, second in ((source, destination), (source, backup_destination), (destination, backup_destination)):
        if paths_overlap(first, second):
            raise ValueError("Source, backup and destination roots must be separate")
    if previous_source_root is not None:
        previous_source_root = Path(previous_source_root)
        if not previous_source_root.is_absolute() or ".." in previous_source_root.parts:
            raise ValueError("The previous source root must be an absolute guarded path")
        previous_source_root = reject_link_components(previous_source_root)
        if previous_source_root.exists() or previous_source_root.is_symlink():
            raise ValueError("The previous source root must be absent")
        if any(paths_overlap(previous_source_root, root) for root in (source, destination, backup_destination)):
            raise ValueError("The previous source root must be separate from current source, backup and destination")
    validate_state_ready(source)
    if not source.is_dir():
        raise ValueError("The source instance does not exist")
    published = False
    temporary = None
    with ManagedLock(source / "coordinator.lock"):
        with _database(source) as connection:
            _quiescent(source, connection)
            logical = _logical_digest(connection)
        backups.BackupManager(source).create(backup_destination)
        if fault_at == "after_backup":
            raise RuntimeError("Injected interruption after complete backup")
        manifest, backup_hash = backups._manifest(backup_destination)
        backups._verify(backup_destination, manifest)
        target, temporary, existing = backups._prepare(destination)
        output = ManagedFS(temporary, max_bytes=backups.MAX_FILE_BYTES)
        migration_id = "relocation_" + uuid.uuid4().hex
        try:
            for directory in manifest["directories"]:
                output.resolve(directory).mkdir(parents=True, exist_ok=True)
            for row in manifest["files"]:
                backups._copy(backup_destination, row["path"], output, row)
            backups._verify(backup_destination, manifest)
            files = {row["path"] for row in manifest["files"]}
            counts = _transform(temporary / backups.DATABASE, source, target, files, migration_id, previous_source_root)
            # Connector keys are runtime authority, not historical evidence.
            if "agent/connector-key" in files:
                output.write_bytes("agent/connector-key", secrets.token_bytes(32))
            for endpoint in ("agent/engine.json",):
                if endpoint in files:
                    output.resolve(endpoint).unlink()
            published_files = [row for row in _file_rows(temporary) if row["path"] != STATE]
            published_files.append(backups.hash_source_file(temporary, backups.DATABASE, max_bytes=backups.MAX_FILE_BYTES))
            immutable_files = [row for row in manifest["files"] if row["path"] not in {
                backups.DATABASE, "agent/connector-key", "agent/engine.json", STATE}]
            state = {"format": FORMAT, "id": migration_id, "state": "recovery_required",
                     "source_root": str(source), "destination_root": str(target),
                     "backup_root": str(backup_destination), "backup_manifest_sha256": backup_hash,
                     "source_database_logical_sha256": logical, "created_at": time.time(),
                     "immutable_files_sha256": _hash(_canonical(immutable_files)),
                     "immutable_file_count": len(immutable_files),
                     "published_files": published_files, "authority_rotated": True, **counts}
            if previous_source_root is not None:
                state["previous_source_root"] = str(previous_source_root)
            if len(_canonical(state)) > 32 * 1024 * 1024:
                raise ValueError("Relocation metadata exceeds its explicit 32 MiB verification budget")
            output.write_bytes(STATE, _canonical(state), replace=STATE in files)
            _verify_source(source, manifest, logical)
            if fault_at == "before_publish":
                raise RuntimeError("Injected interruption before publication")
            backups._publish(temporary, target, existing)
            published = True
            if fault_at == "after_publish":
                raise RuntimeError("Injected interruption after publication; run explicit relocation recovery")
            return _complete_relocation(source, target, state, manifest)
        except Exception:
            if not published and temporary is not None:
                shutil.rmtree(temporary, ignore_errors=True)
            raise


def _complete_relocation(source: Path, destination: Path, state: dict, manifest: dict) -> dict:
    _verify_published(destination, state)
    _verify_source(source, manifest, state["source_database_logical_sha256"])
    retired = {"format": FORMAT, "migration_id": state["id"], "destination_root": str(destination),
               "backup_root": state["backup_root"], "backup_manifest_sha256": state["backup_manifest_sha256"]}
    fs = ManagedFS(source)
    if (source / RETIRED).exists():
        if json.loads(fs.read_bytes(RETIRED)) != retired:
            raise ValueError("The original instance has a different retirement record")
    else:
        fs.write_bytes(RETIRED, _canonical(retired), replace=False)
    state = {**state, "state": "complete", "completed_at": time.time()}
    ManagedFS(destination, max_bytes=32 * 1024 * 1024).write_bytes(STATE, _canonical(state))
    return {"relocated": True, "source_root": str(source), "destination_root": str(destination),
            "backup_root": state["backup_root"], "migration_id": state["id"],
            "historical_probes": state["historical_probes"], "records_preserved": state["records_preserved"],
            "records_updated": state["records_updated"], "game_launched": False,
            **({"previous_source_root": state["previous_source_root"]} if "previous_source_root" in state else {})}


def recover_relocation(destination: Path) -> dict:
    """Finish only the exact verified published relocation; never rerun side effects."""
    destination = reject_link_components(Path(destination).absolute())
    state = json.loads(backups.read_source_bytes(destination, STATE, max_bytes=32 * 1024 * 1024))
    if state.get("format") != FORMAT or state.get("destination_root") != str(destination):
        raise ValueError("Unrecognized relocation recovery record")
    if state.get("state") == "complete":
        validate_state_ready(destination)
        return {"relocated": True, "destination_root": str(destination), "already_complete": True, "game_launched": False}
    source = reject_link_components(Path(state["source_root"]))
    backup_root = reject_link_components(Path(state["backup_root"]))
    manifest, checksum = backups._manifest(backup_root)
    if checksum != state["backup_manifest_sha256"] or manifest["source_root"] != str(source):
        raise ValueError("The relocation backup or provenance changed")
    backups._verify(backup_root, manifest)
    with ManagedLock(source / "coordinator.lock"), ManagedLock(destination / "coordinator.lock"):
        return _complete_relocation(source, destination, state, manifest)


def _restore_journals(backup: Path, manifest: dict) -> None:
    """Historical jobs/probes may be interrupted; external write recovery may not be replayed."""
    with _database(backup) as connection:
        rows = connection.execute("SELECT kind,payload FROM records WHERE kind IN ('lab_journal','deployment','lab_deployment')")
        for kind, encoded in rows:
            record = json.loads(encoded)
            if ((kind == "lab_journal" and record.get("state") != "committed") or
                    (kind != "lab_journal" and record.get("state") in {
                        "starting", "publishing", "activating", "recovery_required"})):
                raise ValueError("The backup contains unresolved external-write recovery; no restore was published")
    for row in manifest["files"]:
        name = row["path"]
        if "/journals/" not in "/" + name and not name.endswith(".install.json"):
            continue
        value = json.loads(backups.read_source_bytes(backup, name, max_bytes=4 * 1024 * 1024))
        if value.get("state", value.get("phase")) not in FINAL_JOURNALS:
            raise ValueError("The backup contains unresolved external-write journals; no restore was published")


def _fork_instance(root: Path, restore_id: str) -> dict:
    """Re-own copied read history; immutable plans keep their former instance binding."""
    from toolkit_core.storage import Store, uid
    store = Store(root)
    counts = {"ownership_reassigned": 0, "interrupted_runs": 0}
    try:
        try:
            previous = store.get("configuration", "lab-instance")
        except KeyError:
            previous = {"id": "lab-instance"}
        old_instance = previous.get("instance_id")
        new_instance = uid("instance")
        with store.atomic():
            store.put("configuration", {**previous, "instance_id": new_instance,
                      "origin_instance_id": old_instance, "restore_id": restore_id},
                      expected_revision=previous.get("revision", 0))
            for kind in ("lab_artifact", "job", "run", "lab_run"):
                for record in store.iter_records(kind):
                    record.setdefault("origin_instance_id", record.get("instance_id", old_instance))
                    record.update(instance_id=new_instance, restore_id=restore_id)
                    if kind != "lab_artifact" and record.get("state") in ACTIVE_RUNS:
                        record.setdefault("original_state", record["state"])
                        record.update(state="interrupted", phase="interrupted",
                            error="Backup restored as an independent instance; interrupted work was not resumed")
                        counts["interrupted_runs"] += 1
                    store.put(kind, record, expected_revision=record["revision"])
                    counts["ownership_reassigned"] += 1
        return {"source_instance_id": old_instance, "instance_id": new_instance, **counts}
    finally:
        store.close()


def _restore_inventory(root: Path):
    rows = [row for row in _file_rows(root) if row["path"] != RESTORE_STATE]
    rows.append(backups.hash_source_file(root, backups.DATABASE, max_bytes=backups.MAX_FILE_BYTES))
    return sorted(rows, key=lambda row: row["path"])


def _verify_restored(root: Path, state: dict) -> None:
    if _restore_inventory(root) != state["published_files"]:
        raise ValueError("Restored files changed before recovery; preserve the copy for review")
    with _database(root) as connection:
        row = connection.execute("SELECT payload FROM records WHERE kind='configuration' AND id='lab-instance'").fetchone()
        if row is None or json.loads(row[0]).get("instance_id") != state["instance_id"]:
            raise ValueError("The restored instance identity differs from its verified restore plan")


def _complete_restore(destination: Path, state: dict) -> dict:
    _verify_restored(destination, state)
    complete = {**state, "state": "complete", "completed_at": time.time()}
    ManagedFS(destination, max_bytes=32 * 1024 * 1024).write_bytes(RESTORE_STATE, _canonical(complete))
    return {"restored": True, "destination_root": str(destination), "backup_root": state["backup_root"],
            "restore_id": state["id"], "instance_id": state["instance_id"],
            "source_instance_id": state["source_instance_id"], "historical_probes": state["historical_probes"],
            "interrupted_runs": state["interrupted_runs"], "authority_rotated": True, "game_launched": False}


def restore_state(source: Path, destination: Path, *, fault_at=None) -> dict:
    """Restore a verified backup as a separate instance without retiring its source.

    Saved investigations remain readable. Restored plans, questions and process
    handles are historical; a fork cannot inherit authority to act on its parent.
    """
    source = reject_link_components(Path(source).absolute())
    destination = reject_link_components(Path(destination).absolute())
    if paths_overlap(source, destination):
        raise ValueError("Backup and restore destination must be separate")
    manifest, checksum = backups._manifest(source)
    original_root = reject_link_components(Path(manifest["source_root"]).absolute())
    if paths_overlap(original_root, destination):
        raise ValueError("Restore must create an independent root outside the original instance")
    before, directories = backups._verify(source, manifest)
    _restore_journals(source, manifest)
    target, temporary, existing = backups._prepare(destination)
    published = False
    restore_id = "restore_" + uuid.uuid4().hex
    output = ManagedFS(temporary, max_bytes=backups.MAX_FILE_BYTES)
    try:
        for directory in directories:
            output.resolve(directory).mkdir(parents=True, exist_ok=True)
        for row in manifest["files"]:
            backups._copy(source, row["path"], output, row)
        files = {row["path"] for row in manifest["files"]}
        counts = _transform(temporary / backups.DATABASE, original_root, target, files, restore_id)
        identity = _fork_instance(temporary, restore_id)
        if "agent/connector-key" in files:
            output.write_bytes("agent/connector-key", secrets.token_bytes(32))
        if "agent/engine.json" in files:
            output.resolve("agent/engine.json").unlink()
        # Earlier root bindings are operational metadata, never an authority to
        # operate the restored instance. Archive their exact bytes separately.
        archived = []
        for name in (STATE, RETIRED, RESTORE_STATE):
            if name in files:
                raw = output.read_bytes(name)
                archive_path = "history/" + restore_id + "/" + name
                output.write_bytes(archive_path, raw, replace=False)
                output.resolve(name).unlink()
                archived.append({"original_path": name, "archive_path": archive_path, "sha256": _hash(raw)})
        state = {"format": RESTORE_FORMAT, "id": restore_id, "state": "recovery_required",
                 "backup_root": str(source), "backup_manifest_sha256": checksum,
                 "original_root": str(original_root), "destination_root": str(target),
                 "created_at": time.time(), "authority_rotated": True,
                 "archived_operational_metadata": archived,
                 "published_files": _restore_inventory(temporary), **counts, **identity}
        if len(_canonical(state)) > 32 * 1024 * 1024:
            raise ValueError("Restore metadata exceeds its explicit 32 MiB verification budget")
        output.write_bytes(RESTORE_STATE, _canonical(state), replace=False)
        after, after_directories = backups._verify(source, manifest)
        if before != after or directories != after_directories:
            raise ValueError("The backup changed during restore")
        if fault_at == "before_publish":
            raise RuntimeError("Injected restore interruption before publication")
        backups._publish(temporary, target, existing)
        published = True
        if fault_at == "after_publish":
            raise RuntimeError("Injected restore interruption after publication; run explicit restore recovery")
        return _complete_restore(target, state)
    except Exception:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def recover_restore(destination: Path) -> dict:
    """Finish only the exact verified restore publication; never replay parent actions."""
    destination = reject_link_components(Path(destination).absolute())
    state = json.loads(backups.read_source_bytes(destination, RESTORE_STATE, max_bytes=32 * 1024 * 1024))
    if state.get("format") != RESTORE_FORMAT or state.get("destination_root") != str(destination):
        raise ValueError("Unrecognized restore recovery record")
    if state.get("state") == "complete":
        validate_state_ready(destination)
        return {"restored": True, "destination_root": str(destination), "already_complete": True, "game_launched": False}
    source = reject_link_components(Path(state["backup_root"]))
    manifest, checksum = backups._manifest(source)
    if checksum != state["backup_manifest_sha256"] or manifest["source_root"] != state["original_root"]:
        raise ValueError("The restore backup provenance changed")
    backups._verify(source, manifest)
    _restore_journals(source, manifest)
    with ManagedLock(destination / "coordinator.lock"):
        return _complete_restore(destination, state)
