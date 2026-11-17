"""Candidate release build, validation gates, assets, atomic publication and rollback.

A release selects coherent source result sets under curation decisions, derives
serving rows with recorded inputs, reconciles county partitions to selected contest
totals, compares what exists with the expected-contest inventory, and writes immutable
content-addressed map assets. Publication switches ``active_release`` in one transaction
only after every referenced asset is verified.
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
from .adapters import nara
from .core import (DataRoot, ElectionDataError, InvalidInput, ReviewRequired, canonical_json, sha256_text,
                   stable_id, transaction, utc_now)
from .federal import (FIRST_MODERN_CYCLE, Registrar, congress_for_election, decision, expected_contests,
                      load_federal, president_seats, states)
from .importer import ADAPTERS, REQUIRED, latest_runs
from .pipeline import candidacy_id, contest_id, load_curation, person_id, state_versions, unit_id

RULE_VERSION = "release-rules-v2"
RULES = {
    "state_total_equals_option_sum": 2,
    "county_partition_reconciles_to_state": 2,
    "batch_sum_matches_totalvotes_column": 2,
    "unit_without_total_mode": 2,
    "unit_missing_option_count": 1,
    "unit_merge_requires_reconciliation": 1,
    "rcv_first_round_majority": 1,
    "electoral_components_match": 1,
    "fec_electoral_votes_match_nara": 1,
    "plurality_leader_matches_electors": 1,
    "contest_missing_option_count": 1,
    "expected_contest_missing": 1,
}
CONTEST_TOTAL_SOURCES = ("fec-president-2024", "mit-state-president", "mit-senate", "mit-house")
COUNTY_SOURCE = "mit-county-president"
BOUNDARY_SET_STATE = stable_id("boundary_set", "census", "2020", "cb_2020_us_state_20m")
BOUNDARY_SET_COUNTY = stable_id("boundary_set", "census", "2020", "cb_2020_us_county_20m")
COUNTY_VINTAGE_CYCLES = (2020, 2024)
LAST_INVENTORY_CYCLE = 2026


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
    inventory: list[tuple] = field(default_factory=list)
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


@dataclass
class Context:
    connection: sqlite3.Connection
    runs: dict[str, sqlite3.Row]
    contests: dict[str, sqlite3.Row]
    candidacy_of: dict[str, str]
    states: dict[str, sqlite3.Row]
    state_features: dict[str, sqlite3.Row]
    county_features: dict[str, sqlite3.Row]
    district_features: dict[str, str]
    curation: dict[str, Any]
    ticket_candidacies: set[str]

    def postal(self, contest: str) -> str:
        return self.contests[contest]["seat_key"].split(":")[1]


def _curated(curation: dict, decision_id: str) -> dict:
    return next(item for item in curation["decisions"] if item["decision_id"] == decision_id)


def _features(connection: sqlite3.Connection, boundary_set: str) -> dict[str, sqlite3.Row]:
    return {row["geo_version_id"]: row for row in connection.execute(
        "SELECT feature_id, geo_version_id, source_feature_key FROM boundary_feature WHERE boundary_set_id=?",
        (boundary_set,))}


def _district_features(connection: sqlite3.Connection) -> dict[str, str]:
    """Map '<congress>:<postal>:<district>' to the feature drawn for that Congress."""
    features = {}
    for row in connection.execute(
            "SELECT gi.value, bf.feature_id, bs.vintage FROM geographic_identifier gi "
            "JOIN boundary_feature bf ON bf.geo_version_id = gi.geo_version_id "
            "JOIN boundary_set bs ON bs.boundary_set_id = bf.boundary_set_id "
            "WHERE gi.scheme='cd_congress' AND bs.vintage LIKE 'congress-%'"):
        wanted, drawn = int(row["value"].split(":")[0]), int(row["vintage"].split("-")[1])
        fallback = decision("dec-fed-district-boundaries")["scope"]["congress_117_fallback"]
        if wanted == drawn or (wanted == 117 and drawn == fallback["use"]):
            features[row["value"]] = row["feature_id"]
    return features


def _votes_by_set(connection: sqlite3.Connection, run_id: str) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
            "SELECT v.observation_id, v.set_id, v.unit_id, v.option_id, v.round_key, v.voting_mode, v.batch_key, "
            "v.votes, v.value_status FROM vote_observation v JOIN source_result_set s USING (set_id) "
            "WHERE s.run_id=? ORDER BY v.set_id, v.unit_id, v.option_id, v.voting_mode, v.batch_key", (run_id,)):
        grouped[row["set_id"]].append(row)
    return grouped


def _metrics_by_set(connection: sqlite3.Connection, run_id: str) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(
            "SELECT m.metric_id, m.set_id, m.unit_id, m.voting_mode, m.metric_code, m.integer_value "
            "FROM metric_observation m JOIN source_result_set s USING (set_id) WHERE s.run_id=?", (run_id,)):
        grouped[row["set_id"]].append(row)
    return grouped


def _sets(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return connection.execute("SELECT set_id, contest_id, source_id, source_status FROM source_result_set "
                              "WHERE run_id=? ORDER BY contest_id", (run_id,)).fetchall()


# --- contest totals --------------------------------------------------------------------------------
def _contest_totals(ctx: Context, build: Build) -> dict[str, dict]:
    """Publish each contest's total from the highest-priority source that reports it."""
    totals: dict[str, dict] = {}
    for name in CONTEST_TOTAL_SOURCES:
        run = ctx.runs.get(name)
        if run is None:
            continue
        votes, metrics = _votes_by_set(ctx.connection, run["run_id"]), _metrics_by_set(ctx.connection, run["run_id"])
        for source_set in _sets(ctx.connection, run["run_id"]):
            contest = source_set["contest_id"]
            if contest in totals:
                continue  # a higher-priority source already supplies this contest; both stay stored
            rows = votes.get(source_set["set_id"], [])
            if not rows:
                continue
            meta = ctx.contests[contest]
            unit = rows[0]["unit_id"]
            values: dict[str, int | None] = {}
            lines: dict[str, int] = defaultdict(int)
            repeated = False
            for row in rows:
                candidacy = ctx.candidacy_of[row["option_id"]]
                lines[candidacy] += row["batch_key"] == "b1"
                repeated = repeated or row["batch_key"] != "b1"
                if row["value_status"] != "reported" or values.get(candidacy, 0) is None:
                    values[candidacy] = None
                else:
                    values[candidacy] = values.get(candidacy, 0) + row["votes"]
            complete = all(value is not None for value in values.values())
            option_sum = sum(value or 0 for value in values.values())
            denominator = option_sum if complete and option_sum > 0 else None
            reported_total = next((item for item in metrics.get(source_set["set_id"], []) if item["metric_code"] in
                                   ("total_votes_all_options", "source_total_votes")), None)
            other_metrics = sum(item["integer_value"] or 0 for item in metrics.get(source_set["set_id"], [])
                                if item["metric_code"] not in ("total_votes_all_options", "source_total_votes"))
            notes = []
            if any(count > 1 for count in lines.values()):
                notes.append("party lines summed per candidate")
            if repeated:
                notes.append("repeated source rows summed")
            if rows[0]["round_key"] == "unknown":
                notes.append("ranked-choice contest; the source does not state which tabulation round it reports")
            if source_set["source_status"] == "unofficial":
                notes.append("source marks these returns unofficial")
            if not complete:
                notes.append("one or more option counts missing; shares withheld")
                build.issue("contest_missing_option_count", "warning", "contest", contest,
                            f"{meta['label']}: a count is missing in the source; shares withheld.",
                            resolution="accepted", decision="dec-fed-mit-state-format")
            elif reported_total is not None and reported_total["integer_value"] not in (None, option_sum,
                                                                                         option_sum + other_metrics):
                notes.append("source total differs from the sum of its listed options")
                build.issue("state_total_equals_option_sum", "warning", "contest", contest,
                            f"{meta['label']}: source total differs from the sum of listed options; shares use the option sum.",
                            expected=reported_total["integer_value"], actual=option_sum, resolution="accepted",
                            decision="dec-fed-fusion-sum" if name != "fec-president-2024" else "dec-2024-pres-source-priority")
            inputs = [("vote_observation", row["observation_id"]) for row in rows]
            if reported_total is not None:
                inputs.append(("metric_observation", reported_total["metric_id"]))
            level = "district" if meta["code"] in ("house", "house_delegate") else "state"
            derivation = build.derive(contest, unit, level, inputs, {
                "rule": "contest_total_from_selected_source", "source": source_set["source_id"],
                "fusion": "sum_party_lines_per_candidacy", "denominator": "all_reported_options"})
            for candidacy, value in sorted(values.items()):
                build.results.append((build.release_id, contest, unit, candidacy, rows[0]["round_key"], "all", value,
                                      denominator, "all_reported_options" if denominator else None,
                                      "reported" if value is not None else "missing", derivation))
            if level == "state":
                feature = ctx.state_features.get(meta["scope_geo_version_id"])
                feature_id = feature["feature_id"] if feature else None
            else:
                identifier = ctx.connection.execute("SELECT source_identifier FROM reporting_unit WHERE unit_id=?",
                                                    (unit,)).fetchone()[0]
                feature_id = ctx.district_features.get(identifier)
                if feature_id is None:
                    notes.append("no district boundary acquired for this Congress")
            build.unit_status.append((build.release_id, contest, unit, level, "complete" if complete else "partial",
                                      feature_id, "matched" if feature_id else "boundary_unavailable",
                                      source_set["source_status"], canonical_json(notes)))
            build.selections.append((build.release_id, contest, unit, "contest_total", source_set["set_id"],
                                     "dec-fed-source-priority" if name != "fec-president-2024"
                                     else "dec-2024-pres-source-priority"))
            build.coverage.append((build.release_id, contest, level, "complete" if complete else "partial", 1,
                                   int(complete), canonical_json([]), "matched" if feature_id else "boundary_unavailable",
                                   {"fec-president-2024": "FEC official compilation of state-certified totals."}.get(
                                       name, "MIT Election Lab state-level file (secondary compilation).")))
            totals[contest] = {"contest": contest, "unit": unit, "candidacies": values, "total": denominator,
                               "option_sum": option_sum, "round_key": rows[0]["round_key"], "complete": complete,
                               "source": name, "matched": bool(feature_id)}
    return totals


