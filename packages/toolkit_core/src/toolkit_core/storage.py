"""Small transactional index; immutable bytes and large artifacts remain files."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import uuid
from typing import Any

from toolkit_core.safety import ManagedFS, reject_link_components, iter_source_file, hash_source_file


class Conflict(ValueError):
    pass


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class Store:
    def __init__(self, root: Path):
        selected = reject_link_components(root)
        self.fs = ManagedFS(selected, max_bytes=128 * 1024 * 1024)
        self.root = self.fs.root
        reject_link_components(self.root)
        self.db_path = self.root / "workspace.sqlite3"
        self._check_reserved_paths()
        self.lock = threading.RLock()
        self._transaction_depth = 0
        # SQLite's writable file is created in the private managed tree before
        # connecting. It cannot be a link/hardlink to an original source file.
        with self.fs._parent("workspace.sqlite3") as (parent, name):
            options = {} if parent == -1 else {"dir_fd": parent}
            descriptor = os.open(self.root / name if parent == -1 else name,
                                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, **options)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("managed SQLite file must be an unshared regular file")
                self._db_identity = (info.st_dev, info.st_ino)
                self._check_database()
                self.connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
                self._check_database()
            finally:
                os.close(descriptor)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
          CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS records(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, workspace_id TEXT,
            revision INTEGER NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
          );
          CREATE INDEX IF NOT EXISTS records_kind_workspace ON records(kind,workspace_id,created);
          CREATE TABLE IF NOT EXISTS events(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            payload TEXT NOT NULL, created REAL NOT NULL
          );
          CREATE INDEX IF NOT EXISTS events_job ON events(job_id,sequence);
          CREATE TABLE IF NOT EXISTS requests(
            key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response TEXT NOT NULL
          );
        """)
        self.connection.execute("INSERT OR IGNORE INTO migrations VALUES (1,?)", (time.time(),))
        self.connection.commit()

    def _check_reserved_paths(self) -> None:
        reject_link_components(self.root)
        for name in ("workspace.sqlite3", "workspace.sqlite3-wal", "workspace.sqlite3-shm",
                     "session-token", "coordinator.lock", "blobs", "backups", "managed",
                     "artifacts", "runtime", "evidence-registry"):
            reject_link_components(self.root / name)
            path = self.root / name
            if path.exists():
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise ValueError("reserved managed state requires an unshared inode; hardlinks are forbidden")

    def _check_database(self) -> None:
        self._check_reserved_paths()
        info = self.db_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (info.st_dev, info.st_ino) != self._db_identity):
            raise Conflict("Managed SQLite identity changed; refuse to write or follow another file")

    def put(self, kind: str, data: dict, expected_revision: int | None = None) -> dict:
        with self.lock:
            self._check_database()
            record = dict(data)
            record.setdefault("id", uid(kind))
            old = self.connection.execute("SELECT * FROM records WHERE id=?", (record["id"],)).fetchone()
            if old and old["kind"] != kind:
                raise Conflict("Record kind cannot change")
            if expected_revision is not None and (old["revision"] if old else 0) != expected_revision:
                raise Conflict("Record changed; refresh and reanalyze before retrying")
            record["revision"] = (old["revision"] if old else 0) + 1
            record.setdefault("created_at", old["created"] if old else time.time())
            record["updated_at"] = time.time()
            self.connection.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "revision=excluded.revision,payload=excluded.payload,updated=excluded.updated",
                (record["id"], kind, record.get("workspace_id"), record["revision"],
                 canonical(record).decode(), record["created_at"], record["updated_at"]),
            )
            if not self._transaction_depth:
                self.connection.commit()
            return record

    @contextmanager
    def atomic(self):
        with self.lock:
            self._check_database()
            outermost = self._transaction_depth == 0
            if outermost:
                self.connection.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
                if outermost:
                    self.connection.commit()
            except Exception:
                if outermost:
                    self.connection.rollback()
                raise
            finally:
                self._transaction_depth -= 1

    def get(self, kind: str, identifier: str) -> dict:
        with self.lock:
            self._check_database()
            row = self.connection.execute("SELECT payload FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        if not row:
            raise KeyError(f"Unknown {kind}: {identifier}")
        return json.loads(row["payload"])

    def record_header(self, kind: str, identifier: str) -> dict:
        """Read immutable identity/revision guards without decoding large payloads."""
        with self.lock:
            self._check_database()
            row = self.connection.execute("SELECT id,kind,workspace_id,revision FROM records WHERE kind=? AND id=?",
                                          (kind, identifier)).fetchone()
        if row is None:
            raise KeyError(f"Unknown {kind}: {identifier}")
        return dict(row)

    def list(self, kind: str, workspace_id: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        query = "SELECT payload FROM records WHERE kind=?"
        parameters: list[Any] = [kind]
        if workspace_id is not None:
            query += " AND workspace_id=?"
            parameters.append(workspace_id)
        query += " ORDER BY created DESC LIMIT ? OFFSET ?"
        parameters.extend([min(max(limit, 1), 1000), max(offset, 0)])
        with self.lock:
            self._check_database()
            return [json.loads(row[0]) for row in self.connection.execute(query, parameters)]

    def iter_records(self, kind: str, workspace_id: str | None = None):
        offset = 0
        while True:
            entries = self.list(kind, workspace_id, limit=1000, offset=offset)
            yield from entries
            if len(entries) < 1000:
                break
            offset += len(entries)

    def append_event(self, job_id: str, payload: dict) -> int:
        with self.lock:
            self._check_database()
            cursor = self.connection.execute("INSERT INTO events(job_id,payload,created) VALUES(?,?,?)", (job_id, canonical(payload).decode(), time.time()))
            if not self._transaction_depth:
                self.connection.commit()
            return int(cursor.lastrowid)

    def events(self, job_id: str, after: int = 0) -> list[dict]:
        with self.lock:
            self._check_database()
            rows = self.connection.execute("SELECT sequence,payload FROM events WHERE job_id=? AND sequence>? ORDER BY sequence LIMIT 500", (job_id, after)).fetchall()
        return [{"sequence": row[0], "data": json.loads(row[1])} for row in rows]

    def cached_request(self, key: str, request_hash: str) -> dict | None:
        with self.lock:
            self._check_database()
            row = self.connection.execute("SELECT * FROM requests WHERE key=?", (key,)).fetchone()
        if row and row["request_hash"] != request_hash:
            raise Conflict("Idempotency key was already used for a different command")
        return json.loads(row["response"]) if row else None

    def cache_request(self, key: str, request_hash: str, response: dict) -> None:
        with self.lock:
            self._check_database()
            self.connection.execute("INSERT INTO requests VALUES(?,?,?)", (key, request_hash, canonical(response).decode()))
            if not self._transaction_depth:
                self.connection.commit()

    def blob(self, raw: bytes) -> str:
        checksum = digest(raw)
        relative = f"blobs/sha256/{checksum[:2]}/{checksum}"
        with self.lock:
            self._check_database()
            target = self.fs.resolve(relative)
            if target.exists():
                if digest(self.fs.read_bytes(relative)) != checksum:
                    raise Conflict("Immutable blob integrity failure")
                return checksum
            try:
                self.fs.write_bytes(relative, raw, replace=False)
            except FileExistsError:
                if digest(self.fs.read_bytes(relative)) != checksum:
                    raise Conflict("Concurrent immutable blob integrity failure")
        return checksum

    def backup(self, destination: Path) -> Path:
        from toolkit_core.backups import BackupManager
        with self.lock:
            self._check_database()
            result = BackupManager(self.root).create(Path(destination), connection=self.connection)
            return Path(result["path"])

    def capture_source(self, source_root: Path, relative: str, *, checkpoint=None,
                       max_bytes: int = 2 ** 42, native_paths: bool = False) -> dict:
        """Stream an immutable source copy; large opaque payloads never enter JSON."""
        with self.lock:
            self._check_database()
        observed = hash_source_file(source_root, relative, max_bytes=max_bytes, native_paths=native_paths)
        final = f"blobs/sha256/{observed['sha256'][:2]}/{observed['sha256']}"
        with self.lock:
            self._check_database()
            present = self.fs.resolve(final).exists()
        if present:
            # A fresh full source hash selects the content address. Verify the
            # complete existing blob before reusing it; neither timestamps nor
            # an existing filename are evidence that its bytes are intact.
            actual = hash_source_file(self.root, final, max_bytes=max_bytes)
            if actual["sha256"] != observed["sha256"] or actual["size"] != observed["size"]:
                raise Conflict("Immutable streamed blob integrity failure")
            if checkpoint:
                checkpoint(observed["size"])
            return {"sha256": observed["sha256"], "size": observed["size"]}
        temporary = f"blobs/ingest/{uuid.uuid4().hex}.tmp"
        try:
            with self.fs._parent(temporary, create=True) as (parent, name):
                options = {} if parent == -1 else {"dir_fd": parent}
                target = self.root / name if parent == -1 else name
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, **options)
                checksum, size = hashlib.sha256(), 0
                with os.fdopen(descriptor, "wb") as output:
                    for chunk in iter_source_file(source_root, relative, max_bytes=max_bytes, native_paths=native_paths):
                        size += len(chunk)
                        checksum.update(chunk)
                        output.write(chunk)
                        if checkpoint:
                            checkpoint(size)
                    output.flush()
                    os.fsync(output.fileno())
            sha = checksum.hexdigest()
            if sha != observed["sha256"] or size != observed["size"]:
                raise Conflict("Source changed between hashing and capture")
            final = f"blobs/sha256/{sha[:2]}/{sha}"
            with self.lock:
                self._check_database()
                with self.fs._parent(temporary) as (source_parent, source_name), self.fs._parent(final, create=True) as (destination_parent, destination_name):
                    try:
                        if source_parent == -1:
                            os.rename(self.root / source_name, self.root / destination_name)
                        else:
                            os.link(source_name, destination_name, src_dir_fd=source_parent, dst_dir_fd=destination_parent, follow_symlinks=False)
                    except FileExistsError:
                        pass
                    if destination_parent != -1:
                        os.fsync(destination_parent)
            # Hash verification remains outside the database lock so large
            # assets do not stop API progress/cancellation queries.
            actual = hash_source_file(self.root, final, max_bytes=max_bytes)
            if actual["sha256"] != sha or actual["size"] != size:
                raise Conflict("Immutable streamed blob integrity failure")
            return {"sha256": sha, "size": size}
        finally:
            if self.fs.resolve(temporary).exists():
                self.fs.unlink(temporary)

    def close(self):
        with self.lock:
            self.connection.close()
