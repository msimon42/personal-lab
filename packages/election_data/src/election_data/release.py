"""Candidate release build, validation gates, assets, atomic publication and rollback.

A release selects coherent source result sets under curation decisions, derives
serving rows with recorded inputs, reconciles county partitions to official state
totals, and writes immutable map assets. Publication switches ``active_release`` in
one transaction only after every referenced asset is verified.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .core import (DataRoot, ElectionDataError, InvalidInput, ReviewRequired, canonical_json, sha256_text,
                   stable_id, transaction, utc_now)
from .importer import ADAPTERS
from .pipeline import load_curation, person_id, state_versions, statewide_contest, unit_id

RULE_VERSION = "release-rules-v1"
RULES = {
    "state_total_equals_option_sum": 1,
    "county_partition_reconciles_to_state": 1,
    "batch_sum_matches_totalvotes_column": 1,
    "unit_without_total_mode": 1,
    "unit_missing_option_count": 1,
    "unit_merge_requires_reconciliation": 1,
    "rcv_first_round_majority": 1,
    "electoral_components_match": 1,
    "fec_electoral_votes_match_nara": 1,
    "plurality_leader_matches_electors": 1,
    "unmapped_reporting_unit": 1,
    "boundary_join": 1,
}
BOUNDARY_SET_STATE = stable_id("boundary_set", "census", "2020", "cb_2020_us_state_20m")
BOUNDARY_SET_COUNTY = stable_id("boundary_set", "census", "2020", "cb_2020_us_county_20m")


@dataclass
class Build:
    release_id: str
    issues: list[tuple] = field(default_factory=list)
    results: list[tuple] = field(default_factory=list)
    unit_status: list[tuple] = field(default_factory=list)
    selections: list[tuple] = field(default_factory=list)
    derivations: dict[str, tuple] = field(default_factory=dict)
    derivation_inputs: set[tuple] = field(default_factory=set)
    outcomes: list[tuple] = field(default_factory=list)
    coverage: list[tuple] = field(default_factory=list)
    extra_units: list[tuple] = field(default_factory=list)

    def issue(self, rule: str, severity: str, entity_type: str, entity: str, message: str, *,
              expected: Any = None, actual: Any = None, resolution: str = "open", decision: str | None = None) -> None:
        identifier = stable_id("issue", self.release_id, rule, entity_type, entity, message)
        self.issues.append((identifier, None, self.release_id, rule, RULES[rule], severity, entity_type, entity,
                            None if expected is None else canonical_json(expected),
                            None if actual is None else canonical_json(actual), message, resolution, None, decision))

    def derive(self, contest: str, unit: str, level: str, inputs: list[tuple[str, str]], parameters: dict) -> str:
        ordered = sorted(set(inputs))
        output = sha256_text(canonical_json({"inputs": ordered, "parameters": parameters}))
        identifier = stable_id("deriv", RULE_VERSION, contest, unit, level, output)
        self.derivations[identifier] = (identifier, RULE_VERSION, None, canonical_json(parameters), output)
        for kind, observation in ordered:
            self.derivation_inputs.add((identifier, kind, observation))
        return identifier


def _decision(curation: dict, decision_id: str) -> dict:
    return next(decision for decision in curation["decisions"] if decision["decision_id"] == decision_id)


def latest_runs(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    adapter_ids = {"census-state-2020": "census-cartographic-boundary", "census-county-2020": "census-cartographic-boundary"}
    runs = {}
    for name in ADAPTERS:
        adapter_id = adapter_ids.get(name)
        if adapter_id:
            layer = "cb_2020_us_state_20m" if name == "census-state-2020" else "cb_2020_us_county_20m"
            row = connection.execute(
                "SELECT * FROM import_run WHERE adapter_id=? AND status='completed' AND config_hash=? "
                "ORDER BY finished_at DESC, rowid DESC LIMIT 1",
                (adapter_id, sha256_text(canonical_json({"layer": layer})))).fetchone()
        else:
            from .adapters import fec, mit, nara
            adapter_id = {"fec-president-2024": fec.ADAPTER_ID, "mit-county-president-2024": mit.ADAPTER_ID,
                          "nara-electoral-2024": nara.ADAPTER_ID}[name]
            row = connection.execute("SELECT * FROM import_run WHERE adapter_id=? AND status='completed' "
                                     "ORDER BY finished_at DESC, rowid DESC LIMIT 1", (adapter_id,)).fetchone()
        if row is None:
            raise InvalidInput(f"No completed import for {name}; run the import first.")
        runs[name] = row
    return runs


def _sets(connection: sqlite3.Connection, run_id: str) -> dict[str, str]:
    return {row["contest_id"]: row["set_id"] for row in connection.execute(
        "SELECT contest_id, set_id FROM source_result_set WHERE run_id=?", (run_id,))}


def _candidacy_of_option(connection: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in connection.execute("SELECT option_id, candidacy_id FROM ballot_option")}


def _features(connection: sqlite3.Connection, boundary_set: str) -> dict[str, sqlite3.Row]:
    return {row["geo_version_id"]: row for row in connection.execute(
        "SELECT feature_id, geo_version_id, source_feature_key, geometry_json FROM boundary_feature "
        "WHERE boundary_set_id=?", (boundary_set,))}


def _state_level(connection, build: Build, curation, runs, candidacy_of, state_features) -> dict[str, dict]:
    """Official FEC totals for each statewide contest; returns per-state candidacy totals."""
    fec_sets = _sets(connection, runs["fec-president-2024"]["run_id"])
    totals: dict[str, dict] = {}
    for postal, state in sorted(state_versions(connection).items()):
        if postal not in curation["states_with_electors"]:
            continue
        contest = statewide_contest(postal, curation)
        set_id = fec_sets[contest]
        unit = unit_id(f"state:{state['fips']}")
        votes = connection.execute(
            "SELECT observation_id, option_id, round_key, votes FROM vote_observation WHERE set_id=? AND unit_id=? "
            "AND voting_mode='all' AND value_status='reported'", (set_id, unit)).fetchall()
        metric = connection.execute(
            "SELECT metric_id, integer_value FROM metric_observation WHERE set_id=? AND unit_id=? "
            "AND metric_code='total_votes_all_options'", (set_id, unit)).fetchone()
        option_sum = sum(row["votes"] for row in votes)
        denominator = metric["integer_value"] if metric else None
        if denominator != option_sum:
            build.issue("state_total_equals_option_sum", "warning", "contest", contest,
                        f"{postal}: FEC TOTAL VOTES differs from the sum of listed options; shares use the option sum.",
                        expected=denominator, actual=option_sum, resolution="accepted",
                        decision="dec-2024-pres-source-priority")
            denominator = option_sum
        inputs = [("vote_observation", row["observation_id"]) for row in votes]
        if metric:
            inputs.append(("metric_observation", metric["metric_id"]))
        derivation = build.derive(contest, unit, "state", inputs, {"rule": "official_state_totals",
                                                                   "denominator": "all_reported_options"})
        per_candidacy = {}
        round_key = votes[0]["round_key"] if votes else "final_or_only"
        for row in votes:
            candidacy = candidacy_of[row["option_id"]]
            per_candidacy[candidacy] = row["votes"]
            build.results.append((build.release_id, contest, unit, candidacy, row["round_key"], "all", row["votes"],
                                  denominator, "all_reported_options", "reported", derivation))
        feature = state_features.get(state["geo_version_id"])
        build.unit_status.append((build.release_id, contest, unit, "state", "complete",
                                  feature["feature_id"] if feature else None,
                                  "matched" if feature else "boundary_unavailable",
                                  "official_compilation_of_certified", canonical_json([])))
        build.selections.append((build.release_id, contest, unit, "state_totals", set_id,
                                 "dec-2024-pres-source-priority"))
        totals[postal] = {"contest": contest, "unit": unit, "candidacies": per_candidacy, "total": denominator,
                          "round_key": round_key}
    return totals


def _choose_mode(postal: str, modes: dict[str, Any], curation) -> tuple[str | None, str | None]:
    if "all" in modes:
        return "all", None
    blank = _decision(curation, "dec-2024-pres-mit-blank-mode")["scope"]
    if postal in blank["states"] and set(modes) == {"unknown"}:
        return "unknown", "dec-2024-pres-mit-blank-mode"
    vote_center = _decision(curation, "dec-2024-pres-mit-sd-vote-center")["scope"]
    if postal in vote_center["states"] and set(modes) == {"vote_center"}:
        return "vote_center", "dec-2024-pres-mit-sd-vote-center"
    return None, None


def _county_units(connection, build: Build, curation, runs, candidacy_of) -> dict[str, dict]:
    """Per raw MIT unit, the selected all-mode tally under curation decisions (or a withheld reason)."""
    mit_sets = _sets(connection, runs["mit-county-president-2024"]["run_id"])
    batch_scope = _decision(curation, "dec-2024-pres-mit-repeated-rows")["scope"]["states"]
    raw: dict[str, dict] = {}
    unit_rows = {row["unit_id"]: row for row in connection.execute("SELECT * FROM reporting_unit")}
    states_by_unit = {unit_id(f"state:{row['fips']}"): postal for postal, row in state_versions(connection).items()}
    for contest, set_id in sorted(mit_sets.items()):
        observations = connection.execute(
            "SELECT observation_id, unit_id, option_id, voting_mode, batch_key, votes, value_status, round_key "
            "FROM vote_observation WHERE set_id=? ORDER BY unit_id, option_id, voting_mode, batch_key",
            (set_id,)).fetchall()
        totalvotes = {(row["unit_id"], row["voting_mode"]): row for row in connection.execute(
            "SELECT metric_id, unit_id, voting_mode, integer_value FROM metric_observation WHERE set_id=? "
            "AND metric_code='mit_totalvotes_column'", (set_id,))}
        grouped: dict[str, dict[str, dict[str, list]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for row in observations:
            grouped[row["unit_id"]][row["voting_mode"]][row["option_id"]].append(row)
        for unit, modes in grouped.items():
            unit_row = unit_rows[unit]
            postal = states_by_unit[unit_row["parent_unit_id"]]
            entry = {"contest": contest, "set_id": set_id, "postal": postal, "unit": unit, "kind": unit_row["kind"],
                     "natural_key": unit_row["natural_key"], "geo_version_id": unit_row["geo_version_id"],
                     "values": {}, "inputs": [], "notes": [unit_row["notes"]] if unit_row["notes"] else [],
                     "status": "complete", "decisions": [],
                     "round_key": observations[0]["round_key"]}
            raw[unit] = entry
            if unit_row["natural_key"].startswith("unmapped:"):
                build.issue("unmapped_reporting_unit", "error", "reporting_unit", unit,
                            f"{postal}: source unit {unit_row['source_identifier']} has no reviewed geography.")
                entry["status"], entry["notes"] = "unavailable", ["unmapped source unit"]
                continue
            mode, decision = _choose_mode(postal, modes, curation)
            if mode is None:
                build.issue("unit_without_total_mode", "warning", "reporting_unit", unit,
                            f"{postal} {unit_row['label']}: no all-mode total and no reviewed mode decision.",
                            actual=sorted(modes), resolution="accepted", decision="dec-2024-pres-source-priority")
                entry["status"], entry["notes"] = "unavailable", [f"no all-mode total (modes: {sorted(modes)})"]
                continue
            if decision:
                entry["decisions"].append(decision)
            entry["mode"] = mode
            repeated = any(len(batches) > 1 for batches in modes[mode].values())
            if repeated:
                metric = totalvotes.get((unit, mode))
                unit_sum = sum(row["votes"] or 0 for batches in modes[mode].values() for row in batches)
                if postal not in batch_scope or metric is None or metric["integer_value"] != unit_sum:
                    build.issue("batch_sum_matches_totalvotes_column", "warning", "reporting_unit", unit,
                                f"{postal} {unit_row['label']}: repeated source rows cannot be summed under review.",
                                expected=None if metric is None else metric["integer_value"], actual=unit_sum,
                                resolution="accepted", decision="dec-2024-pres-mit-repeated-rows")
                    entry["status"], entry["notes"] = "unavailable", ["repeated source rows not reconcilable"]
                    continue
                entry["decisions"].append("dec-2024-pres-mit-repeated-rows")
                entry["inputs"].append(("metric_observation", metric["metric_id"]))
                entry["notes"].append("repeated source rows summed after matching the MIT totalvotes column")
            for option, batches in modes[mode].items():
                candidacy = candidacy_of[option]
                entry["inputs"].extend(("vote_observation", row["observation_id"]) for row in batches)
                if any(row["value_status"] != "reported" for row in batches):
                    entry["values"][candidacy] = None
                    entry["status"] = "partial"
                else:
                    entry["values"][candidacy] = sum(row["votes"] for row in batches)
            if entry["status"] == "partial":
                build.issue("unit_missing_option_count", "warning", "reporting_unit", unit,
                            f"{postal} {unit_row['label']}: at least one option count is missing; shares withheld.",
                            resolution="accepted", decision="dec-2024-pres-source-priority")
                entry["notes"].append("one or more option counts missing; shares withheld")
    return raw


def _reconcile(build: Build, raw: dict[str, dict], state_totals: dict[str, dict]) -> dict[str, dict]:
    """Compare every state's county partition with the official state totals."""
    report = {}
    by_state: dict[str, list[dict]] = defaultdict(list)
    for entry in raw.values():
        by_state[entry["postal"]].append(entry)
    for postal, entries in sorted(by_state.items()):
        official = state_totals[postal]
        complete = all(entry["status"] == "complete" for entry in entries)
        sums: dict[str, int] = defaultdict(int)
        for entry in entries:
            for candidacy, value in entry["values"].items():
                sums[candidacy] += value or 0
        compared = {}
        for candidacy, official_votes in official["candidacies"].items():
            if candidacy in sums:
                compared[candidacy] = {"official": official_votes, "county_sum": sums[candidacy],
                                       "difference": sums[candidacy] - official_votes}
        county_total = sum(sums.values())
        differences = {key: value for key, value in compared.items() if value["difference"]}
        status = "reconciled" if complete and not differences and county_total == official["total"] else "differs"
        report[postal] = {"status": status, "complete_partition": complete, "candidacies": compared,
                          "county_total_all_options": county_total, "official_total_all_options": official["total"]}
        if status == "reconciled":
            build.issue("county_partition_reconciles_to_state", "info", "contest", official["contest"],
                        f"{postal}: county partition equals the official state totals.")
        else:
            build.issue("county_partition_reconciles_to_state", "warning", "contest", official["contest"],
                        f"{postal}: county partition does not equal the official state totals; county view is "
                        "published with this difference disclosed.",
                        expected={"total": official["total"]}, actual={"total": county_total,
                                                                         "differences": differences},
                        resolution="accepted", decision="dec-2024-pres-source-priority")
    return report


