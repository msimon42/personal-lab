"""Reference bootstrap, curation records and the deterministic import-run lifecycle."""
from __future__ import annotations

import json
import platform
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, __version__
from .core import (DataRoot, InvalidInput, ReviewRequired, canonical_json, sha256_text, stable_id,
                   transaction, utc_now, verify_artifact)

CURATION_FILE = "curation-us-2024-president.json"


def load_curation() -> tuple[dict[str, Any], str]:
    text = resources.files("election_data.manifests").joinpath(CURATION_FILE).read_text(encoding="utf-8")
    value = json.loads(text)
    return value, sha256_text(canonical_json(value))


def code_revision() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                                cwd=Path(__file__).resolve().parent)
        return result.stdout.strip() if result.returncode == 0 else f"package-{__version__}"
    except (OSError, subprocess.SubprocessError):
        return f"package-{__version__}"


def environment_manifest() -> dict[str, Any]:
    return {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version, "platform": platform.system(),
            "package_version": __version__, "locale_independent": True, "timezone": "UTC"}


# --- identifiers for shared reference entities ----------------------------------------------------
def person_id(key: str) -> str:
    return stable_id("person", key)


def party_id(key: str) -> str:
    return stable_id("party", key)


def office_id(code: str) -> str:
    return stable_id("office", code)


def election_id(natural_key: str) -> str:
    return stable_id("election", natural_key)


def geo_id(natural_key: str) -> str:
    return stable_id("geo", natural_key)


def geo_version_id(natural_key: str, valid_from: str) -> str:
    return stable_id("geov", natural_key, valid_from)


def unit_id(natural_key: str) -> str:
    return stable_id("unit", natural_key)


def contest_id(election_key: str, office_code: str, seat_key: str, stage: str = "general",
               contest_type: str = "regular") -> str:
    return stable_id("contest", election_key, office_code, seat_key, stage, contest_type)


def candidacy_id(contest: str, natural_key: str) -> str:
    return stable_id("candidacy", contest, natural_key)


def option_id(contest: str, natural_key: str) -> str:
    return stable_id("option", contest, natural_key)


def decision_ids(curation: dict[str, Any]) -> dict[str, str]:
    return {decision["decision_id"]: decision["decision_id"] for decision in curation["decisions"]}


def ensure_reference(connection: sqlite3.Connection, curation: dict[str, Any]) -> None:
    """Idempotently register office, parties, persons, the nation entity and curation decisions."""
    office = curation["office"]
    with transaction(connection):
        connection.execute("INSERT OR IGNORE INTO office VALUES (?,?,?,?,?)",
                           (office_id(office["code"]), office["code"], office["name"], office["level"],
                            office["default_selection_method"]))
        for party in curation["parties"]:
            connection.execute("INSERT OR IGNORE INTO party VALUES (?,?,?)",
                               (party_id(party["key"]), party["name"], party["key"]))
        for person in curation["persons"]:
            connection.execute("INSERT OR IGNORE INTO person VALUES (?,?,?,0)",
                               (person_id(person["key"]), person["name"], person["key"]))
        connection.execute("INSERT OR IGNORE INTO geographic_entity VALUES (?,?,?,?)",
                           (geo_id("nation:US"), "nation", "United States", "nation:US"))
        for decision in curation["decisions"]:
            payload = {key: value for key, value in decision.items() if key != "decision_id"}
            content_hash = sha256_text(canonical_json(payload))
            existing = connection.execute("SELECT content_hash FROM curation_decision WHERE decision_id=?",
                                          (decision["decision_id"],)).fetchone()
            if existing and existing[0] != content_hash:
                raise ReviewRequired(
                    f"Curation decision {decision['decision_id']} changed. Decisions are append-only: add a new "
                    "decision that supersedes it instead of editing it.")
            if not existing:
                connection.execute(
                    "INSERT INTO curation_decision VALUES (?,?,?,?,?,?,?,?,?)",
                    (decision["decision_id"], decision["decision_type"], decision["entity_id"], decision["rationale"],
                     canonical_json({"evidence": decision.get("evidence", {}), "scope": decision.get("scope", {})}),
                     curation["author"], curation["created_at"], decision.get("supersedes"), content_hash))


