"""MIT Election Lab state-level returns for President, Senate and House, 1976-2024.

Rows are kept at source grain (one row per candidate per party line), with repeated
rows as ordered batches. Format quirks are decoded under dec-fed-mit-state-format and
fail closed. Contests, stages and seats come from reviewed rules in ``federal``.
"""
from __future__ import annotations

import csv
import sqlite3
from typing import Any, Iterator

from ..core import DataRoot, ReviewRequired, canonical_json, stable_id, transaction
from ..federal import (AGGREGATE_LABELS, Registrar, classify_label, election_key, house_seat, load_federal, match_ticket,
                       senate_seat, stage_for, statewide_president_seat, states, voting_system)
from ..pipeline import Normalized, begin_run, candidacy_id, complete_run, contest_id, existing_result

ADAPTER_VERSION = "2"
COMMON = ["year", "state", "state_po", "state_fips", "state_cen", "state_ic", "office"]
DATASETS: dict[str, dict[str, Any]] = {
    "president": {
        "adapter_id": "mit-state-president", "source_id": "mit-state-president", "office_label": "US PRESIDENT",
        "header": COMMON + ["candidate", "party_detailed", "writein", "candidatevotes", "totalvotes", "version",
                            "notes", "party_simplified"],
        "delimiter": ",", "party": "party_detailed"},
    "senate": {
        "adapter_id": "mit-senate", "source_id": "mit-senate", "office_label": "US SENATE",
        "header": COMMON + ["district", "stage", "special", "candidate", "party_detailed", "writein", "mode",
                            "candidatevotes", "totalvotes", "unofficial", "version", "party_simplified"],
        "delimiter": "\t", "party": "party_detailed"},
    "house": {
        "adapter_id": "mit-house", "source_id": "mit-house", "office_label": "US HOUSE",
        "header": COMMON + ["district", "stage", "runoff", "special", "candidate", "party", "writein", "mode",
                            "candidatevotes", "totalvotes", "unofficial", "version", "fusion_ticket"],
        "delimiter": "wrapped", "party": "party"},
}


def decode_wrapped(line: str, number: int) -> list[str]:
    """Decode one House record: the whole CSV record is a single quoted field with \\" escapes."""
    if len(line) < 2 or line[0] != '"' or line[-1] != '"':
        raise ReviewRequired(f"Line {number}: House record is not wrapped in quotes; the served format changed.")
    body = line[1:-1].replace('\\\\"\\"', "\x00").replace('\\"', '"').replace("\x00", '""')
    return next(csv.reader([body]))


def records(path, dataset: dict[str, Any]) -> Iterator[tuple[int, dict[str, str]]]:
    header = dataset["header"]
    with path.open("r", encoding="utf-8", newline="") as handle:
        if dataset["delimiter"] == "wrapped":
            first = handle.readline().rstrip("\n").split(",")
            if first != header:
                raise ReviewRequired(f"Layout changed: header {first} does not match the reviewed fingerprint.")
            for number, line in enumerate(handle, start=2):
                fields = decode_wrapped(line.rstrip("\n"), number)
                if len(fields) != len(header):
                    raise ReviewRequired(f"Line {number}: decoded {len(fields)} fields, expected {len(header)}.")
                yield number, dict(zip(header, fields))
            return
        reader = csv.reader(handle, delimiter=dataset["delimiter"])
        if next(reader) != header:
            raise ReviewRequired("Layout changed: header does not match the reviewed fingerprint.")
        for fields in reader:
            if len(fields) != len(header):
                raise ReviewRequired(f"Line {reader.line_num}: {len(fields)} fields, expected {len(header)}.")
            yield reader.line_num, dict(zip(header, fields))


def parse_count(text: str, line: int) -> int | None:
    text = text.strip()
    if text in ("-1", "-1.0", "", "NA"):
        return None
    if text.endswith(".0"):
        text = text[:-2]
    if not text.isdigit():
        raise ReviewRequired(f"Line {line}: count {text!r} is not a nonnegative integer.")
    return int(text)


def _flag(text: str) -> bool:
    return text.strip().upper() == "TRUE"