def _publish_county_level(connection, build: Build, curation, raw, reconciliation, county_features) -> None:
    merge = _decision(curation, "dec-2024-pres-kansas-city-merge")["scope"]
    members = {entry["natural_key"]: entry for entry in raw.values()
               if entry["natural_key"].startswith((f"county-part:{merge['target_fips']}", "election-jurisdiction:MO:"))}
    published: list[dict] = []
    merge_ok = (len(members) == len(merge["members"]) and reconciliation.get("MO", {}).get("status") == "reconciled"
                and all(entry["status"] == "complete" for entry in members.values()))
    if members:
        if merge_ok:
            target_natural = f"county:{merge['target_fips']}"
            target_unit = unit_id(target_natural)
            geo = connection.execute("SELECT geo_version_id FROM geographic_identifier WHERE scheme='county_fips' "
                                     "AND value=?", (merge["target_fips"],)).fetchone()[0]
            sample = next(iter(members.values()))
            build.extra_units.append((target_unit, "county", geo, unit_id(f"state:{merge['target_fips'][:2]}"),
                                      "Jackson County (merged reporting units)", f"merge:{'+'.join(merge['members'])}",
                                      target_natural, "Documented merge of nonoverlapping reporting units."))
            values: dict[str, int] = defaultdict(int)
            inputs = []
            for entry in members.values():
                for candidacy, value in entry["values"].items():
                    values[candidacy] += value
                inputs.extend(entry["inputs"])
            published.append({**sample, "unit": target_unit, "values": dict(values), "inputs": inputs,
                              "geo_version_id": geo, "kind": "county",
                              "notes": ["merge of Jackson County election authority and Kansas City (MIT 36000)"],
                              "decisions": ["dec-2024-pres-kansas-city-merge"]})
            build.issue("unit_merge_requires_reconciliation", "info", "reporting_unit", target_unit,
                        "Kansas City merge accepted: Missouri partition reconciles to the official total.",
                        resolution="accepted", decision="dec-2024-pres-kansas-city-merge")
        else:
            build.issue("unit_merge_requires_reconciliation", "warning", "reporting_unit",
                        unit_id(f"county:{merge['target_fips']}"),
                        "Kansas City merge withheld: Missouri partition does not reconcile; members published "
                        "as separate units without geometry.", resolution="accepted",
                        decision="dec-2024-pres-kansas-city-merge")
    for entry in raw.values():
        if merge_ok and entry["natural_key"] in members:
            continue
        published.append(entry)
    for entry in published:
        contest, unit = entry["contest"], entry["unit"]
        feature = county_features.get(entry["geo_version_id"]) if entry["geo_version_id"] else None
        boundary = "matched" if feature else "boundary_unavailable"
        notes = list(entry["notes"])
        if reconciliation[entry["postal"]]["status"] != "reconciled":
            notes.append("state county partition differs from official state totals")
        if entry["status"] == "unavailable":
            build.unit_status.append((build.release_id, contest, unit, "county", "unavailable",
                                      feature["feature_id"] if feature else None, boundary,
                                      "secondary_compilation", canonical_json(notes)))
            continue
        complete = entry["status"] == "complete"
        denominator = sum(entry["values"].values()) if complete else None
        if denominator == 0:
            denominator = None
        derivation = build.derive(contest, unit, "county", entry["inputs"], {
            "rule": "mit_selected_mode_sum", "mode": entry.get("mode"), "decisions": sorted(set(entry["decisions"])),
            "denominator": "all_reported_options"})
        for candidacy, value in sorted(entry["values"].items()):
            build.results.append((build.release_id, contest, unit, candidacy, entry["round_key"], "all", value,
                                  denominator, "all_reported_options" if denominator else None,
                                  "reported" if value is not None else "missing", derivation))
        build.unit_status.append((build.release_id, contest, unit, "county", "complete" if complete else "partial",
                                  feature["feature_id"] if feature else None, boundary, "secondary_compilation",
                                  canonical_json(notes)))
        build.selections.append((build.release_id, contest, unit, "county_detail", entry["set_id"],
                                 sorted(set(entry["decisions"]))[0] if entry["decisions"]
                                 else "dec-2024-pres-source-priority"))