# --- county detail ---------------------------------------------------------------------------------
def _select_unit_total(modes: dict[str, dict[str, list]], totalvotes: dict[str, sqlite3.Row]):
    """Apply dec-fed-county-total-selection; returns (rows_by_option, notes, inputs) or (None, reason, [])."""
    if "all" in modes:
        chosen, label = {"all": modes["all"]}, "all"
    elif len(modes) == 1:
        (mode, options), = modes.items()
        chosen, label = {mode: options}, mode
    else:
        chosen, label = modes, "mode_sum"
    merged: dict[str, list] = defaultdict(list)
    for options in chosen.values():
        for option, batches in options.items():
            merged[option].extend(batches)
    repeated = any(len(batches) > 1 for options in chosen.values() for batches in options.values())
    needs_check = repeated or label == "mode_sum"
    metric_values = {totalvotes[mode]["integer_value"] for mode in chosen if mode in totalvotes}
    notes, inputs = [], []
    if needs_check:
        unit_sum = sum(row["votes"] or 0 for batches in merged.values() for row in batches)
        if len(metric_values) != 1 or unit_sum not in metric_values:
            reason = (f"no all-mode total (modes: {sorted(modes)})" if label == "mode_sum"
                      else "repeated source rows not reconcilable")
            return None, reason, [], label, (sorted(metric_values), unit_sum)
        inputs.extend(("metric_observation", totalvotes[mode]["metric_id"]) for mode in chosen if mode in totalvotes)
        if repeated:
            notes.append("repeated source rows summed after matching the MIT totalvotes column")
        if label == "mode_sum":
            notes.append("voting modes summed after matching the MIT totalvotes column")
    return merged, notes, inputs, label, None


