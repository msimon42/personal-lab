"""Complete immutable runtime backups and guarded restore into a fresh directory.

The caller serializes application mutations and holds Store.lock when supplying a
live connection. No coordinator lock is acquired recursively. Before/after byte
inventories and SQLite change tokens also detect external changes. A backup is
published only after all copies are checked; restore never activates a playset.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import uuid

from .safety import (ManagedFS, hash_source_file, read_source_bytes,
                     reject_link_components, validate_relative_path,
                     check_path_collisions)

MANIFEST = "backup-manifest.json"
DATABASE = "workspace.sqlite3"
MAX_FILE_BYTES = 2 ** 42
MAX_FILES = 200_000
HASH = re.compile(r"^[a-f0-9]{64}$")


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _excluded(path: str):
    parts = path.split("/")
    name = parts[-1].casefold()
    return (parts[0] == "backups" or ".locks" in parts or name.endswith((".lock", "-wal", "-shm", "-journal"))
            or name in {"session-token", ".session-token", "session-token.json"}
            or path in {DATABASE, MANIFEST})


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _root(path: Path) -> Path:
    selected = reject_link_components(Path(path))
    if not selected.is_dir():
        raise ValueError("backup source must be an existing real directory")
    return selected.resolve()


def _paths(root: Path, *, archive=False):
    files, directories = [], []

    def inspect(entries, prefix, open_child):
        for entry in sorted(entries, key=lambda e: e.name):
            relative = prefix + entry.name
            if relative == MANIFEST or (not archive and _excluded(relative)):
                continue
            relative = validate_relative_path(relative)
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("backup trees cannot contain links/reparse points")
            if stat.S_ISDIR(info.st_mode):
                directories.append(relative)
                open_child(entry.name, relative + "/")
            elif stat.S_ISREG(info.st_mode):
                files.append(relative)
                if len(files) > MAX_FILES:
                    raise ValueError("backup file budget exceeded")
            else:
                raise ValueError("backup trees contain an unsupported nonregular file")

    if os.name != "nt":
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        def walk(descriptor, prefix):
            with os.scandir(descriptor) as entries:
                def child(name, next_prefix):
                    handle = os.open(name, flags, dir_fd=descriptor)
                    try:
                        walk(handle, next_prefix)
                    finally:
                        os.close(handle)
                inspect(entries, prefix, child)
        handle = os.open(root, flags)
        try:
            walk(handle, "")
        finally:
            os.close(handle)
    else:
        def walk(directory, prefix):
            reject_link_components(directory)
            with os.scandir(directory) as entries:
                inspect(entries, prefix, lambda name, next_prefix: walk(directory / name, next_prefix))
        walk(root, "")
    check_path_collisions(files + directories)
    return sorted(files), sorted(directories)


def _inventory(root: Path, *, archive=False):
    names, directories = _paths(root, archive=archive)
    rows = []
    for name in names:
        record = hash_source_file(root, name, max_bytes=MAX_FILE_BYTES)
        # Content-addressed source bytes are not allowed to acquire a new identity silently.
        parts = name.split("/")
        if len(parts) == 4 and parts[:2] == ["blobs", "sha256"]:
            if not HASH.fullmatch(parts[-1]) or parts[-2] != parts[-1][:2] or record["sha256"] != parts[-1]:
                raise ValueError("content-addressed blob integrity failure")
        info = (root / name).lstat()
        record["_identity"] = _identity(info)
        rows.append(record)
    return rows, directories


@contextmanager
def _opened_source(root: Path, path: str):
    path = validate_relative_path(path)
    if os.name == "nt":
        from .safety import _windows_source_descriptor
        descriptor = _windows_source_descriptor(root, path)
    else:
        reader = ManagedFS.__new__(ManagedFS)
        reader.root, reader.sources, reader.max_bytes = root, [], MAX_FILE_BYTES
        info = root.stat()
        reader._identity = (info.st_dev, info.st_ino)
        with reader._parent(path) as (parent, name):
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def _copy(root: Path, path: str, destination: ManagedFS, expected=None):
    path = validate_relative_path(path)
    with _opened_source(root, path) as source, destination._parent(path, create=True) as (parent, name):
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise ValueError("backup source is not a bounded regular file")
        options = {} if parent == -1 else {"dir_fd": parent}
        target = destination.root / name if parent == -1 else name
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, **options)
        checksum, size = hashlib.sha256(), 0
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("backup file budget exceeded")
                    checksum.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(descriptor)
            after = os.fstat(source.fileno())
            if _identity(before) != _identity(after) or size != before.st_size:
                raise ValueError("source changed while copying backup")
        finally:
            os.close(descriptor)
    record = {"path": path, "size": size, "sha256": checksum.hexdigest()}
    if expected and any(record[key] != expected[key] for key in ("path", "size", "sha256")):
        raise ValueError("source bytes changed between backup inventory and copy")
    return record


def _prepare(destination: Path):
    target = reject_link_components(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_link_components(target)
    existing = None
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise FileExistsError("backup/restore destination must be new or empty")
        existing = (target.stat().st_dev, target.stat().st_ino)
    temporary = target.parent / ("." + target.name + ".partial-" + uuid.uuid4().hex)
    temporary.mkdir(mode=0o700)
    return target, temporary, existing


def _publish(temporary: Path, target: Path, existing):
    reject_link_components(target)
    if target.exists():
        actual = target.stat()
        if existing != (actual.st_dev, actual.st_ino) or not target.is_dir() or any(target.iterdir()):
            raise FileExistsError("destination changed during backup/restore")
        target.rmdir()
    elif existing is not None:
        raise ValueError("destination changed during backup/restore")
    os.rename(temporary, target)
    if os.name != "nt":
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _db_token(connection):
    if connection.in_transaction:
        raise ValueError("backup requires a committed SQLite transaction boundary")
    return (connection.total_changes, connection.execute("PRAGMA data_version").fetchone()[0],
            connection.execute("PRAGMA schema_version").fetchone()[0])


def _db_input_inventory(root):
    rows = []
    for name in (DATABASE, DATABASE + "-wal", DATABASE + "-journal"):
        path = root / name
        if path.exists() or path.is_symlink():
            row = hash_source_file(root, name, max_bytes=MAX_FILE_BYTES)
            row["_identity"] = _identity(path.lstat())
            rows.append(row)
    if not rows or rows[0]["path"] != DATABASE:
        raise ValueError("runtime has no SQLite database to back up")
    return rows


def _consistent_database(root, temporary, connection):
    destination = temporary / DATABASE
    owned = None
    if connection is None:
        # Read through no-follow capabilities, then let SQLite recover only a private copy.
        # Stable main/WAL/journal fingerprints are checked again before publication.
        seed = ManagedFS(temporary / ".sqlite-input", max_bytes=MAX_FILE_BYTES)
        before = _db_input_inventory(root)
        for record in before:
            _copy(root, record["path"], seed, record)
        owned = sqlite3.connect(seed.root / DATABASE)
        connection = owned
    else:
        paths = [Path(row[2]).resolve() for row in connection.execute("PRAGMA database_list") if row[1] == "main"]
        if paths != [root / DATABASE]:
            raise ValueError("SQLite connection belongs to another runtime")
        before = _db_token(connection)
    output = sqlite3.connect(destination)
    try:
        connection.backup(output)
        output.execute("PRAGMA journal_mode=DELETE")
        if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("SQLite backup failed integrity check")
    finally:
        output.close()
        if owned:
            owned.close()
    if owned:
        shutil.rmtree(temporary / ".sqlite-input")
    return before


def _manifest(source):
    raw = read_source_bytes(source, MANIFEST, max_bytes=32 * 1024 * 1024)
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("schema_version") != 2 or value.get("kind") != "runtime_backup":
        raise ValueError("unsupported backup manifest")
    if value.get("database") != DATABASE or not isinstance(value.get("files"), list) or not isinstance(value.get("directories"), list):
        raise ValueError("invalid backup manifest")
    if len(value["files"]) > MAX_FILES:
        raise ValueError("backup manifest exceeds file budget")
    paths = []
    for row in value["files"]:
        if not isinstance(row, dict) or set(row) != {"path", "size", "sha256"}:
            raise ValueError("invalid backup file record")
        path = validate_relative_path(row["path"])
        if path != DATABASE and _excluded(path):
            raise ValueError("backup includes excluded credentials/lock/control paths")
        if type(row["size"]) is not int or not 0 <= row["size"] <= MAX_FILE_BYTES or not isinstance(row["sha256"], str) or not HASH.fullmatch(row["sha256"]):
            raise ValueError("invalid backup size/hash")
        paths.append(path)
    if paths.count(DATABASE) != 1:
        raise ValueError("backup must include one complete SQLite database")
    for directory in value["directories"]:
        validate_relative_path(directory)
        if _excluded(directory):
            raise ValueError("backup includes excluded directory")
    check_path_collisions(paths + value["directories"])
    return value, _digest(raw)


def _verify(source, manifest):
    actual, directories = _inventory(source, archive=True)
    expected = {row["path"]: row for row in manifest["files"]}
    if set(expected) != {row["path"] for row in actual} or set(directories) != set(manifest["directories"]):
        raise ValueError("backup inventory differs from complete manifest")
    for row in actual:
        if any(row[key] != expected[row["path"]][key] for key in ("size", "sha256")):
            raise ValueError("backup bytes failed hash/size verification")
    return actual, directories


def _stale_and_rebase(database, old_root, new_root, files, *, rebase):
    """Only artifact records gain relocated paths. Original-source paths stay exact."""
    connection = sqlite3.connect(database)
    counts = {"stale_records": 0, "rebased_artifacts": 0, "interrupted_jobs": 0}
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "records" not in tables:
            return counts
        rows = connection.execute("SELECT id,kind,payload,revision FROM records").fetchall()
        for identifier, kind, encoded, revision in rows:
            payload = json.loads(encoded)
            changed = False
            if kind == "artifact" and rebase and isinstance(payload.get("path"), str):
                old = Path(payload["path"])
                if old.is_absolute():
                    try:
                        relative = old.relative_to(Path(old_root)).as_posix()
                    except ValueError:
                        relative = None
                    if relative and relative in files and (relative.startswith("artifacts/") or relative.startswith("managed/")):
                        payload["path"] = str(new_root / relative)
                        counts["rebased_artifacts"] += 1
                        changed = True
            if kind == "candidate":
                payload.update(status="stale", stale_reason="Restored runtime requires source/decision revalidation")
                if isinstance(payload.get("validation"), dict):
                    payload["validation"].update(experimental_eligible=False, validated_promotion_eligible=False)
                changed = True
            elif kind == "decision":
                payload["state"] = "stale"
                changed = True
            elif kind == "workspace":
                payload.update(status="needs_rescan", generation=None)
                changed = True
            elif kind == "job" and payload.get("state") in {"queued", "running", "awaiting_user"}:
                payload.update(state="failed", error="Runtime restored; interrupted job must be restarted")
                counts["interrupted_jobs"] += 1
                changed = True
            if changed:
                payload["revision"] = revision + 1
                payload["restored_requires_revalidation"] = True
                connection.execute("UPDATE records SET payload=?,revision=? WHERE id=?", (_canonical(payload).decode(), revision + 1, identifier))
                counts["stale_records"] += kind in {"candidate", "decision", "workspace"}
        # Cached responses reference historical job/generation identities; callers must resubmit.
        if "requests" in tables:
            connection.execute("DELETE FROM requests")
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("restored SQLite failed integrity check")
    finally:
        connection.close()
    return counts


class BackupManager:
    def __init__(self, root: Path, store=None):
        self.root = _root(root)
        self.store = store

    def create(self, destination: Path, connection=None) -> dict:
        selected = reject_link_components(destination).resolve()
        if selected == self.root or selected in self.root.parents or (self.root in selected.parents and not selected.is_relative_to(self.root / "backups")):
            raise ValueError("backup destination must not overlap runtime data except its backups directory")
        if selected == self.root / "backups":
            raise ValueError("choose a new named backup below the backups directory")
        lock = self.store.lock if self.store is not None else nullcontext()
        connection = connection or (self.store.connection if self.store is not None else None)
        with lock:
            target, temporary, existing = _prepare(selected)
            try:
                root_identity = (self.root.stat().st_dev, self.root.stat().st_ino)
                before, directories = _inventory(self.root)
                database_token = _consistent_database(self.root, temporary, connection)
                output = ManagedFS(temporary, max_bytes=MAX_FILE_BYTES)
                for directory in directories:
                    output.resolve(directory).mkdir(parents=True, exist_ok=True)
                copied = [_copy(self.root, row["path"], output, row) for row in before]
                after, after_directories = _inventory(self.root)
                after_database = _db_token(connection) if connection is not None else _db_input_inventory(self.root)
                if before != after or directories != after_directories or database_token != after_database or root_identity != (self.root.stat().st_dev, self.root.stat().st_ino):
                    raise ValueError("runtime changed during complete backup; retry while writes are paused")
                database = hash_source_file(temporary, DATABASE, max_bytes=MAX_FILE_BYTES)
                copied.append(database)
                manifest = {"schema_version": 2, "kind": "runtime_backup", "created_at": datetime.now(timezone.utc).isoformat(),
                            "source_root": str(self.root), "database": DATABASE, "files": sorted(copied, key=lambda r: r["path"]),
                            "directories": directories, "exclusions": ["backups", "locks", "session capability", "SQLite sidecars"]}
                output.write_bytes(MANIFEST, _canonical(manifest), replace=False)
                _verify(temporary, manifest)
                _publish(temporary, target, existing)
                return {"path": str(target), "manifest": manifest, "manifest_sha256": _digest(_canonical(manifest)),
                        "files": len(copied), "bytes": sum(row["size"] for row in copied), "consistent": True}
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

    @staticmethod
    def restore(source: Path, destination: Path, *, rebase_managed_paths=True) -> dict:
        source = _root(source)
        destination = reject_link_components(destination).resolve()
        if source == destination or source in destination.parents or destination in source.parents:
            raise ValueError("restore source and destination must not overlap")
        manifest, manifest_hash = _manifest(source)
        before, directories = _verify(source, manifest)
        target, temporary, existing = _prepare(destination)
        try:
            output = ManagedFS(temporary, max_bytes=MAX_FILE_BYTES)
            for directory in directories:
                output.resolve(directory).mkdir(parents=True, exist_ok=True)
            for row in manifest["files"]:
                _copy(source, row["path"], output, row)
            after, after_directories = _verify(source, manifest)
            if before != after or directories != after_directories:
                raise ValueError("backup changed during restore")
            counts = _stale_and_rebase(temporary / DATABASE, manifest.get("source_root", ""), target,
                                       {row["path"] for row in manifest["files"]}, rebase=rebase_managed_paths)
            pending = []
            for row in manifest["files"]:
                if "/journals/" in row["path"] or row["path"].startswith("journals/"):
                    try:
                        journal = json.loads(output.read_bytes(row["path"]))
                    except (ValueError, UnicodeDecodeError):
                        pending.append(row["path"])
                        continue
                    if journal.get("state") not in {"verified", "rolled_back"}:
                        pending.append(row["path"])
            state = {"schema_version": 1, "kind": "restored_runtime", "source_manifest_sha256": manifest_hash,
                     "source_root": manifest.get("source_root"), "restored_root": str(target),
                     "requires_rescan": True, "activation_verified": False,
                     "recovery_required": bool(pending), "pending_journals": pending,
                     "recovery_action": "Run managed journal recovery, then rescan/revalidate before using releases" if pending else "Rescan and revalidate before using releases",
                     **counts}
            output.write_bytes("restore-state.json", _canonical(state))
            _publish(temporary, target, existing)
            return {"path": str(target), "files": len(manifest["files"]), "stale": True, **state}
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
