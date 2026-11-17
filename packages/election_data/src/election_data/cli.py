"""``election-data`` command line. Mutating acquisition, import and publication are CLI-only.

Exit codes: 0 success, 2 invalid input, 3 acquisition failure, 4 validation failure,
5 review required, 6 storage/operational failure.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from .core import DataRoot, ElectionDataError, InvalidInput, connect, migrate, writer_lock


def _open(arguments, *, create: bool = False) -> DataRoot:
    return DataRoot.open(arguments.data_dir, create=create)


def _emit(value: Any, as_json: bool) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            text = item if isinstance(item, (str, int, float, type(None))) else json.dumps(item, default=str)
            print(f"{key}: {text}")
    else:
        print(json.dumps(value, indent=2, default=str))


def command_init(arguments) -> dict:
    from .acquire import register_sources
    from .pipeline import ensure_reference, load_curation

    root = _open(arguments, create=True)
    with closing(connect(root.database)) as connection:
        version = migrate(connection)
        register_sources(connection)
        ensure_reference(connection, load_curation()[0])
    return {"data_dir": str(root.path), "schema_version": version,
            "local_config": str(root.path / "config" / "local.toml")}


def command_sources(arguments) -> list:
    from .acquire import load_manifest

    return [{"source_id": source["source_id"], "publisher": source["publisher"],
             "authority_kind": source["authority_kind"], "license_id": source["license_id"],
             "access_conditions": source["access_conditions"],
             "artifacts": [artifact["artifact_key"] for artifact in source["artifacts"]]}
            for source in load_manifest()["sources"]]


def _artifact_targets(arguments) -> list[tuple[str, str]]:
    from .acquire import load_manifest

    manifest = load_manifest()
    targets = []
    for source in manifest["sources"]:
        if arguments.source in (None, source["source_id"]):
            for artifact in source["artifacts"]:
                if getattr(arguments, "artifact", None) in (None, artifact["artifact_key"]):
                    targets.append((source["source_id"], artifact["artifact_key"]))
    if not targets:
        raise InvalidInput("No manifest artifact matches the requested source/artifact.")
    return targets


def command_fetch(arguments) -> list:
    from .acquire import fetch_artifact

    root = _open(arguments)
    results = []
    with writer_lock(root), closing(connect(root.database)) as connection:
        for source_id, artifact_key in _artifact_targets(arguments):
            results.append({"source_id": source_id, "artifact_key": artifact_key,
                            **fetch_artifact(root, connection, source_id, artifact_key)})
    return results


def command_archive_add(arguments) -> dict:
    from .acquire import add_manual_artifact

    root = _open(arguments)
    with writer_lock(root), closing(connect(root.database)) as connection:
        return add_manual_artifact(root, connection, arguments.source, arguments.artifact_key, Path(arguments.file))


def command_import(arguments) -> list:
    from .acquire import latest_artifact
    from .importer import ADAPTERS, run_adapter

    root = _open(arguments)
    names = sorted(ADAPTERS, key=lambda name: ADAPTERS[name].order) if arguments.adapter == "all" else [arguments.adapter]
    results = []
    with writer_lock(root), closing(connect(root.database)) as connection:
        for name in names:
            if name not in ADAPTERS:
                raise InvalidInput(f"Unknown adapter {name}; choose from {sorted(ADAPTERS)} or 'all'.")
            entry = ADAPTERS[name]
            artifact = arguments.artifact or latest_artifact(connection, entry.source_id, entry.artifact_key)
            result = run_adapter(root, connection, name, artifact)
            result.pop("diagnostics", None)
            results.append(result)
    return results


def command_release(arguments) -> Any:
    from .release import activate_release, build_release, diff_releases, publish_release

    root = _open(arguments)
    if arguments.release_command == "list":
        with closing(connect(root.database, read_only=True)) as connection:
            active = connection.execute("SELECT release_id FROM active_release").fetchone()
            return [{**dict(row), "active": bool(active and active[0] == row["release_id"])} for row in
                    connection.execute("SELECT release_id, status, created_at, published_at, parent_release_id "
                                       "FROM data_release ORDER BY created_at DESC")]
    if arguments.release_command == "diff":
        with closing(connect(root.database, read_only=True)) as connection:
            return diff_releases(connection, arguments.from_release, arguments.to_release)
    with writer_lock(root), closing(connect(root.database)) as connection:
        if arguments.release_command == "build":
            return build_release(root, connection, notes=arguments.notes)
        if arguments.release_command == "publish":
            return publish_release(root, connection, arguments.release_id)
        return activate_release(root, connection, arguments.release_id)


def command_refresh(arguments) -> dict:
    """Fetch every manifest artifact, import, and build a candidate release (never publishes)."""
    arguments.source = arguments.artifact = None
    fetched = command_fetch(arguments)
    arguments.adapter = "all"
    imported = command_import(arguments)
    arguments.release_command, arguments.notes = "build", "refresh"
    built = command_release(arguments)
    return {"fetched": fetched, "imported": imported, "candidate_release": built}


def command_issues(arguments):
    from .contracts import IssuesRequest
    from .query import ElectionDataReader

    reader = ElectionDataReader(_open(arguments).path)
    return reader.issues(IssuesRequest(release_id=arguments.release, severity=arguments.severity,
                                       cursor=arguments.cursor, limit=arguments.limit))


def command_coverage(arguments):
    from .query import ElectionDataReader

    return ElectionDataReader(_open(arguments).path).status().coverage


def command_results(arguments):
    from .contracts import ContestsRequest, ResultsRequest
    from .query import ElectionDataReader

    reader = ElectionDataReader(_open(arguments).path)
    contest = arguments.contest
    if contest is None:
        if arguments.state is None:
            raise InvalidInput("Pass --contest or --state.")
        contest = reader.contests(ContestsRequest(state=arguments.state, release_id=arguments.release)).contests[0].contest_id
    return reader.results(ResultsRequest(contest_id=contest, level=arguments.level, release_id=arguments.release,
                                         cursor=arguments.cursor, limit=arguments.limit))


def command_provenance(arguments):
    from .contracts import ProvenanceRequest
    from .query import ElectionDataReader

    return ElectionDataReader(_open(arguments).path).provenance(
        ProvenanceRequest(derivation_id=arguments.derivation_id, cursor=arguments.cursor, limit=arguments.limit))


def command_export(arguments) -> dict:
    root = _open(arguments)
    output = Path(arguments.output)
    if output.exists():
        raise InvalidInput(f"{output} already exists; exports never overwrite files.")
    with closing(connect(root.database, read_only=True)) as connection:
        release = arguments.release or connection.execute("SELECT release_id FROM active_release").fetchone()[0]
        rows = connection.execute(
            "SELECT r.release_id, c.label AS contest, r.contest_id, s.geography_level, u.label AS unit, r.unit_id, "
            "u.source_identifier, cand.display_name AS candidacy, r.candidacy_id, r.round_key, r.voting_mode, r.votes, "
            "r.denominator, r.denominator_kind, r.result_status, s.result_status AS unit_status, s.source_status, "
            "r.derivation_id FROM published_result r JOIN contest c USING (contest_id) "
            "JOIN reporting_unit u USING (unit_id) JOIN candidacy cand USING (candidacy_id) "
            "JOIN published_unit_status s ON s.release_id=r.release_id AND s.contest_id=r.contest_id AND s.unit_id=r.unit_id "
            "WHERE r.release_id=? ORDER BY c.seat_key, s.geography_level, u.label, r.votes DESC", (release,)).fetchall()
    with output.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(rows[0].keys() if rows else [])
        writer.writerows(tuple(row) for row in rows)
    return {"release_id": release, "rows": len(rows), "output": str(output), "format": "csv"}


def command_backup(arguments) -> dict:
    from .backup import create_backup, verify_backup

    if arguments.backup_command == "verify":
        return verify_backup(Path(arguments.manifest))
    root = _open(arguments)
    with writer_lock(root):
        return create_backup(root, Path(arguments.destination))


def command_restore(arguments) -> dict:
    from .backup import restore_backup

    return restore_backup(Path(arguments.backup), Path(arguments.destination))


def command_doctor(arguments) -> dict:
    root = _open(arguments)
    with closing(connect(root.database, read_only=True)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
        running = connection.execute("SELECT count(*) FROM import_run WHERE status='running'").fetchone()[0]
        active = connection.execute("SELECT release_id FROM active_release").fetchone()
        from .release import verify_assets
        assets = verify_assets(root, connection, active[0]) if active else []
    orphans = [path.name for path in (root.path / "staging").glob("download-*")]
    ok = integrity == "ok" and not foreign and not assets
    return {"ok": ok, "integrity_check": integrity, "foreign_key_violations": len(foreign),
            "orphaned_running_imports": running, "active_release": active[0] if active else None,
            "asset_problems": assets, "orphaned_staging_files": orphans}


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(prog="election-data", description=__doc__.splitlines()[0])
    main.add_argument("--data-dir", type=Path, default=None,
                      help="Data root (default: OS application-data directory or $ELECTION_DATA_DIR).")
    main.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    commands = main.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Create the data root, database schema and reference records")
    sources = commands.add_parser("sources", help="List reviewed sources")
    sources.add_argument("action", choices=["list"])
    fetch = commands.add_parser("fetch", help="Download manifest artifacts into the immutable archive")
    fetch.add_argument("--source")
    fetch.add_argument("--artifact")
    archive = commands.add_parser("archive", help="Archive a file you downloaded yourself")
    archive.add_argument("action", choices=["add"])
    archive.add_argument("--source", required=True)
    archive.add_argument("--artifact-key", required=True)
    archive.add_argument("file")
    importing = commands.add_parser("import", help="Run a reviewed adapter on an archived artifact")
    importing.add_argument("--adapter", required=True, help="Adapter id or 'all'")
    importing.add_argument("--artifact", help="Artifact id (default: latest archived for the adapter's source)")
    release = commands.add_parser("release", help="Build, diff, publish or activate releases")
    actions = release.add_subparsers(dest="release_command", required=True)
    build = actions.add_parser("build")
    build.add_argument("--notes")
    actions.add_parser("list")
    diff = actions.add_parser("diff")
    diff.add_argument("--from", dest="from_release", required=True)
    diff.add_argument("--to", dest="to_release", required=True)
    for name in ("publish", "activate"):
        action = actions.add_parser(name)
        action.add_argument("release_id")
    commands.add_parser("refresh", help="Fetch, import and build a candidate release (does not publish)")
    issues = commands.add_parser("issues", help="List validation issues for a release")
    issues.add_argument("--release")
    issues.add_argument("--severity", choices=["error", "warning", "info"])
    commands.add_parser("coverage", help="Summarize coverage of the active release")
    results = commands.add_parser("results", help="Show published results")
    results.add_argument("--contest")
    results.add_argument("--state")
    results.add_argument("--level", choices=["state", "county"], default="state")
    results.add_argument("--release")
    provenance = commands.add_parser("provenance", help="Trace a published result to archived source rows")
    provenance.add_argument("derivation_id")
    for paged in (issues, results, provenance):
        paged.add_argument("--cursor", type=int, default=0)
        paged.add_argument("--limit", type=int, default=10)
    export = commands.add_parser("export", help="Export a release's published results as CSV")
    export.add_argument("--release")
    export.add_argument("--format", choices=["csv"], default="csv")
    export.add_argument("--output", required=True)
    backup = commands.add_parser("backup", help="Create or verify a consistent backup")
    backup_actions = backup.add_subparsers(dest="backup_command", required=True)
    create = backup_actions.add_parser("create")
    create.add_argument("--destination", required=True)
    verify = backup_actions.add_parser("verify")
    verify.add_argument("manifest")
    restore = commands.add_parser("restore", help="Restore a verified backup into an empty directory")
    restore.add_argument("backup")
    restore.add_argument("destination")
    commands.add_parser("doctor", help="Integrity, foreign-key, asset and orphan checks")
    return main


HANDLERS = {"init": command_init, "sources": command_sources, "fetch": command_fetch, "archive": command_archive_add,
            "import": command_import, "release": command_release, "refresh": command_refresh,
            "issues": command_issues, "coverage": command_coverage, "results": command_results,
            "provenance": command_provenance, "export": command_export, "backup": command_backup,
            "restore": command_restore, "doctor": command_doctor}


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        value = HANDLERS[arguments.command](arguments)
    except ElectionDataError as error:
        payload = {"error": {"code": error.code, "message": str(error)}}
        print(json.dumps(payload) if arguments.json else f"error ({error.code}): {error}", file=sys.stderr)
        return error.exit_code
    except ValueError as error:
        print(json.dumps({"error": {"code": "INVALID_INPUT", "message": str(error)}}) if arguments.json
              else f"error (INVALID_INPUT): {error}", file=sys.stderr)
        return 2
    _emit(value, arguments.json)
    if arguments.command == "doctor" and not value["ok"]:
        return 6
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