def state_versions(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Map postal code -> state geographic version from the imported boundary reference."""
    rows = connection.execute(
        "SELECT gi.value AS postal, gv.geo_version_id, gv.name, fips.value AS fips "
        "FROM geographic_identifier gi JOIN geographic_version gv USING (geo_version_id) "
        "JOIN geographic_identifier fips ON fips.geo_version_id = gv.geo_version_id AND fips.scheme = 'state_fips' "
        "WHERE gi.scheme = 'state_postal'").fetchall()
    return {row["postal"]: row for row in rows}


def county_versions(connection: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in connection.execute(
        "SELECT value, geo_version_id FROM geographic_identifier WHERE scheme = 'county_fips'")}


def ensure_contests(connection: sqlite3.Connection, curation: dict[str, Any]) -> dict[str, str]:
    """Register the election event and one contest per elector-selection scope.

    Each state's statewide elector contest is distinct. Maine and Nebraska also have
    congressional-district elector contests; their popular votes are not in Phase 1
    sources and remain explicitly unavailable rather than inferred.
    """
    states = state_versions(connection)
    missing = [postal for postal in curation["states_with_electors"] if postal not in states]
    if missing:
        raise InvalidInput(f"Import Census state boundaries first; missing state geography for {missing}.")
    election = curation["election"]
    event = election_id(election["natural_key"])
    rcv_states = set(next(decision["scope"]["states"] for decision in curation["decisions"]
                          if decision["decision_id"] == "dec-2024-pres-rcv-first-round"))
    components = next(decision["scope"] for decision in curation["decisions"]
                      if decision["decision_type"] == "electoral_components")
    contests: dict[str, str] = {}
    with transaction(connection):
        connection.execute("INSERT OR IGNORE INTO election_event VALUES (?,?,?,?,?,?,?,?,?)",
                           (event, election["label"], election["date"], election["date"], "day",
                            election["cycle_year"], geo_id("nation:US"), "general", election["natural_key"]))
        for postal in curation["states_with_electors"]:
            seats = [("statewide", f"president:{postal}", "Presidential electors")]
            if postal in components:
                seats = [("statewide_at_large", f"president:{postal}:at-large", "At-large presidential electors")]
                seats += [(name, f"president:{postal}:{name.lower()}", f"Presidential elector, {name}")
                          for name in components[postal] if name.startswith("CD")]
            for kind, seat_key, label in seats:
                identifier = contest_id(election["natural_key"], "president", seat_key)
                voting_system = "ranked_choice" if postal in rcv_states else "plurality"
                connection.execute(
                    "INSERT OR IGNORE INTO contest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identifier, event, office_id("president"), seat_key, states[postal]["geo_version_id"],
                     "general", "regular", "popular_vote_for_electors", voting_system,
                     1, None, "2025-01-20", "2029-01-20", f"2024 President - {states[postal]['name']} ({label})"))
                contests[seat_key] = identifier
    return contests


def statewide_contest(postal: str, curation: dict[str, Any]) -> str:
    components = next(decision["scope"] for decision in curation["decisions"]
                      if decision["decision_type"] == "electoral_components")
    seat = f"president:{postal}:at-large" if postal in components else f"president:{postal}"
    return contest_id(curation["election"]["natural_key"], "president", seat)


def round_key_for(postal: str, curation: dict[str, Any]) -> str:
    scope = next(decision["scope"] for decision in curation["decisions"]
                 if decision["decision_id"] == "dec-2024-pres-rcv-first-round")
    return scope["round_key"] if postal in scope["states"] else "final_or_only"


def ensure_ticket(connection: sqlite3.Connection, curation: dict[str, Any], contest: str, person_key: str,
                  *, provisional_label: str | None = None) -> str:
    """Candidacy for a ticket headed by ``person_key``; unresolved labels get provisional persons."""
    people = {person["key"]: person for person in curation["persons"]}
    if person_key not in people and provisional_label is None:
        raise InvalidInput(f"Unknown person {person_key}.")
    identifier = candidacy_id(contest, f"ticket:{person_key}")
    if person_key not in people:
        connection.execute("INSERT OR IGNORE INTO person VALUES (?,?,?,1)",
                           (person_id(person_key), provisional_label, person_key))
        name, party = provisional_label, None
    else:
        name, party = people[person_key]["name"], people[person_key]["party"]
    connection.execute("INSERT OR IGNORE INTO candidacy VALUES (?,?,?,?,?,?,?)",
                       (identifier, contest, name, "ticket", party_id(party) if party else None, "on_ballot_or_reported",
                        f"ticket:{person_key}"))
    connection.execute("INSERT OR IGNORE INTO candidacy_member VALUES (?,?,?)",
                       (identifier, person_id(person_key), "president"))
    for ticket in curation["tickets"]:
        if ticket["president"] == person_key:
            connection.execute("INSERT OR IGNORE INTO candidacy_member VALUES (?,?,?)",
                               (identifier, person_id(ticket["vice_president"]), "vice_president"))
    connection.execute("INSERT OR IGNORE INTO ballot_option VALUES (?,?,?,?,?,?,?)",
                       (option_id(contest, f"ticket:{person_key}"), contest, identifier,
                        party_id(party) if party else None, "candidacy_total", name, f"ticket:{person_key}"))
    return option_id(contest, f"ticket:{person_key}")


def ensure_aggregate(connection: sqlite3.Connection, contest: str, key: str, label: str) -> str:
    kind = {"aggregate-write-in-scattered": "aggregate_write_in",
            "none-of-these-candidates": "none_of_these"}.get(key, "aggregate_other")
    identifier = candidacy_id(contest, key)
    connection.execute("INSERT OR IGNORE INTO candidacy VALUES (?,?,?,?,?,?,?)",
                       (identifier, contest, label, "aggregate", None, "source_aggregate", key))
    connection.execute("INSERT OR IGNORE INTO ballot_option VALUES (?,?,?,?,?,?,?)",
                       (option_id(contest, key), contest, identifier, None, kind, label, key))
    return option_id(contest, key)


# --- import runs -----------------------------------------------------------------------------------
@dataclass
class RunContext:
    run_id: str
    identity: str
    existing: bool
    artifacts: dict[str, str]
    paths: dict[str, Path]


@dataclass
class Normalized:
    """Typed rows produced by an adapter, inserted atomically with the run's completion."""
    result_sets: list[tuple] = field(default_factory=list)
    votes: list[tuple] = field(default_factory=list)
    metrics: list[tuple] = field(default_factory=list)
    evidence: list[tuple] = field(default_factory=list)
    other: list[tuple[str, tuple]] = field(default_factory=list)
    accounting: dict[str, int] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    def output_hash(self) -> str:
        content = {"sets": sorted(self.result_sets), "votes": sorted(self.votes), "metrics": sorted(self.metrics),
                   "evidence": sorted(self.evidence), "other": sorted(self.other, key=repr)}
        return sha256_text(canonical_json(content))


def import_identity(adapter_id: str, adapter_version: str, artifacts: dict[str, str], mapping_hash: str,
                    config: dict[str, Any]) -> str:
    return sha256_text(canonical_json({
        "adapter": adapter_id, "adapter_version": adapter_version, "artifacts": artifacts,
        "mapping": mapping_hash, "config": config, "schema_version": SCHEMA_VERSION}))


def begin_run(root: DataRoot, connection: sqlite3.Connection, *, adapter_id: str, adapter_version: str,
              artifacts: dict[str, str], mapping_version: str, mapping_hash: str,
              config: dict[str, Any]) -> RunContext:
    sha = {role: connection.execute("SELECT sha256 FROM source_artifact WHERE artifact_id=?",
                                    (artifact,)).fetchone()[0] for role, artifact in artifacts.items()}
    identity = import_identity(adapter_id, adapter_version, sha, mapping_hash, config)
    completed = connection.execute("SELECT run_id FROM import_run WHERE import_identity=? AND status='completed'",
                                   (identity,)).fetchone()
    paths = {role: verify_artifact(root, connection, artifact) for role, artifact in artifacts.items()}
    if completed:
        return RunContext(completed[0], identity, True, artifacts, paths)
    started = utc_now()
    run = stable_id("run", identity, started, len(connection.execute("SELECT 1 FROM import_run").fetchall()))
    with transaction(connection):
        connection.execute(
            "INSERT INTO import_run VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run, identity, adapter_id, adapter_version, code_revision(), sha256_text(canonical_json(config)),
             mapping_version, SCHEMA_VERSION, started, None, "running", None, None,
             canonical_json(environment_manifest()), None))
        for role, artifact in artifacts.items():
            connection.execute("INSERT INTO run_artifact VALUES (?,?,?)", (run, artifact, role))
    return RunContext(run, identity, False, artifacts, paths)