def _county_units(ctx: Context, build: Build) -> dict[str, dict[str, dict]]:
    """Per contest, each raw MIT unit's selected all-mode tally (or a withheld reason)."""
    run = ctx.runs.get(COUNTY_SOURCE)
    if run is None:
        return {}
    votes, metrics = _votes_by_set(ctx.connection, run["run_id"]), _metrics_by_set(ctx.connection, run["run_id"])
    unit_rows = {row["unit_id"]: row for row in ctx.connection.execute(
        "SELECT * FROM reporting_unit WHERE source_identifier LIKE 'mit:%'")}
    result: dict[str, dict[str, dict]] = {}
    for source_set in _sets(ctx.connection, run["run_id"]):
        contest, set_id = source_set["contest_id"], source_set["set_id"]
        postal = ctx.postal(contest)
        totalvotes: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
        for item in metrics.get(set_id, []):
            if item["metric_code"] == "mit_totalvotes_column":
                totalvotes[item["unit_id"]][item["voting_mode"]] = item
        grouped: dict[str, dict[str, dict[str, list]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
        for row in votes.get(set_id, []):
            grouped[row["unit_id"]][row["voting_mode"]][row["option_id"]].append(row)
        units: dict[str, dict] = {}
        for unit, modes in grouped.items():
            unit_row = unit_rows[unit]
            first = next(iter(next(iter(modes.values())).values()))[0]
            entry = {"contest": contest, "set_id": set_id, "postal": postal, "unit": unit, "kind": unit_row["kind"],
                     "natural_key": unit_row["natural_key"], "geo_version_id": unit_row["geo_version_id"],
                     "label": unit_row["label"], "values": {}, "inputs": [],
                     "notes": [unit_row["notes"]] if unit_row["notes"] else [], "status": "complete",
                     "round_key": first["round_key"]}
            units[unit] = entry
            merged, notes, inputs, label, problem = _select_unit_total(modes, totalvotes[unit])
            if merged is None:
                rule = "unit_without_total_mode" if label == "mode_sum" else "batch_sum_matches_totalvotes_column"
                build.issue(rule, "warning", "reporting_unit", f"{contest}:{unit}",
                            f"{postal} {unit_row['label']}: {notes}; unit withheld.", expected=problem[0],
                            actual=problem[1], resolution="accepted", decision="dec-fed-county-total-selection")
                entry["status"], entry["notes"] = "unavailable", entry["notes"] + [notes]
                continue
            entry["mode"] = label
            entry["notes"] += notes
            entry["inputs"] += inputs
            for option, batches in merged.items():
                candidacy = ctx.candidacy_of[option]
                entry["inputs"].extend(("vote_observation", row["observation_id"]) for row in batches)
                if any(row["value_status"] != "reported" for row in batches) or entry["values"].get(candidacy, 0) is None:
                    entry["values"][candidacy] = None
                    entry["status"] = "partial"
                else:
                    entry["values"][candidacy] = entry["values"].get(candidacy, 0) + sum(row["votes"] for row in batches)
            if entry["status"] == "partial":
                build.issue("unit_missing_option_count", "warning", "reporting_unit", f"{contest}:{unit}",
                            f"{postal} {unit_row['label']}: at least one option count is missing; shares withheld.",
                            resolution="accepted", decision="dec-fed-county-total-selection")
                entry["notes"].append("one or more option counts missing; shares withheld")
        result[contest] = units
    return result


def _reconcile(ctx: Context, build: Build, county: dict[str, dict[str, dict]], totals: dict[str, dict]) -> dict:
    """Compare every contest's county partition with its selected contest total."""
    report: dict[str, dict] = {}
    for contest, units in sorted(county.items()):
        meta, postal = ctx.contests[contest], ctx.postal(contest)
        official = totals.get(contest)
        complete = all(entry["status"] == "complete" for entry in units.values())
        sums: dict[str, int] = defaultdict(int)
        for entry in units.values():
            for candidacy, value in entry["values"].items():
                sums[candidacy] += value or 0
        county_total = sum(sums.values())
        if official is None:
            report[contest] = {"state": postal, "cycle": meta["cycle_year"], "status": "no_contest_total",
                               "complete_partition": complete, "county_total_all_options": county_total}
            continue
        compared = {candidacy: {"official": votes, "county_sum": sums[candidacy], "difference": sums[candidacy] - votes}
                    for candidacy, votes in official["candidacies"].items() if candidacy in sums and votes is not None}
        differences = {key: value for key, value in compared.items() if value["difference"]}
        reconciled = complete and not differences and county_total == official["option_sum"]
        named_match = complete and bool(compared) and not any(
            value["difference"] for key, value in compared.items() if key in ctx.ticket_candidacies)
        report[contest] = {"state": postal, "cycle": meta["cycle_year"], "status": "reconciled" if reconciled else "differs",
                           "named_tickets_match": named_match,
                           "complete_partition": complete, "candidacies": compared, "compared_source": official["source"],
                           "county_total_all_options": county_total, "official_total_all_options": official["option_sum"]}
        if reconciled:
            build.issue("county_partition_reconciles_to_state", "info", "contest", contest,
                        f"{meta['cycle_year']} {postal}: county partition equals the selected state totals.")
        else:
            detail = ("named tickets match exactly and only the residual 'other' category differs" if named_match
                      else "a named ticket's county sum differs from its state total")
            build.issue("county_partition_reconciles_to_state", "warning", "contest", contest,
                        f"{meta['cycle_year']} {postal}: county partition does not equal the selected state totals "
                        f"({detail}); county view is published with this difference disclosed.",
                        expected={"total": official["option_sum"]},
                        actual={"total": county_total, "differences": differences}, resolution="accepted",
                        decision="dec-fed-source-priority")
    return report


def _publish_county_level(ctx: Context, build: Build, county: dict[str, dict[str, dict]], reconciliation: dict) -> None:
    merge = _curated(ctx.curation, "dec-2024-pres-kansas-city-merge")["scope"]
    target_natural = f"county:{merge['target_fips']}"
    for contest, units in sorted(county.items()):
        cycle = ctx.contests[contest]["cycle_year"]
        reconciled = reconciliation[contest]["status"] == "reconciled"
        members = {entry["natural_key"]: entry for entry in units.values() if entry["natural_key"].startswith(
            (f"county-part:{merge['target_fips']}", f"election-jurisdiction:{ctx.postal(contest)}:"))}
        published = [entry for entry in units.values() if entry["natural_key"] not in members]
        if len(members) == 1 and next(iter(members)).startswith("county-part:"):
            # No separately reported jurisdiction in this cycle: the row is the whole county.
            entry = next(iter(members.values()))
            geo = ctx.connection.execute("SELECT geo_version_id FROM geographic_identifier WHERE scheme='county_fips' "
                                         "AND value=?", (merge["target_fips"],)).fetchone()[0]
            build.extra_units.append((unit_id(target_natural), "county", geo, unit_id(f"state:{merge['target_fips'][:2]}"),
                                      "Jackson County", f"mit:{merge['target_fips']}", target_natural, None))
            published.append({**entry, "unit": unit_id(target_natural), "geo_version_id": geo, "kind": "county",
                              "notes": ["reported as a whole county in this cycle"]})
        elif members:
            merge_ok = (len(members) == len(merge["members"]) and reconciled
                        and all(entry["status"] == "complete" for entry in members.values()))
            target_unit = unit_id(target_natural)
            if merge_ok:
                geo = ctx.connection.execute("SELECT geo_version_id FROM geographic_identifier WHERE "
                                             "scheme='county_fips' AND value=?", (merge["target_fips"],)).fetchone()[0]
                build.extra_units.append((target_unit, "county", geo, unit_id(f"state:{merge['target_fips'][:2]}"),
                                          "Jackson County (merged reporting units)",
                                          f"merge:{'+'.join(merge['members'])}", target_natural,
                                          "Documented merge of nonoverlapping reporting units."))
                values: dict[str, int] = defaultdict(int)
                inputs = []
                for entry in members.values():
                    for candidacy, value in entry["values"].items():
                        values[candidacy] += value
                    inputs.extend(entry["inputs"])
                sample = next(iter(members.values()))
                published.append({**sample, "unit": target_unit, "values": dict(values), "inputs": inputs,
                                  "geo_version_id": geo, "kind": "county", "label": "Jackson County", "mode": "merged",
                                  "notes": ["merge of Jackson County election authority and Kansas City (MIT 36000)"]})
                build.issue("unit_merge_requires_reconciliation", "info", "reporting_unit", f"{contest}:{target_unit}",
                            f"{cycle}: Kansas City merge accepted: Missouri partition reconciles to the state total.",
                            resolution="accepted", decision="dec-2024-pres-kansas-city-merge")
            else:
                published.extend(members.values())
                build.issue("unit_merge_requires_reconciliation", "warning", "reporting_unit", f"{contest}:{target_unit}",
                            f"{cycle}: Kansas City merge withheld: Missouri partition does not reconcile; members "
                            "published as separate units without geometry.", resolution="accepted",
                            decision="dec-2024-pres-kansas-city-merge")
        rows = []
        covered = set()
        for entry in published:
            unit = entry["unit"]
            feature = ctx.county_features.get(entry["geo_version_id"]) if entry["geo_version_id"] else None
            boundary = ("boundary_unavailable" if feature is None else
                        "matched" if cycle in COUNTY_VINTAGE_CYCLES else "reference_vintage")
            notes = list(entry["notes"])
            if boundary == "reference_vintage":
                notes.append("joined to a 2020-vintage county polygon for reference; not the historical boundary")
            if not reconciled:
                notes.append("state county partition differs from the selected state totals")
            if feature is not None and entry["status"] != "unavailable":
                covered.add(feature["source_feature_key"])
            if entry["status"] == "unavailable":
                rows.append((build.release_id, contest, unit, "county", "unavailable",
                             feature["feature_id"] if feature else None, boundary, "secondary_compilation",
                             canonical_json(notes)))
                continue
            complete = entry["status"] == "complete"
            denominator = sum(entry["values"].values()) if complete else None
            denominator = denominator or None
            derivation = build.derive(contest, unit, "county", entry["inputs"], {
                "rule": "mit_county_total_selection", "mode": entry.get("mode"),
                "decisions": ["dec-fed-county-total-selection"], "denominator": "all_reported_options"})
            for candidacy, value in sorted(entry["values"].items()):
                build.results.append((build.release_id, contest, unit, candidacy, entry["round_key"], "all", value,
                                      denominator, "all_reported_options" if denominator else None,
                                      "reported" if value is not None else "missing", derivation))
            rows.append((build.release_id, contest, unit, "county", "complete" if complete else "partial",
                         feature["feature_id"] if feature else None, boundary, "secondary_compilation",
                         canonical_json(notes)))
            build.selections.append((build.release_id, contest, unit, "county_detail", entry["set_id"],
                                     "dec-fed-county-total-selection"))
        build.unit_status.extend(rows)
        expected = {row["source_feature_key"] for row in ctx.county_features.values()
                    if row["source_feature_key"][:2] == ctx.states[ctx.postal(contest)]["fips"]}
        missing = sorted(expected - covered)
        complete_units = sum(1 for row in rows if row[4] == "complete")
        all_complete = bool(rows) and complete_units == len(rows)
        boundary = ("boundary_unavailable" if not covered else
                    "partial" if missing or any(row[6] == "boundary_unavailable" for row in rows) else
                    "matched" if cycle in COUNTY_VINTAGE_CYCLES else "reference_vintage")
        notes = [] if reconciled else [
            "county partition differs from the selected state totals"
            + (" only in the residual 'other' category; named tickets match" if reconciliation[contest].get(
                "named_tickets_match") else "")]
        if ctx.postal(contest) == "AK":
            notes.append("reported by state house district; district geometry not acquired")
        build.coverage.append((build.release_id, contest, "county", "complete" if all_complete and reconciled else "partial",
                               len(rows), complete_units, canonical_json(missing[:200]), boundary,
                               "; ".join(notes) or None))


# --- outcomes --------------------------------------------------------------------------------------
def _electoral_by_state(ctx: Context) -> dict[tuple[int, str], dict]:
    """(cycle, state geo version) -> {'votes': {person_id: n}, 'artifact': id, 'labels': {person_id: name}}."""
    result: dict[tuple[int, str], dict] = {}
    for year in nara.YEARS:
        run = ctx.runs.get(f"nara-electoral-{year}")
        if run is None:
            continue
        for row in ctx.connection.execute(
                "SELECT ev.recipient_person_id, ev.votes, ea.geo_version_id, ea.source_artifact_id, p.canonical_name "
                "FROM electoral_vote ev JOIN electoral_allocation ea USING (allocation_id) "
                "LEFT JOIN person p ON p.person_id = ev.recipient_person_id "
                "WHERE ev.run_id=? AND ev.vote_role='president' AND ea.allocation_kind='state_total'", (run["run_id"],)):
            entry = result.setdefault((year, row["geo_version_id"]), {"votes": defaultdict(int), "labels": {},
                                                                       "artifact": row["source_artifact_id"]})
            entry["votes"][row["recipient_person_id"]] += row["votes"]
            entry["labels"][row["recipient_person_id"]] = row["canonical_name"] or "other recipients"
    return result


def _outcomes(ctx: Context, build: Build, totals: dict[str, dict]) -> None:
    electoral = _electoral_by_state(ctx)
    ticket_candidacy = {(row["contest_id"], row["person_id"]): row["candidacy_id"] for row in ctx.connection.execute(
        "SELECT c.candidacy_id, c.contest_id, m.person_id FROM candidacy c JOIN candidacy_member m USING (candidacy_id) "
        "WHERE m.role='president'")}
    runoffs = {(ctx.contests[contest]["code"], ctx.contests[contest]["seat_key"], ctx.contests[contest]["cycle_year"],
                ctx.contests[contest]["contest_type"]) for contest in totals if ctx.contests[contest]["stage"] == "runoff"}
    components = _curated(ctx.curation, "dec-2024-pres-district-electors")["scope"]
    fec_run = ctx.runs.get("fec-president-2024")
    fec_columns = {entry["state"]: entry for entry in json.loads(fec_run["diagnostics_json"])[0]["fec_electoral_columns"]} \
        if fec_run else {}
    for contest, total in sorted(totals.items()):
        meta, postal = ctx.contests[contest], ctx.postal(contest)
        leader = None
        if total["complete"] and total["total"]:
            ranked = sorted(total["candidacies"].items(), key=lambda item: (-item[1], item[0]))
            leader, leader_votes = ranked[0]
            tie = len(ranked) > 1 and ranked[1][1] == leader_votes
            share = leader_votes / total["total"]
            notes = "Derived popular-vote plurality from the selected source; not a certified winner."
            key = (meta["code"], meta["seat_key"], meta["cycle_year"], meta["contest_type"])
            if meta["stage"] == "general" and key in runoffs:
                notes += " This contest proceeded to a runoff, recorded as a separate linked stage."
            if meta["voting_system"] == "ranked_choice":
                if total["round_key"] == "round_1" and share > 0.5:
                    build.issue("rcv_first_round_majority", "info", "contest", contest,
                                f"{meta['cycle_year']} {postal}: first-round leader has {share:.4f} of reported options; "
                                "no further round needed.", resolution="accepted",
                                decision="dec-2024-pres-rcv-first-round")
                elif total["round_key"] == "round_1":
                    build.issue("rcv_first_round_majority", "error", "contest", contest,
                                f"{meta['cycle_year']} {postal}: first-round leader lacks a majority; round tabulation required.")
                    leader = None
                else:
                    notes += " Ranked-choice contest: the reported tally's round is unknown, so the leader may not be the winner."
            if leader is not None:
                build.outcomes.append((build.release_id, contest, None if tie else leader,
                                       "unresolved" if tie else "plurality_leader", "derived_from_selected_source", None,
                                       notes))
                if tie:
                    leader = None
        if meta["code"] != "president" or ":cd" in meta["seat_key"] or meta["stage"] != "general":
            continue
        cast = electoral.get((meta["cycle_year"], meta["scope_geo_version_id"]))
        if cast is None:
            continue
        votes, artifact = dict(cast["votes"]), cast["artifact"]
        if meta["cycle_year"] == 2024 and postal in fec_columns:
            fec = fec_columns[postal]
            fec_votes = {person_id("donald-j-trump"): int(fec["trump"] or 0),
                         person_id("kamala-d-harris"): int(fec["harris"] or 0)}
            if {key: value for key, value in fec_votes.items() if value} != votes:
                build.issue("fec_electoral_votes_match_nara", "error", "contest", contest,
                            f"{postal}: FEC electoral vote columns differ from NARA certificates.",
                            expected=votes, actual=fec_votes)
        if meta["cycle_year"] == 2024 and postal in components:
            parts = components[postal]
            winners: dict[str, int] = defaultdict(int)
            for part in parts.values():
                winners[person_id(part["winner"])] += part["electors"]
            if dict(winners) != votes:
                build.issue("electoral_components_match", "error", "contest", contest,
                            f"{postal}: curated district components do not sum to NARA electoral votes.",
                            expected=votes, actual=dict(winners))
            else:
                build.issue("electoral_components_match", "info", "contest", contest,
                            f"{postal}: district components match NARA electoral votes.", resolution="accepted",
                            decision="dec-2024-pres-district-electors")
            for key, part in parts.items():
                seat = f"president:{postal}:at-large" if key == "statewide_at_large" else f"president:{postal}:{key.lower()}"
                target = contest_id(ctx.contests[contest]["election_key"], "president", seat)
                if (target, person_id(part["winner"])) not in ticket_candidacy:
                    # The curated district winner must be a candidacy in that district's elector contest.
                    natural = Registrar(ctx.connection).ticket(target, 2024, part["winner"])
                    ticket_candidacy[(target, person_id(part["winner"]))] = candidacy_id(target, natural)
                build.outcomes.append((build.release_id, target, ticket_candidacy.get((target, person_id(part["winner"]))),
                                       "won_electors", "nara_certificate_note_curated", artifact,
                                       f"{part['electors']} elector(s); dec-2024-pres-district-electors"))
            continue
        ranked_votes = sorted(votes.items(), key=lambda item: (-item[1], str(item[0])))
        if not ranked_votes:
            continue
        winner, count = ranked_votes[0]
        if len(ranked_votes) > 1 and ranked_votes[1][1] == count:
            build.outcomes.append((build.release_id, contest, None, "unresolved", "nara_electoral_votes", artifact,
                                   "Electoral votes evenly split; see electoral votes."))
            continue
        candidacy = ticket_candidacy.get((contest, winner))
        split = "; ".join(f"{cast['labels'][person]} {number}" for person, number in ranked_votes)
        note = f"{count} electoral vote(s) cast" if len(ranked_votes) == 1 else f"Electoral votes as cast: {split}"
        if candidacy is None:
            note += f" (recipient {cast['labels'][winner]} is not a candidacy in the selected popular-vote source)"
        build.outcomes.append((build.release_id, contest, candidacy, "won_electors", "nara_electoral_votes", artifact, note))
        if leader is not None and candidacy is not None and candidacy != leader:
            build.issue("plurality_leader_matches_electors", "warning", "contest", contest,
                        f"{meta['cycle_year']} {postal}: elector recipient differs from the popular-vote plurality leader.")


# --- coverage inventory ----------------------------------------------------------------------------
def _inventory(ctx: Context, build: Build, totals: dict[str, dict], reconciliation: dict) -> None:
    published: dict[tuple[str, int], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    unmatched: dict[tuple[str, int], list[str]] = defaultdict(list)
    for contest, total in totals.items():
        meta = ctx.contests[contest]
        kind = "regular_general" if (meta["stage"], meta["contest_type"]) == ("general", "regular") else \
            f"{meta['contest_type']}_{meta['stage']}"
        published[(meta["code"], meta["cycle_year"])][kind].add(meta["seat_key"])
        if meta["code"] == "house" and not total["matched"]:
            unmatched[(meta["code"], meta["cycle_year"])].append(meta["seat_key"])
    history = "dec-fed-history-inventory"

    def add(office, year, series, status, expected, available, missing=(), extra=None, notes=None):
        build.inventory.append((build.release_id, office, year, series, status, expected, available,
                                canonical_json(sorted(missing)), canonical_json(extra or {}), notes))

    for year in range(1788, LAST_INVENTORY_CYCLE + 1, 2):
        for office in ("president", "senate", "house"):
            if office == "president" and (year % 4 or year == 1788):
                continue
            if year < FIRST_MODERN_CYCLE:
                if office == "senate" and year < 1914:
                    add(office, year, "contest_totals", "not_applicable", None, 0,
                        notes=f"Senators were chosen by state legislatures before the 17th Amendment ({history}).")
                else:
                    add(office, year, "contest_totals", "not_researched", None, 0,
                        notes=f"Popular-vote returns before 1976 are not researched in this release ({history}).")
                continue
            expected = expected_contests(office, year)
            if office == "president":
                expected = [seat for seat in expected if ":cd" not in seat]
            kinds = published.get((office, year), {})
            regular = kinds.get("regular_general", set())
            special = set().union(*[seats for kind, seats in kinds.items() if kind.startswith("special_")]) if kinds else set()
            available = [seat for seat in expected if seat in regular or seat in special]
            missing = [seat for seat in expected if seat not in regular and seat not in special]
            by_special = [seat for seat in expected if seat not in regular and seat in special]
            extra = {kind: len(seats) for kind, seats in sorted(kinds.items()) if kind != "regular_general"}
            notes = []
            if by_special:
                notes.append(f"{len(by_special)} expected seat(s) are recorded only as special elections")
            if office == "president":
                districts = sum(len(president_seats(postal, year)) - 1 for postal in states()
                                if len(president_seats(postal, year)) > 1)
                if districts:
                    notes.append(f"{districts} Maine/Nebraska district elector contests have no popular-vote source")
            if not kinds:
                status = "not_researched"
                notes.append("No results source for this cycle has been imported"
                             + ("; 2026 returns were still being certified at build time" if year == 2026 else ""))
            else:
                status = "complete" if not missing else "partial"
                for seat in missing:
                    build.issue("expected_contest_missing", "warning", "contest_inventory", f"{office}:{year}:{seat}",
                                f"{year} {seat}: expected regular contest is absent from every imported source.",
                                resolution="accepted", decision="dec-fed-source-priority")
            add(office, year, "contest_totals", status, len(expected), len(available), missing, extra,
                "; ".join(notes) or None)
            if office == "house" and kinds:
                bad = unmatched.get((office, year), [])
                total = sum(len(seats) for seats in kinds.values())
                add(office, year, "boundaries", "complete" if not bad else "unavailable" if len(bad) == total else "partial",
                    total, total - len(bad), bad, notes=f"districts of the {congress_for_election(year)}th Congress")
            if office == "president":
                cycle = {contest: entry for contest, entry in reconciliation.items() if entry["cycle"] == year}
                if cycle:
                    reconciled = sum(entry["status"] == "reconciled" for entry in cycle.values())
                    named = sum(bool(entry.get("named_tickets_match")) for entry in cycle.values())
                    add(office, year, "county_detail", "complete" if reconciled == len(cycle) == len(expected) else "partial",
                        len(expected), reconciled,
                        [entry["state"] for entry in cycle.values() if not entry.get("named_tickets_match")],
                        {"states_reconciled_exactly": reconciled, "states_where_named_tickets_match": named},
                        "available = states whose county partition equals the selected state total exactly; "
                        "missing lists states where a named ticket differs")
                else:
                    add(office, year, "county_detail", "not_researched", len(expected), 0,
                        notes="County returns before 2000 are not in an imported source.")
            elif kinds:
                add(office, year, "county_detail", "not_researched", None, 0,
                    notes="County and precinct detail for congressional contests needs per-state sources.")
    for year in nara.YEARS:
        run = ctx.runs.get(f"nara-electoral-{year}")
        if run is not None:
            electors = json.loads(run["row_accounting_json"])["electors"]
            add("president", year, "electoral_votes", "complete", electors, electors,
                notes="Actual electoral votes as cast (NARA).")
        else:
            add("president", year, "electoral_votes", "not_researched", None, 0,
                notes="NARA page not imported or awaiting layout review.")


# --- assets ----------------------------------------------------------------------------------------
def _asset(root: DataRoot, build: Build, kind: str, rows, license_id: str) -> tuple:
    collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": row["feature_id"],
         "properties": {"feature_id": row["feature_id"], "geo_version_id": row["geo_version_id"],
                        "key": row["source_feature_key"]},
         "geometry": json.loads(row["geometry_json"])} for row in rows]}
    payload = canonical_json(collection).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    target = root.path / "assets" / "by-hash" / f"{digest}.geojson"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, target)
    return (build.release_id, stable_id("asset", build.release_id, kind), root.relative(target), digest, len(payload),
            kind, license_id)