def _outcomes(connection, build: Build, curation, runs, state_totals) -> None:
    nara_run = runs["nara-electoral-2024"]["run_id"]
    components = _decision(curation, "dec-2024-pres-district-electors")["scope"]
    rcv = _decision(curation, "dec-2024-pres-rcv-first-round")["scope"]["states"]
    persons_to_candidacy = {}
    for row in connection.execute("SELECT c.candidacy_id, c.contest_id, m.person_id FROM candidacy c "
                                  "JOIN candidacy_member m USING (candidacy_id) WHERE m.role='president'"):
        persons_to_candidacy[(row["contest_id"], row["person_id"])] = row["candidacy_id"]
    electoral = defaultdict(dict)
    for row in connection.execute(
            "SELECT ev.electoral_vote_id, ev.recipient_person_id, ev.vote_role, ev.votes, ea.geo_version_id, "
            "ea.source_artifact_id FROM electoral_vote ev JOIN electoral_allocation ea USING (allocation_id) "
            "WHERE ev.run_id=? AND ea.allocation_kind='state_total'", (nara_run,)):
        electoral[row["geo_version_id"]][(row["recipient_person_id"], row["vote_role"])] = row
    fec_columns = json.loads(runs["fec-president-2024"]["diagnostics_json"])[0]["fec_electoral_columns"]
    fec_by_state = {entry["state"]: entry for entry in fec_columns}
    states = state_versions(connection)
    for postal, totals in sorted(state_totals.items()):
        contest = totals["contest"]
        votes = electoral[states[postal]["geo_version_id"]]
        president_votes = {person: row["votes"] for (person, role), row in votes.items() if role == "president"}
        artifact = next(iter(votes.values()))["source_artifact_id"] if votes else None
        fec = fec_by_state[postal]
        fec_votes = {person_id("donald-j-trump"): int(fec["trump"] or 0), person_id("kamala-d-harris"): int(fec["harris"] or 0)}
        if {key: value for key, value in fec_votes.items() if value} != president_votes:
            build.issue("fec_electoral_votes_match_nara", "error", "contest", contest,
                        f"{postal}: FEC electoral vote columns differ from NARA certificates.",
                        expected=president_votes, actual=fec_votes)
        ranked = sorted(totals["candidacies"].items(), key=lambda item: -item[1])
        leader, leader_votes = ranked[0]
        if postal in rcv:
            share = leader_votes / totals["total"]
            if share > 0.5:
                build.issue("rcv_first_round_majority", "info", "contest", contest,
                            f"{postal}: first-round leader has {share:.4f} of reported options; no further round needed.",
                            resolution="accepted", decision="dec-2024-pres-rcv-first-round")
            else:
                build.issue("rcv_first_round_majority", "error", "contest", contest,
                            f"{postal}: first-round leader lacks a majority; round tabulation required.")
                leader = None
        if leader is not None:
            tie = len(ranked) > 1 and ranked[1][1] == leader_votes
            build.outcomes.append((build.release_id, contest, None if tie else leader,
                                   "unresolved" if tie else "plurality_leader", "derived_from_official_compilation",
                                   None, "Popular-vote plurality from FEC official state totals; not an elector outcome."))
        if postal in components:
            parts = components[postal]
            winners = defaultdict(int)
            for part in parts.values():
                winners[person_id(part["winner"])] += part["electors"]
            if dict(winners) != president_votes:
                build.issue("electoral_components_match", "error", "contest", contest,
                            f"{postal}: curated district components do not sum to NARA electoral votes.",
                            expected=president_votes, actual=dict(winners))
            for key, part in parts.items():
                seat = (f"president:{postal}:at-large" if key == "statewide_at_large"
                        else f"president:{postal}:{key.lower()}")
                from .pipeline import contest_id, ensure_ticket
                target = contest_id(curation["election"]["natural_key"], "president", seat)
                candidacy = persons_to_candidacy.get((target, person_id(part["winner"])))
                if candidacy is None:
                    ensure_ticket(connection, curation, target, part["winner"])
                    candidacy = persons_to_candidacy[(target, person_id(part["winner"]))] = \
                        connection.execute("SELECT candidacy_id FROM candidacy WHERE contest_id=? AND natural_key=?",
                                           (target, f"ticket:{part['winner']}")).fetchone()[0]
                build.outcomes.append((build.release_id, target, candidacy, "won_electors",
                                       "nara_certificate_note_curated", artifact,
                                       f"{part['electors']} elector(s); dec-2024-pres-district-electors"))
            build.issue("electoral_components_match", "info", "contest", contest,
                        f"{postal}: district components match NARA electoral votes.",
                        resolution="accepted", decision="dec-2024-pres-district-electors")
            continue
        if len(president_votes) == 1:
            winner_person = next(iter(president_votes))
            candidacy = persons_to_candidacy.get((contest, winner_person))
            build.outcomes.append((build.release_id, contest, candidacy, "won_electors", "nara_electoral_votes",
                                   artifact, f"{president_votes[winner_person]} electoral vote(s) cast"))
            if leader is not None and candidacy != leader:
                build.issue("plurality_leader_matches_electors", "warning", "contest", contest,
                            f"{postal}: elector recipient differs from the popular-vote plurality leader.",
                            resolution="open")
        else:
            build.outcomes.append((build.release_id, contest, None, "unresolved", "nara_electoral_votes", artifact,
                                   "Electoral votes split; see electoral votes."))


