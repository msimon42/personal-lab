"""Stable identifiers, data-root layout, SQLite connections and the immutable archive."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Iterator

from . import SCHEMA_VERSION

DATA_DIR_ENV = "ELECTION_DATA_DIR"
SUBDIRECTORIES = ("config", "archive/sha256", "manifests", "staging", "db", "assets", "reports",
                  "exports", "backups", "logs")


class ElectionDataError(Exception):
    """Base error; ``exit_code`` follows the CLI contract in spec section 11."""
    exit_code = 6
    code = "OPERATIONAL_FAILURE"


class InvalidInput(ElectionDataError):
    exit_code, code = 2, "INVALID_INPUT"


class AcquisitionFailure(ElectionDataError):
    exit_code, code = 3, "ACQUISITION_FAILED"


class ValidationFailure(ElectionDataError):
    exit_code, code = 4, "VALIDATION_FAILED"


class ReviewRequired(ElectionDataError):
    exit_code, code = 5, "REVIEW_REQUIRED"


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    """Deterministic serialization used for every semantic hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_id(prefix: str, *parts: object) -> str:
    """Namespaced deterministic identifier from a canonical natural key.

    Display names are never the only input: callers pass normalized natural keys
    (dates, FIPS codes, office codes, source row keys). Spelling corrections are
    handled by aliases, so a published ID does not change when a label does.
    """
    key = canonical_json([prefix, *[str(part) for part in parts]])
    return f"{prefix}_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"


def default_data_root() -> Path:
    """OS application-data directory; no particular machine or path is assumed."""
    if configured := os.environ.get(DATA_DIR_ENV):
        return Path(configured)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "personal-lab/election-data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/personal-lab/election-data"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "personal-lab/election-data"


@dataclass(frozen=True)
class DataRoot:
    path: Path

    @classmethod
    def open(cls, path: Path | None = None, *, create: bool = False) -> "DataRoot":
        root = cls(Path(path).absolute() if path else default_data_root().absolute())
        if create:
            for name in SUBDIRECTORIES:
                (root.path / name).mkdir(parents=True, exist_ok=True)
        elif not root.database.exists():
            raise InvalidInput(f"No election data root at {root.path}; run 'election-data init' first.")
        return root

    @property
    def database(self) -> Path:
        return self.path / "db" / "elections.sqlite"

    def archive_path(self, sha256: str) -> Path:
        return self.path / "archive" / "sha256" / sha256[:2] / sha256

    def relative(self, path: Path) -> str:
        return path.relative_to(self.path).as_posix()

    def resolve(self, relative: str) -> Path:
        candidate = (self.path / relative).resolve()
        if not candidate.is_relative_to(self.path.resolve()):
            raise InvalidInput("Stored path escapes the data root.")
        return candidate


def connect(database: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Connection with the spec's pragmas; readers use separate read-only handles."""
    if read_only:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=5,
                                     isolation_level=None)
    else:
        connection = sqlite3.connect(database, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Short explicit write transaction; never held across network I/O."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def migrations() -> list[tuple[int, str, str]]:
    found = []
    for entry in resources.files("election_data.migrations").iterdir():
        if entry.name.endswith(".sql"):
            found.append((int(entry.name.split("_", 1)[0]), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found)


def migrate(connection: sqlite3.Connection) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migration'").fetchone()
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migration")} if exists else set()
    for version, name, script in migrations():
        if version in applied:
            continue
        # executescript commits implicitly; wrap the whole script in its own transaction.
        connection.executescript(f"BEGIN IMMEDIATE;\n{script}\n"
                                 f"INSERT INTO schema_migration VALUES ({version}, '{name}', '{utc_now()}');\nCOMMIT;")
    current = connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0]
    if current != SCHEMA_VERSION:
        raise ElectionDataError(f"Database schema {current} does not match code schema {SCHEMA_VERSION}.")
    return current


@contextlib.contextmanager
def writer_lock(root: DataRoot) -> Iterator[None]:
    """One writer process per data root; readers continue against published records."""
    lock = root.path / "db" / "writer.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise ElectionDataError(
            f"Another writer holds {lock}. If no election-data process is running, remove it.") from error
    try:
        os.write(descriptor, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)


def archive_bytes(root: DataRoot, connection: sqlite3.Connection, payload_path: Path, *,
                  format: str, mime_type: str | None, original_filename: str | None) -> tuple[str, bool]:
    """Move a fully written temporary file into the content-addressed archive.

    Returns ``(artifact_id, created)``. Archived bytes are never overwritten; a
    matching hash reuses the existing artifact.
    """
    digest = hashlib.sha256()
    length = 0
    with payload_path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            length += len(block)
    sha256 = digest.hexdigest()
    artifact_id = f"artifact_{sha256[:24]}"
    existing = connection.execute("SELECT artifact_id FROM source_artifact WHERE sha256 = ?", (sha256,)).fetchone()
    destination = root.archive_path(sha256)
    if existing:
        payload_path.unlink(missing_ok=True)
        if not destination.exists():
            raise ElectionDataError(f"Archive is missing bytes for recorded artifact {artifact_id}.")
        return existing[0], False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        payload_path.unlink(missing_ok=True)
    else:
        os.replace(payload_path, destination)
        destination.chmod(0o444)
    with transaction(connection):
        connection.execute(
            "INSERT INTO source_artifact VALUES (?,?,?,?,?,?,?,?)",
            (artifact_id, sha256, root.relative(destination), length, mime_type, format, original_filename, utc_now()))
    return artifact_id, True


def staging_file(root: DataRoot) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="download-", dir=root.path / "staging")
    os.close(descriptor)
    return Path(name)


def verify_artifact(root: DataRoot, connection: sqlite3.Connection, artifact_id: str) -> Path:
    row = connection.execute("SELECT sha256, archive_path, byte_length FROM source_artifact WHERE artifact_id = ?",
                             (artifact_id,)).fetchone()
    if row is None:
        raise InvalidInput(f"Unknown artifact {artifact_id}.")
    path = root.resolve(row["archive_path"])
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != row["sha256"]:
        raise ElectionDataError(f"Archived bytes for {artifact_id} do not match their recorded hash.")
    return path