def _write_assets(root: DataRoot, ctx: Context, build: Build) -> list[tuple]:
    wanted = {row["fips"] for row in ctx.states.values()}
    records = []
    for kind, boundary_set, prefix in (("geojson:states", BOUNDARY_SET_STATE, 2), ("geojson:counties", BOUNDARY_SET_COUNTY, 2)):
        rows = [row for row in ctx.connection.execute(
            "SELECT feature_id, geo_version_id, source_feature_key, geometry_json FROM boundary_feature "
            "WHERE boundary_set_id=? ORDER BY source_feature_key", (boundary_set,))
            if row["source_feature_key"][:prefix] in wanted]
        records.append(_asset(root, build, kind, rows, "us-government-work"))
    for boundary in ctx.connection.execute(
            "SELECT boundary_set_id, vintage, license_id FROM boundary_set WHERE vintage LIKE 'congress-%' ORDER BY vintage"):
        rows = ctx.connection.execute(
            "SELECT feature_id, geo_version_id, source_feature_key, geometry_json FROM boundary_feature "
            "WHERE boundary_set_id=? ORDER BY source_feature_key", (boundary["boundary_set_id"],)).fetchall()
        records.append(_asset(root, build, f"geojson:districts-{boundary['vintage'].split('-')[1]}", rows,
                              boundary["license_id"]))
    return records