def _coverage(connection, build: Build, curation, reconciliation) -> None:
    status_by_contest: dict[str, list] = defaultdict(list)
    for row in build.unit_status:
        if row[3] == "county":
            status_by_contest[row[1]].append(row)
    expected = defaultdict(set)
    for row in connection.execute(
            "SELECT gi.value AS fips, rel.parent_version_id FROM geographic_relation rel "
            "JOIN geographic_identifier gi ON gi.geo_version_id = rel.child_version_id AND gi.scheme='county_fips'"):
        expected[row["parent_version_id"]].add(row["fips"])
    states = state_versions(connection)
    components = _decision(curation, "dec-2024-pres-district-electors")["scope"]
    for postal in curation["states_with_electors"]:
        contest = statewide_contest(postal, curation)
        build.coverage.append((build.release_id, contest, "state", "complete", 1, 1, canonical_json([]), "matched",
                               "FEC official compilation of state-certified totals."))
        rows = status_by_contest[contest]
        published_geo = {row[5] for row in rows if row[4] != "unavailable" and row[5]}
        features = {row["feature_id"]: row["source_feature_key"] for row in connection.execute(
            "SELECT feature_id, source_feature_key FROM boundary_feature WHERE boundary_set_id=?", (BOUNDARY_SET_COUNTY,))}
        covered = {features[feature] for feature in published_geo}
        expected_counties = expected[states[postal]["geo_version_id"]]
        missing = sorted(expected_counties - covered)
        complete_units = sum(1 for row in rows if row[4] == "complete")
        all_complete = rows and all(row[4] == "complete" for row in rows)
        boundary = ("matched" if all(row[6] == "matched" for row in rows) and not missing
                    else "boundary_unavailable" if not covered else "partial")
        reconciled = reconciliation[postal]["status"] == "reconciled"
        status = "complete" if all_complete and reconciled else "partial" if rows else "unavailable"
        notes = [] if reconciled else ["county partition differs from official state totals"]
        if postal == "AK":
            notes.append("reported by state house district; district geometry not acquired")
        build.coverage.append((build.release_id, contest, "county", status, len(rows), complete_units,
                               canonical_json(missing[:200]), boundary, "; ".join(notes) or None))
        if postal in components:
            for key in components[postal]:
                if key == "statewide_at_large":
                    continue
                from .pipeline import contest_id
                target = contest_id(curation["election"]["natural_key"], "president", f"president:{postal}:{key.lower()}")
                build.coverage.append((build.release_id, target, "district", "unavailable", 1, 0, canonical_json([key]),
                                       "boundary_unavailable",
                                       "District popular votes are not in Phase 1 sources; elector outcome from NARA."))