def complete_run(connection: sqlite3.Connection, context: RunContext, normalized: Normalized,
                 write_extra=None) -> dict[str, Any]:
    """Insert all normalized facts and mark the run completed in one transaction."""
    output_hash = normalized.output_hash()
    try:
        with transaction(connection):
            # Reference rows (units, geography, components) precede the facts that cite them.
            for statement, values in normalized.other:
                connection.execute(statement, values)
            connection.executemany("INSERT INTO source_result_set VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                   [(row[0], context.run_id, *row[1:]) for row in normalized.result_sets])
            connection.executemany("INSERT INTO vote_observation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", normalized.votes)
            connection.executemany("INSERT INTO metric_observation VALUES (?,?,?,?,?,?,?,?,?,?)", normalized.metrics)
            connection.executemany("INSERT INTO observation_evidence VALUES (?,?,?)", normalized.evidence)
            if write_extra:
                write_extra(connection, context)
            connection.execute(
                "UPDATE import_run SET status='completed', finished_at=?, output_hash=?, row_accounting_json=?, "
                "diagnostics_json=? WHERE run_id=?",
                (utc_now(), output_hash, canonical_json(normalized.accounting),
                 canonical_json(normalized.diagnostics[:200]), context.run_id))
    except Exception as error:
        with transaction(connection):
            connection.execute("UPDATE import_run SET status='failed', finished_at=?, diagnostics_json=? WHERE run_id=?",
                               (utc_now(), canonical_json([{"error": str(error)[:1000]}]), context.run_id))
        raise
    return {"run_id": context.run_id, "status": "completed", "output_hash": output_hash,
            "accounting": normalized.accounting, "diagnostics": normalized.diagnostics[:20]}


def fail_run(connection: sqlite3.Connection, context: RunContext, error: Exception) -> None:
    with transaction(connection):
        connection.execute("UPDATE import_run SET status='failed', finished_at=?, diagnostics_json=? WHERE run_id=?",
                           (utc_now(), canonical_json([{"error": str(error)[:1000]}]), context.run_id))


def existing_result(connection: sqlite3.Connection, context: RunContext) -> dict[str, Any]:
    row = connection.execute("SELECT output_hash, row_accounting_json FROM import_run WHERE run_id=?",
                             (context.run_id,)).fetchone()
    return {"run_id": context.run_id, "status": "already_imported", "output_hash": row[0],
            "accounting": json.loads(row[1] or "{}"), "diagnostics": []}