# --- build -----------------------------------------------------------------------------------------
def _context(connection: sqlite3.Connection, runs: dict[str, sqlite3.Row], curation: dict) -> Context:
    contests = {row["contest_id"]: row for row in connection.execute(
        "SELECT k.contest_id, o.code, e.cycle_year, e.natural_key AS election_key, k.seat_key, k.stage, k.contest_type, "
        "k.voting_system, k.scope_geo_version_id, k.label FROM contest k JOIN office o USING (office_id) "
        "JOIN election_event e USING (election_id)")}
    candidacy_of = {row[0]: row[1] for row in connection.execute("SELECT option_id, candidacy_id FROM ballot_option")}
    reference = states()
    state_rows = {postal: row for postal, row in state_versions(connection).items() if postal in reference}
    return Context(connection, runs, contests, candidacy_of, state_rows, _features(connection, BOUNDARY_SET_STATE),
                   _features(connection, BOUNDARY_SET_COUNTY), _district_features(connection), curation,
                   {row[0] for row in connection.execute("SELECT candidacy_id FROM candidacy WHERE candidacy_kind='ticket'")})


def build_release(root: DataRoot, connection: sqlite3.Connection, *, notes: str | None = None) -> dict[str, Any]:
    curation, curation_hash = load_curation()
    _, federal_hash = load_federal()
    runs = latest_runs(connection)
    if missing := [name for name in REQUIRED if name not in runs]:
        raise InvalidInput(f"No completed import for {missing}; run the imports first.")
    if not any(name in runs for name in CONTEST_TOTAL_SOURCES):
        raise InvalidInput("No results source has been imported; run the imports first.")
    decisions = sorted((row[0], row[1]) for row in connection.execute(
        "SELECT decision_id, content_hash FROM curation_decision"))
    # The release identity pins the rule implementation too: any change to this module yields a new release.
    rules_source = hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    # Runs enter the identity by their fact hash, not their run id: re-importing byte-different
    # copies of a source that state the same facts does not create a new release.
    manifest = {"runs": {name: row["output_hash"] for name, row in sorted(runs.items())},
                "decisions": decisions, "curation_hash": curation_hash, "federal_hash": federal_hash,
                "rules": RULE_VERSION, "rules_source_sha256": rules_source, "schema_version": SCHEMA_VERSION,
                "scope": "us-federal"}
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
                            manifest_hash, "us-federal", notes))
    build = Build(release_id)
    try:
        ctx = _context(connection, runs, curation)
        totals = _contest_totals(ctx, build)
        county = _county_units(ctx, build)
        reconciliation = _reconcile(ctx, build, county, totals)
        _publish_county_level(ctx, build, county, reconciliation)
        _outcomes(ctx, build, totals)
        _district_elector_cells(ctx, build, totals)
        _inventory(ctx, build, totals, reconciliation)
        assets = _write_assets(root, ctx, build)
        blocking = [issue for issue in build.issues if issue[5] == "error" and issue[11] != "accepted"]
        report_path = root.path / "reports" / release_id / "reconciliation.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        by_cycle: dict[str, dict] = defaultdict(dict)
        for entry in reconciliation.values():
            by_cycle[str(entry["cycle"])][entry["state"]] = entry
        report = {"release_id": release_id, "cycles": by_cycle, "states": by_cycle.get("2024", {})}
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        electoral_runs = [row["run_id"] for name, row in runs.items() if name.startswith("nara-electoral-")]
        with transaction(connection):
            connection.executemany("INSERT OR IGNORE INTO reporting_unit VALUES (?,?,?,?,?,?,?,?)", build.extra_units)
            connection.executemany("INSERT OR IGNORE INTO derivation VALUES (?,?,?,?,?)", build.derivations.values())
            connection.executemany("INSERT OR IGNORE INTO derivation_input VALUES (?,?,?)", sorted(build.derivation_inputs))
            connection.executemany("INSERT INTO published_result VALUES (?,?,?,?,?,?,?,?,?,?,?)", build.results)
            connection.executemany("INSERT INTO published_unit_status VALUES (?,?,?,?,?,?,?,?,?)", build.unit_status)
            connection.executemany("INSERT INTO release_selection VALUES (?,?,?,?,?,?)", build.selections)
            connection.executemany("INSERT INTO published_outcome VALUES (?,?,?,?,?,?,?)", build.outcomes)
            connection.executemany("INSERT INTO coverage_cell VALUES (?,?,?,?,?,?,?,?,?)", build.coverage)
            connection.executemany("INSERT INTO coverage_inventory VALUES (?,?,?,?,?,?,?,?,?,?)", build.inventory)
            connection.executemany("INSERT INTO release_asset VALUES (?,?,?,?,?,?,?)", assets)
            connection.executemany("INSERT INTO validation_issue VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", build.issues)
            connection.executemany("INSERT OR IGNORE INTO release_decision VALUES (?,?)",
                                   [(release_id, decision_id) for decision_id, _ in decisions])
            for run_id in electoral_runs:
                connection.execute("INSERT INTO published_electoral_vote SELECT ?, electoral_vote_id FROM electoral_vote "
                                   "WHERE run_id=?", (release_id, run_id))
            connection.execute("UPDATE data_release SET status=? WHERE release_id=?",
                               ("failed" if blocking else "staged", release_id))
    except Exception:
        with transaction(connection):
            connection.execute("UPDATE data_release SET status='failed' WHERE release_id=?", (release_id,))
        raise
    return summarize(connection, release_id) | {"build": "built", "blocking_errors": len(blocking)}


