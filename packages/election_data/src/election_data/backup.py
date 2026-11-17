"""Consistent backups (SQLite backup API + immutable files) and verified restoration.

The machine-local ``config/local.toml`` (for example a guestbook identity) is not
copied; record it separately if it should travel with a backup.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .core import DataRoot, ElectionDataError, InvalidInput, connect, utc_now

MANIFEST = "backup-manifest.json"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _empty_destination(destination: Path) -> Path:
    destination = destination.absolute()
    if destination.exists() and any(destination.iterdir()):
        raise InvalidInput(f"Destination {destination} must be empty or absent.")
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def create_backup(root: DataRoot, destination: Path) -> dict[str, Any]:
    destination = _empty_destination(destination)
    if destination.is_relative_to(root.path) and not destination.is_relative_to(root.path / "backups"):
        raise InvalidInput("A backup inside the data root must be placed under backups/.")
    (destination / "db").mkdir()
    snapshot = destination / "db" / "elections.sqlite"
    with closing(connect(root.database, read_only=True)) as source, closing(sqlite3.connect(snapshot)) as target:
        source.backup(target)
    files = []
    with closing(sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True)) as connection:
        archived = [row[0] for row in connection.execute("SELECT archive_path FROM source_artifact ORDER BY archive_path")]
        assets = [row[0] for row in connection.execute(
            "SELECT path FROM release_asset JOIN data_release USING (release_id) "
            "WHERE status IN ('staged', 'published') ORDER BY path")]
        active = connection.execute("SELECT release_id FROM active_release").fetchone()
    for relative in sorted({*archived, *assets}):
        source_path = root.resolve(relative)
        target_path = destination / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
        files.append({"path": relative, "sha256": _hash(target_path), "bytes": target_path.stat().st_size})
    files.append({"path": "db/elections.sqlite", "sha256": _hash(snapshot), "bytes": snapshot.stat().st_size})
    manifest = {"kind": "election-data-backup", "schema_version": SCHEMA_VERSION, "created_at": utc_now(),
                "active_release_id": active[0] if active else None, "files": files,
                "excluded": ["config/local.toml (machine-local identity/settings)", "staging/", "logs/"]}
    (destination / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return {"destination": str(destination), "files": len(files), "active_release_id": manifest["active_release_id"],
            "bytes": sum(item["bytes"] for item in files)}


def verify_backup(location: Path) -> dict[str, Any]:
    """Restore to a temporary directory and check hashes, integrity, FKs, assets and queries."""
    location = Path(location)
    if location.name == MANIFEST:
        location = location.parent
    with tempfile.TemporaryDirectory(prefix="election-data-verify-") as temporary:
        restored = restore_backup(location, Path(temporary) / "root")
    return restored | {"verified": True}


def restore_backup(location: Path, destination: Path) -> dict[str, Any]:
    location = Path(location)
    manifest_path = location / MANIFEST
    if not manifest_path.exists():
        raise InvalidInput(f"{location} is not an election-data backup.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "election-data-backup":
        raise InvalidInput("Backup manifest has an unexpected kind.")
    destination = _empty_destination(destination)
    for item in manifest["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise InvalidInput(f"Unsafe path in backup manifest: {item['path']}")
        source = location / relative
        if not source.exists() or _hash(source) != item["sha256"]:
            raise ElectionDataError(f"Backup file {item['path']} is missing or corrupted.")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    root = DataRoot.open(destination, create=True)
    from .query import ElectionDataReader
    from .release import verify_assets

    with closing(connect(root.database)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ElectionDataError("Restored database failed integrity_check.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ElectionDataError("Restored database failed foreign_key_check.")
        active = connection.execute("SELECT release_id FROM active_release").fetchone()
        if active and (problems := verify_assets(root, connection, active[0])):
            raise ElectionDataError(f"Restored active release assets failed verification: {problems}")
    reader = ElectionDataReader(root.path)
    status = reader.status()
    sample = None
    if status.active_release_id:
        from .contracts import ContestsRequest, ResultsRequest
        contest = reader.contests(ContestsRequest(limit=1)).contests[0]
        units = reader.results(ResultsRequest(contest_id=contest.contest_id)).units  # the contest's own level
        if not units:
            raise ElectionDataError("Restored release returned no result for a published contest.")
        sample = units[0].label
    return {"restored_to": str(destination), "files": len(manifest["files"]),
            "active_release_id": status.active_release_id, "sample_query": sample}