def _write_assets(root: DataRoot, connection, build: Build, state_features, county_features, curation) -> list[tuple]:
    directory = root.path / "assets" / build.release_id
    directory.mkdir(parents=True, exist_ok=True)
    states = state_versions(connection)
    wanted_states = {states[postal]["fips"] for postal in curation["states_with_electors"]}
    layers = {
        "states.geojson": [row for row in state_features.values() if row["source_feature_key"] in wanted_states],
        "counties.geojson": [row for row in county_features.values() if row["source_feature_key"][:2] in wanted_states],
    }
    records = []
    for name, rows in layers.items():
        collection = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": row["feature_id"],
             "properties": {"feature_id": row["feature_id"], "geo_version_id": row["geo_version_id"],
                            "geoid": row["source_feature_key"]},
             "geometry": json.loads(row["geometry_json"])} for row in sorted(rows, key=lambda r: r["source_feature_key"])]}
        payload = canonical_json(collection).encode("utf-8")
        target = directory / name
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != hashlib.sha256(payload).hexdigest():
                raise ElectionDataError(f"Immutable asset {target} exists with different content.")
        else:
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        records.append((build.release_id, stable_id("asset", build.release_id, name), root.relative(target),
                        hashlib.sha256(payload).hexdigest(), len(payload), "geojson", "us-government-work"))
    return records