def _district_elector_cells(ctx: Context, build: Build, totals: dict[str, dict]) -> None:
    """Maine/Nebraska district elector contests exist but have no popular-vote source."""
    cycles = {ctx.contests[contest]["cycle_year"] for contest in totals if ctx.contests[contest]["code"] == "president"}
    for contest, meta in ctx.contests.items():
        if meta["code"] == "president" and ":cd" in meta["seat_key"] and meta["cycle_year"] in cycles:
            build.coverage.append((build.release_id, contest, "district", "unavailable", 1, 0,
                                   canonical_json([meta["seat_key"].split(":")[-1].upper()]), "boundary_unavailable",
                                   "District popular votes are not in an imported source; any elector outcome is from NARA."))


def _purge(connection: sqlite3.Connection, release_id: str) -> None:
    with transaction(connection):
        for table in ("published_result", "published_unit_status", "release_selection", "published_outcome",
                      "coverage_cell", "coverage_inventory", "release_asset", "release_decision",
                      "published_electoral_vote"):
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

    changes: dict[str, Any] = {}
    for name, loader in (("results", results), ("outcomes", outcomes), ("coverage", coverage)):
        before, after = loader(old), loader(new)
        delta = [{"key": list(key), "before": before.get(key), "after": after.get(key)}
                 for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)]
        changes[name] = delta[:limit] + ([{"truncated": len(delta) - limit}] if len(delta) > limit else [])
        changes[f"{name}_changed"] = len(delta)
    return {"from": old, "to": new, **changes}


__all__ = ["ADAPTERS", "activate_release", "build_release", "diff_releases", "publish_release", "summarize",
           "verify_assets"]