def import_dataset(root: DataRoot, connection: sqlite3.Connection, artifact_id: str, name: str) -> dict:
    dataset = DATASETS[name]
    curation, digest = load_federal()
    context = begin_run(root, connection, adapter_id=dataset["adapter_id"], adapter_version=ADAPTER_VERSION,
                        artifacts={"returns": artifact_id}, mapping_version=curation["mapping_version"],
                        mapping_hash=digest, config={"dataset": name})
    if context.existing:
        return existing_result(connection, context)
    normalized = Normalized()
    accounting = {"rows_read": 0, "vote_rows": 0, "metric_rows": 0, "missing_counts": 0, "repeated_grain_rows": 0,
                  "contests": 0}
    known_states = states()
    sets: dict[str, dict[str, Any]] = {}
    president_years: set[int] = set()
    line_votes: dict[tuple[str, str, str], int] = {}
    runoffs: dict[str, tuple[str, str, int, bool]] = {}
    grains: dict[tuple, int] = {}
    totals: dict[tuple, tuple[int | None, int, bool]] = {}
    with transaction(connection):
        registrar = Registrar(connection)
        for line, row in records(context.paths["returns"], dataset):
            accounting["rows_read"] += 1
            if row["office"] != dataset["office_label"]:
                raise ReviewRequired(f"Line {line}: unexpected office {row['office']!r}.")
            postal, year = row["state_po"], int(row["year"])
            if postal not in known_states:
                raise ReviewRequired(f"Line {line}: state {postal!r} is not in the reviewed state list.")
            special = _flag(row.get("special", "FALSE"))
            cycle = year if year % 2 == 0 else year - 1
            if name == "president":
                office, stage = "president", "general"
                if year not in president_years:
                    president_years.add(year)
                    registrar.president_contests(year)  # includes Maine/Nebraska district elector contests
                seat = statewide_president_seat(postal, year)
                unit = registrar.state_unit(postal)
            elif name == "senate":
                office = "senate"
                stage = stage_for("senate", postal, year, row["stage"], False)
                try:
                    seat = senate_seat(postal, cycle, special)
                except ValueError as error:
                    raise ReviewRequired(f"Line {line}: {error}") from error
                unit = registrar.state_unit(postal)
            else:
                stage = stage_for("house", postal, year, row["stage"], _flag(row["runoff"]))
                district = int(row["district"])
                if postal == "DC":
                    office, seat = "house_delegate", "delegate:DC"
                    unit = registrar.district_unit(postal, district, year, delegate=True)
                else:
                    office, seat = "house", house_seat(postal, district, cycle)
                    unit = registrar.district_unit(postal, district, cycle)
            contest = registrar.contest(office=office, year=year, postal=postal, seat_key=seat, stage=stage,
                                        contest_type="special" if special else "regular")
            if stage == "runoff":
                runoffs[contest] = (office, seat, cycle, special)
            if contest not in sets:
                accounting["contests"] += 1
                # Ranked-choice contests: the source does not say which tabulation round it reports.
                # Presidential RCV tallies are first-choice counts, checked by the first-round majority rule.
                ranked = voting_system(office, postal, cycle) == "ranked_choice"
                rounds = ("round_1" if office == "president" else "unknown") if ranked else "final_or_only"
                sets[contest] = {"set_id": stable_id("set", context.identity, contest, "contest"), "versions": set(),
                                 "unofficial": False, "unit": unit, "round": rounds}
            entry = sets[contest]
            entry["versions"].add(row["version"])
            entry["unofficial"] = entry["unofficial"] or _flag(row.get("unofficial", "FALSE"))
            set_id = entry["set_id"]
            round_key = entry["round"]
            votes = parse_count(row["candidatevotes"], line)
            status = "reported" if votes is not None else "missing"
            accounting["missing_counts"] += votes is None
            locator = canonical_json({"line": line, "format": "csv"})
            writein = _flag(row.get("writein", "FALSE"))
            kind, key = classify_label(row["candidate"], writein)
            party_label = row[dataset["party"]]
            total = parse_count(row["totalvotes"], line)
            previous = totals.get((set_id, unit, round_key))
            totals[(set_id, unit, round_key)] = (total, line, bool(previous and previous[0] != total) or bool(previous and previous[2]))
            if kind == "metric":
                grain = ("metric", set_id, unit, round_key, "all", key)
                grains[grain] = grains.get(grain, 0) + 1
                batch = f"b{grains[grain]}"
                identifier = stable_id("mobs", *grain[1:], batch)
                normalized.metrics.append((identifier, set_id, unit, round_key, "all", batch, key, votes, status,
                                           f"line:{line}"))
                normalized.evidence.append((identifier, artifact_id, locator))
                accounting["metric_rows"] += 1
                continue
            party = registrar.party(party_label)
            line_key = " ".join(party_label.upper().split()) or "NO-PARTY-LABEL"
            if kind == "aggregate":
                if key == "aggregate-unnamed" and line_key != "NO-PARTY-LABEL":
                    key = "aggregate-unnamed:" + line_key.lower().replace(" ", "-")
                label, option_kind = AGGREGATE_LABELS[key.split(":")[0]]
                candidacy = registrar.aggregate(contest, key, label if ":" not in key else f"Unnamed {line_key.title()} line")
                option = registrar.option(contest, candidacy, kind=option_kind, label=label)
            else:
                ticket = match_ticket(year, row["candidate"]) if name == "president" else None
                candidacy = registrar.ticket(contest, year, ticket) if ticket else registrar.named(contest, row["candidate"])
                suffix = "|write-in" if writein else ""
                option = registrar.option(contest, candidacy, kind="party_line", party=party,
                                          label=f"{row['candidate']} ({line_key.title()}{', write-in' if writein else ''})",
                                          line=f"{line_key}{suffix}")
            if kind == "candidate" and party is not None:
                line_votes[(contest, candidacy, party)] = line_votes.get((contest, candidacy, party), 0) + (votes or 0)
            grain = ("vote", set_id, unit, option, round_key, "all")
            grains[grain] = grains.get(grain, 0) + 1
            accounting["repeated_grain_rows"] += grains[grain] > 1
            batch = f"b{grains[grain]}"
            identifier = stable_id("vobs", *grain[1:], batch)
            normalized.votes.append((identifier, set_id, unit, option, round_key, "all", batch, votes, status,
                                     f"line:{line}", row["candidate"], party_label))
            normalized.evidence.append((identifier, artifact_id, locator))
            accounting["vote_rows"] += 1
    with transaction(connection):
        for contest, (office, seat, cycle, special) in sorted(runoffs.items()):
            # A runoff is a separate stage linked to the general election it follows.
            general = contest_id(election_key(cycle, "special-general" if special else "general"), office, seat,
                                 "general", "special" if special else "regular")
            if general in sets:
                connection.execute("UPDATE contest SET predecessor_contest_id=? WHERE contest_id=?", (general, contest))
        # A candidate's primary party is the ballot line on which they received the most votes.
        best: dict[tuple[str, str], tuple[int, str]] = {}
        for (contest, candidacy, party), total in sorted(line_votes.items()):
            if total > best.get((contest, candidacy), (-1, ""))[0]:
                best[(contest, candidacy)] = (total, party)
        connection.executemany(
            "UPDATE candidacy SET primary_party_id=? WHERE candidacy_id=? AND candidacy_kind='candidate'",
            [(party, candidacy_id(contest, candidacy)) for (contest, candidacy), (_, party) in sorted(best.items())])
    for contest, entry in sorted(sets.items()):
        versions = ", ".join(sorted(entry["versions"]))
        normalized.result_sets.append((
            entry["set_id"], dataset["source_id"], contest, f"MIT version {versions}",
            "unofficial" if entry["unofficial"] else "secondary_compilation", None, None, "source_reported_lines",
            "contest", "MIT state-level file; party lines and repeated rows preserved."))
    inconsistent = 0
    for (set_id, unit, round_key), (value, line, mixed) in sorted(totals.items()):
        inconsistent += mixed
        identifier = stable_id("mobs", set_id, unit, round_key, "all", "source_total_votes")
        normalized.metrics.append((identifier, set_id, unit, round_key, "all", "b1", "source_total_votes", value,
                                   "reported" if value is not None else "missing", f"line:{line}"))
        normalized.evidence.append((identifier, artifact_id, canonical_json({"line": line, "column": "totalvotes"})))
    if accounting["rows_read"] != accounting["vote_rows"] + accounting["metric_rows"]:
        raise ReviewRequired("Raw row counts do not reconcile to accepted rows.")
    accounting["contests_with_inconsistent_totalvotes"] = inconsistent
    normalized.accounting = accounting
    return complete_run(connection, context, normalized)