def build_release(root: DataRoot, connection: sqlite3.Connection, *, notes: str | None = None) -> dict[str, Any]:
    curation, curation_hash = load_curation()
    runs = latest_runs(connection)
    decisions = sorted((row[0], row[1]) for row in connection.execute(
        "SELECT decision_id, content_hash FROM curation_decision"))
    # The release identity pins the rule implementation too: any change to this module yields a new release.
    rules_source = hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    manifest = {"runs": {name: [row["run_id"], row["output_hash"]] for name, row in sorted(runs.items())},
                "decisions": decisions, "curation_hash": curation_hash, "rules": RULE_VERSION,
                "rules_source_sha256": rules_source,
                "schema_version": SCHEMA_VERSION, "scope": "us-2024-president"}
    manifest_hash = sha256_text(canonical_json(manifest))
    release_id = f"release_{manifest_hash[:20]}"
    existing = connection.execute("SELECT status FROM data_release WHERE release_id=?", (release_id,)).fetchone()
    if existing and existing[0] in ("staged", "published"):
        return summarize(connection, release_id) | {"build": "unchanged_inputs"}
    if existing:
        _purge(connection, release_id)
    active = connection.execute("SELECT release_id FROM active_release WHERE singleton=1").fetchone()
    with transaction(connection):
        connection.execute("INSERT INTO data_release VALUES (?,?,?,?,?,?,?,?,?)",
                           (release_id, active[0] if active else None, utc_now(), None, "building", SCHEMA_VERSION,
                            manifest_hash, "us-2024-president", notes))
    build = Build(release_id)
    candidacy_of = _candidacy_of_option(connection)
    state_features = _features(connection, BOUNDARY_SET_STATE)
    county_features = _features(connection, BOUNDARY_SET_COUNTY)
    try:
        state_totals = _state_level(connection, build, curation, runs, candidacy_of, state_features)
        raw = _county_units(connection, build, curation, runs, candidacy_of)
        reconciliation = _reconcile(build, raw, state_totals)
        _publish_county_level(connection, build, curation, raw, reconciliation, county_features)
        _outcomes(connection, build, curation, runs, state_totals)
        _coverage(connection, build, curation, reconciliation)
        assets = _write_assets(root, connection, build, state_features, county_features, curation)
        blocking = [issue for issue in build.issues if issue[5] == "error" and issue[11] != "accepted"]
        report_path = root.path / "reports" / release_id / "reconciliation.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps({"release_id": release_id, "states": reconciliation}, indent=2,
                                          sort_keys=True), encoding="utf-8")
        with transaction(connection):
            connection.executemany("INSERT OR IGNORE INTO reporting_unit VALUES (?,?,?,?,?,?,?,?)", build.extra_units)
            connection.executemany("INSERT OR IGNORE INTO derivation VALUES (?,?,?,?,?)", build.derivations.values())
            connection.executemany("INSERT OR IGNORE INTO derivation_input VALUES (?,?,?)", sorted(build.derivation_inputs))
            connection.executemany("INSERT INTO published_result VALUES (?,?,?,?,?,?,?,?,?,?,?)", build.results)
            connection.executemany("INSERT INTO published_unit_status VALUES (?,?,?,?,?,?,?,?,?)", build.unit_status)
            connection.executemany("INSERT INTO release_selection VALUES (?,?,?,?,?,?)", build.selections)
            connection.executemany("INSERT INTO published_outcome VALUES (?,?,?,?,?,?,?)", build.outcomes)
            connection.executemany("INSERT INTO coverage_cell VALUES (?,?,?,?,?,?,?,?,?)", build.coverage)
            connection.executemany("INSERT INTO release_asset VALUES (?,?,?,?,?,?,?)", assets)
            connection.executemany("INSERT INTO validation_issue VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", build.issues)
            connection.executemany("INSERT OR IGNORE INTO release_decision VALUES (?,?)",
                                   [(release_id, decision) for decision, _ in decisions])
            connection.execute(
                "INSERT INTO published_electoral_vote SELECT ?, electoral_vote_id FROM electoral_vote WHERE run_id=?",
                (release_id, runs["nara-electoral-2024"]["run_id"]))
            connection.execute("UPDATE data_release SET status=? WHERE release_id=?",
                               ("failed" if blocking else "staged", release_id))
    except Exception:
        with transaction(connection):
            connection.execute("UPDATE data_release SET status='failed' WHERE release_id=?", (release_id,))
        raise
    return summarize(connection, release_id) | {"build": "built", "blocking_errors": len(blocking)}


