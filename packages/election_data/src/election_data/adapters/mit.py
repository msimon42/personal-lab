"""MIT Election Lab county presidential returns (countypres_2000-2024), all cycles.

Rows are preserved at source grain: every voting mode, aggregate and metric row is
kept with its file line as evidence, and repeated rows become ordered batches. Blank
modes become ``unknown``; blank counts become ``missing``; neither is turned into a
total or a zero here. Totals are chosen later, at release, under reviewed rules.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from typing import Any

from ..core import DataRoot, ReviewRequired, canonical_json, stable_id, transaction
from ..federal import (AGGREGATE_LABELS, Registrar, classify_label, load_federal, match_ticket,
                       statewide_president_seat, voting_system)
from ..pipeline import (Normalized, begin_run, complete_run, county_versions, existing_result, load_curation, unit_id)

ADAPTER_ID = "mit-county-president"
ADAPTER_VERSION = "2"
SOURCE_ID = "mit-county-president"
HEADER = ["state", "county_name", "year", "state_po", "county_fips", "office", "candidate", "party",
          "candidatevotes", "totalvotes", "version", "mode"]
OFFICE_LABEL = "US PRESIDENT"


def mode_key(raw: str) -> str:
    if raw == "TOTAL":
        return "all"
    if raw == "":
        return "unknown"
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def _batch(grains: dict[tuple, int], grain: tuple, accounting: dict[str, int]) -> str:
    """Repeated rows at one grain are kept as ordered batches, never merged or dropped."""
    grains[grain] = grains.get(grain, 0) + 1
    if grains[grain] > 1:
        accounting["repeated_grain_rows"] += 1
    return f"b{grains[grain]}"


def unit_mapping(postal: str, fips: str, name: str, year: int, counties: dict[str, str],
                 curation: dict[str, Any]) -> tuple[str, str, str | None, list[tuple[str, str]], str | None]:
    """Return ``(natural_key, kind, geo_version_id, components, notes)`` for an MIT unit."""
    merge = next(d["scope"] for d in curation["decisions"] if d["decision_type"] == "unit_merge")
    if fips in merge["members"] and merge["target_fips"] in counties:
        target_version = counties[merge["target_fips"]]
        others = [member for member in merge["members"] if member != fips]
        if fips == merge["target_fips"]:
            return (f"county-part:{fips}:excluding:{'+'.join(others)}", "county", None,
                    [(target_version, "part_of")], "County election authority excluding separately reported units.")
        return (f"election-jurisdiction:{postal}:{fips}", "election_jurisdiction", None,
                [(target_version, "part_of")], f"Separately reported jurisdiction ({name.title()}).")
    if postal == "AK":
        number = int(fips[2:])
        if number == 99:
            return (f"nongeographic:AK:mit-{fips}", "nongeographic", None, [],
                    "Source bucket DISTRICT 99; not distributed to districts.")
        return (f"ak-house-district-{year}:{number:02d}", "state_legislative_district", None, [],
                f"Alaska state house district ({year} plan); district geometry not acquired.")
    crosswalk = next(d["scope"]["codes"] for d in curation["decisions"] if d["decision_type"] == "unit_code_crosswalk")
    if fips in crosswalk and crosswalk[fips] in counties:
        target = crosswalk[fips]
        return (f"county:{target}", "county", counties[target], [],
                f"Source code {fips} crosswalked to {target} (dec-2024-pres-fips-crosswalk).")
    if fips in counties:
        return f"county:{fips}", "county", counties[fips], [], None
    return (f"county-code:{fips}", "county", None, [],
            f"Source code {fips} has no polygon in the 2020 county boundary vintage; tabular only.")


def import_file(root: DataRoot, connection: sqlite3.Connection, artifact_id: str) -> dict:
    curation, _ = load_curation()
    federal, digest = load_federal()
    context = begin_run(root, connection, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                        artifacts={"returns": artifact_id}, mapping_version=federal["mapping_version"],
                        mapping_hash=digest, config={"years": "all"})
    if context.existing:
        return existing_result(connection, context)
    counties = county_versions(connection)
    normalized = Normalized()
    accounting = {"rows_read": 0, "rows_other_office": 0, "vote_rows": 0, "metric_rows": 0, "missing_counts": 0,
                  "repeated_grain_rows": 0, "cycles": 0}
    sets: dict[str, dict[str, Any]] = {}
    units: dict[tuple[int, str, str], str] = {}
    grains: dict[tuple, int] = {}
    totalvotes: dict[tuple, tuple[int, int]] = {}
    contests_by_year: dict[int, dict[str, str]] = {}
    with context.paths["returns"].open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        header = next(reader)
        if header != HEADER:
            raise ReviewRequired(f"MIT layout changed: header {header} does not match the reviewed fingerprint.")
        with transaction(connection):
            registrar = Registrar(connection)
            for record in reader:
                accounting["rows_read"] += 1
                line = reader.line_num
                row = dict(zip(HEADER, record, strict=True))
                if row["office"] != OFFICE_LABEL:
                    accounting["rows_other_office"] += 1
                    continue
                if not row["year"].isdigit() or int(row["year"]) % 4:
                    raise ReviewRequired(f"Line {line}: {row['year']!r} is not a presidential cycle.")
                year, postal = int(row["year"]), row["state_po"]
                if postal not in registrar.states:
                    raise ReviewRequired(f"Line {line}: state {postal} has no elector contest mapping.")
                if year not in contests_by_year:
                    contests_by_year[year] = registrar.president_contests(year)
                contest = contests_by_year[year][statewide_president_seat(postal, year)]
                if contest not in sets:
                    sets[contest] = {"set_id": stable_id("set", context.identity, contest, "mit-county-units"),
                                     "versions": set()}
                set_id = sets[contest]["set_id"]
                sets[contest]["versions"].add(row["version"])
                fips = row["county_fips"].zfill(5)
                if not fips.isdigit():
                    raise ReviewRequired(f"Line {line}: county code {row['county_fips']!r} is not numeric.")
                unit_key = (year if postal == "AK" else 0, postal, fips)
                if unit_key not in units:
                    natural, kind, version, components, notes = unit_mapping(postal, fips, row["county_name"], year,
                                                                             counties, curation)
                    identifier = unit_id(natural)
                    units[unit_key] = identifier
                    normalized.other.append((
                        "INSERT OR IGNORE INTO reporting_unit VALUES (?,?,?,?,?,?,?,?)",
                        (identifier, kind, version, registrar.state_unit(postal), row["county_name"].title(),
                         f"mit:{fips}", natural, notes)))
                    for component_version, role in components:
                        normalized.other.append(("INSERT OR IGNORE INTO reporting_unit_component VALUES (?,?,?)",
                                                 (identifier, component_version, role)))
                unit = units[unit_key]
                mode = mode_key(row["mode"])
                round_key = "round_1" if voting_system("president", postal, year) == "ranked_choice" else "final_or_only"
                raw = row["candidatevotes"]
                if raw in ("", "NA"):
                    votes, status = None, "missing"
                    accounting["missing_counts"] += 1
                elif raw.isdigit():
                    votes, status = int(raw), "reported"
                else:
                    raise ReviewRequired(f"Line {line}: vote count {raw!r} is not a nonnegative integer.")
                locator = canonical_json({"line": line, "format": "tsv"})
                candidate = row["candidate"]
                kind, key = classify_label(candidate)
                if kind == "metric":
                    grain = ("metric", set_id, unit, round_key, mode, key)
                    batch = _batch(grains, grain, accounting)
                    metric_id = stable_id("mobs", *grain[1:], batch)
                    normalized.metrics.append((metric_id, set_id, unit, round_key, mode, batch, key, votes, status,
                                               f"line:{line}"))
                    normalized.evidence.append((metric_id, artifact_id, locator))
                    accounting["metric_rows"] += 1
                    continue
                if kind == "aggregate":
                    key = "aggregate-other-mit" if key == "aggregate-other" else key
                    label, option_kind = AGGREGATE_LABELS[key]
                    option = registrar.option(contest, registrar.aggregate(contest, key, label), kind=option_kind,
                                              label=label)
                else:
                    ticket = match_ticket(year, candidate)
                    if ticket:
                        option = registrar.option(contest, registrar.ticket(contest, year, ticket),
                                                  kind="candidacy_total", label=candidate,
                                                  party=registrar.party(row["party"]))
                    else:
                        option = registrar.option(contest, registrar.named(contest, candidate), kind="candidacy_total",
                                                  label=candidate, party=registrar.party(row["party"]))
                grain = ("vote", set_id, unit, option, round_key, mode)
                batch = _batch(grains, grain, accounting)
                observation = stable_id("vobs", *grain[1:], batch)
                normalized.votes.append((observation, set_id, unit, option, round_key, mode, batch, votes, status,
                                         f"line:{line}", candidate, row["party"]))
                normalized.evidence.append((observation, artifact_id, locator))
                accounting["vote_rows"] += 1
                if row["totalvotes"].isdigit():
                    key_total = (set_id, unit, round_key, mode)
                    value = int(row["totalvotes"])
                    if key_total not in totalvotes:
                        totalvotes[key_total] = (value, line)
                    elif totalvotes[key_total][0] != value:
                        normalized.diagnostics.append({"line": line, "issue": "inconsistent_totalvotes_column"})
    for (set_id, unit, round_key, mode), (value, line) in sorted(totalvotes.items()):
        metric_id = stable_id("mobs", set_id, unit, round_key, mode, "mit_totalvotes_column")
        normalized.metrics.append((metric_id, set_id, unit, round_key, mode, "b1", "mit_totalvotes_column",
                                   value, "reported", f"line:{line}"))
        normalized.evidence.append((metric_id, artifact_id, canonical_json({"line": line, "column": "totalvotes"})))
    for contest, entry in sorted(sets.items()):
        normalized.result_sets.append((
            entry["set_id"], SOURCE_ID, contest, f"MIT version {', '.join(sorted(entry['versions']))}",
            "secondary_compilation", None, None, "source_reported_by_mode", "mit-county-units",
            "MIT county returns; modes preserved; blank mode stored as unknown."))
    accounting["cycles"] = len(contests_by_year)
    if accounting["rows_read"] != accounting["vote_rows"] + accounting["metric_rows"] + accounting["rows_other_office"]:
        raise ReviewRequired("Raw row counts do not reconcile to accepted and ignored rows.")
    normalized.accounting = accounting
    return complete_run(connection, context, normalized)


def parse_preview(text: str) -> list[dict[str, str]]:
    """Small helper for tests and diagnostics: parse TSV text with the reviewed header."""
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    if next(reader) != HEADER:
        raise ReviewRequired("Header mismatch.")
    return [dict(zip(HEADER, record, strict=True)) for record in reader]