def _purge(connection: sqlite3.Connection, release_id: str) -> None:
    with transaction(connection):
        for table in ("published_result", "published_unit_status", "release_selection", "published_outcome",
                      "coverage_cell", "release_asset", "release_decision", "published_electoral_vote"):
            connection.execute(f"DELETE FROM {table} WHERE release_id=?", (release_id,))
        connection.execute("DELETE FROM validation_issue WHERE release_id=?", (release_id,))
        connection.execute("DELETE FROM data_release WHERE release_id=?", (release_id,))


def verify_assets(root: DataRoot, connection: sqlite3.Connection, release_id: str) -> list[str]:
    problems = []
    for row in connection.execute("SELECT path, sha256, byte_length FROM release_asset WHERE release_id=?", (release_id,)):
        path = root.resolve(row["path"])
        if not path.exists():
            problems.append(f"missing asset {row['path']}")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            problems.append(f"asset hash mismatch {row['path']}")
    return problems


def publish_release(root: DataRoot, connection: sqlite3.Connection, release_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT status FROM data_release WHERE release_id=?", (release_id,)).fetchone()
    if row is None:
        raise InvalidInput(f"Unknown release {release_id}.")
    if row[0] == "failed":
        raise ReviewRequired(f"Release {release_id} has blocking validation errors; see 'election-data issues'.")
    if row[0] == "building":
        raise ReviewRequired(f"Release {release_id} did not finish building.")
    if problems := verify_assets(root, connection, release_id):
        raise ElectionDataError(f"Release assets failed verification: {problems}")
    open_warnings = connection.execute(
        "SELECT count(*) FROM validation_issue WHERE release_id=? AND severity='warning' AND resolution_status='open'",
        (release_id,)).fetchone()[0]
    if open_warnings:
        raise ReviewRequired(f"{open_warnings} warning(s) need a policy or curation decision before publication.")
    with transaction(connection):
        if row[0] == "staged":
            connection.execute("UPDATE data_release SET status='published', published_at=? WHERE release_id=?",
                               (utc_now(), release_id))
        connection.execute("INSERT INTO active_release VALUES (1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                           "release_id=excluded.release_id, activated_at=excluded.activated_at", (release_id, utc_now()))
    return summarize(connection, release_id)


def activate_release(root: DataRoot, connection: sqlite3.Connection, release_id: str) -> dict[str, Any]:
    """Roll back (or forward) to an already published release."""
    row = connection.execute("SELECT status FROM data_release WHERE release_id=?", (release_id,)).fetchone()
    if row is None or row[0] != "published":
        raise InvalidInput(f"Release {release_id} is not a published release.")
    if problems := verify_assets(root, connection, release_id):
        raise ElectionDataError(f"Release assets failed verification: {problems}")
    with transaction(connection):
        connection.execute("UPDATE active_release SET release_id=?, activated_at=? WHERE singleton=1",
                           (release_id, utc_now()))
    return summarize(connection, release_id)


def summarize(connection: sqlite3.Connection, release_id: str) -> dict[str, Any]:
    release = connection.execute("SELECT * FROM data_release WHERE release_id=?", (release_id,)).fetchone()
    counts = {severity: count for severity, count in connection.execute(
        "SELECT severity, count(*) FROM validation_issue WHERE release_id=? GROUP BY severity", (release_id,))}
    coverage = {f"{level}:{status}": count for level, status, count in connection.execute(
        "SELECT geography_level, coverage_status, count(*) FROM coverage_cell WHERE release_id=? "
        "GROUP BY geography_level, coverage_status", (release_id,))}
    active = connection.execute("SELECT release_id FROM active_release WHERE singleton=1").fetchone()
    return {"release_id": release_id, "status": release["status"], "parent_release_id": release["parent_release_id"],
            "manifest_hash": release["manifest_hash"], "active": bool(active and active[0] == release_id),
            "issues": counts, "coverage": coverage,
            "published_results": connection.execute("SELECT count(*) FROM published_result WHERE release_id=?",
                                                    (release_id,)).fetchone()[0]}


def diff_releases(connection: sqlite3.Connection, old: str, new: str, *, limit: int = 100) -> dict[str, Any]:
    def results(release):
        return {(row[0], row[1], row[2]): row[3] for row in connection.execute(
            "SELECT contest_id, unit_id, candidacy_id, votes FROM published_result WHERE release_id=?", (release,))}

    def outcomes(release):
        return {(row[0], row[1]): row[2] for row in connection.execute(
            "SELECT contest_id, outcome_type, candidacy_id FROM published_outcome WHERE release_id=?", (release,))}

    def coverage(release):
        return {(row[0], row[1]): row[2] for row in connection.execute(
            "SELECT contest_id, geography_level, coverage_status FROM coverage_cell WHERE release_id=?", (release,))}

    changes: dict[str, list] = {}
    for name, loader in (("results", results), ("outcomes", outcomes), ("coverage", coverage)):
        before, after = loader(old), loader(new)
        delta = [{"key": list(key), "before": before.get(key), "after": after.get(key)}
                 for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
        changes[name] = delta[:limit] + ([{"truncated": len(delta) - limit}] if len(delta) > limit else [])
    return {"from": old, "to": new, **changes}
