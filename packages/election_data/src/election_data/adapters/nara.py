"""NARA Electoral College result pages (1789-2024) -> allocations and actual electoral votes.

Electoral votes are stored as cast and separately from popular votes. Recipients are
matched to curated tickets where possible and otherwise kept under the NARA label.
State rows must sum to the page's total row or the year is left unimported for review.
"""
from __future__ import annotations

import re
import sqlite3

from ..core import DataRoot, ReviewRequired, canonical_json, stable_id, transaction
from ..federal import Registrar, general_election_date, load_federal, match_ticket, normalize_name, states
from ..pipeline import (Normalized, begin_run, complete_run, election_id, existing_result, geo_id, load_curation,
                        person_id)

ADAPTER_ID = "nara-electoral-college-page"
ADAPTER_VERSION = "3"
YEARS = [1789] + list(range(1792, 2025, 4))
STATE_ALIASES = {"D.C.": "DC", "DIST. OF COLUMBIA": "DC", "DISTRICT OF COLUMBIA": "DC"}


def source_for(year: int) -> str:
    return "nara-electoral-college-2024" if year == 2024 else "nara-electoral-college"


def _count(text: str, where: str) -> int:
    text = text.replace("\xa0", " ").strip().rstrip("*")
    if text in ("-", "", "—", "–", "..."):
        return 0
    if not text.isdigit():
        raise ReviewRequired(f"{where}: electoral vote cell {text!r} is not a count.")
    return int(text)


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _recipient(label: str) -> str:
    return re.sub(r",\s*(of|Esq)\b.*$", "", _clean(label)).strip()


def election_event(connection: sqlite3.Connection, year: int) -> str:
    """Presidential election event; before 1848 there was no single national election day."""
    if year >= 1848:
        return Registrar(connection).election(year)[0]
    key = f"us-presidential-election-{year}"
    connection.execute("INSERT OR IGNORE INTO election_event VALUES (?,?,?,?,?,?,?,?,?)", (
        election_id(key), f"{year} United States presidential election", f"{year}-01-01", f"{year}-12-31", "year",
        year, geo_id("nation:US"), "presidential", key))
    return election_id(key)


def _parse(tables, year: int):
    """Return ``(recipients, state_rows, reported_totals)``.

    recipients: [(label, role)]; state_rows: [(state_label, electors or None, [votes...], row_number)].
    """
    for table in tables:
        if not table or not table[0]:
            continue
        first = _clean(table[0][0][0]).lower()
        if first == "name of candidate":  # 1789: candidates in rows, states in columns
            columns = [_clean(text) for text, _ in table[0][1:]]
            recipients, by_state = [], {column: [] for column in columns if column.lower() != "total"}
            reported = []
            for row in table[1:]:
                cells = [_clean(text) for text, _ in row]
                if not cells or cells[0].lower().startswith(("total", "note")):
                    continue
                recipients.append((_recipient(cells[0]), "president"))
                for column, cell in zip(columns, cells[1:]):
                    if column.lower() == "total":
                        reported.append(_count(cell, f"{year} total"))
                    else:
                        by_state[column].append(_count(cell, f"{year} {column}"))
            rows = [(state, None, votes, index) for index, (state, votes) in enumerate(by_state.items(), start=1)]
            return recipients, rows, reported, True
        if first != "state" or len(table) < 3:
            continue
        groups = [(_clean(text).lower(), span) for text, span in table[0][2:]]
        names = [_recipient(text) for text, _ in table[1]]
        if sum(span for _, span in groups) != len(names):
            raise ReviewRequired(f"{year}: header spans {groups} do not cover {len(names)} recipient columns.")
        roles = []
        for text, span in groups:
            combined = "president" in text and "vice" in text and "and" in text
            roles += ["president" if combined or "vice" not in text else "vice_president"] * span
        recipients = list(zip(names, roles))
        rows, reported = [], None
        for index, row in enumerate(table[2:], start=3):
            cells = [_clean(text) for text, _ in row]
            if len(cells) <= 1 or not cells[0] or cells[0].lower().startswith("note"):
                continue  # footnote and note rows carry no vote cells
            if cells[0].lower().startswith("total"):
                # "176 (175)" in the elector column: 176 electors appointed, 175 voted (dec-fed-electors-appointed).
                appointed = re.fullmatch(r"(\d+) \((\d+)\)\*?", cells[1])
                if appointed:
                    cells[1] = appointed.group(1)
                reported = [_count(cell, f"{year} totals") for cell in cells[1:2 + len(names)]]
                continue
            if len(cells) != 2 + len(names):
                raise ReviewRequired(f"{year}: row {index} ({cells[0]!r}) has {len(cells)} cells.")
            rows.append((cells[0], _count(cells[1], f"{year} {cells[0]}"),
                         [_count(cell, f"{year} {cells[0]}") for cell in cells[2:]], index))
        return recipients, rows, reported, False
    raise ReviewRequired(f"{year}: no electoral vote table with the reviewed layout.")


def import_year(root: DataRoot, connection: sqlite3.Connection, artifact_id: str, year: int) -> dict:
    from ..readers import read_html_tables

    curation_2024, _ = load_curation()
    _, digest = load_federal()
    context = begin_run(root, connection, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                        artifacts={"page": artifact_id}, mapping_version="us-federal-1976-2024-v1",
                        mapping_hash=digest, config={"year": year})
    if context.existing:
        return existing_result(connection, context)
    recipients, rows, reported, transposed = _parse(read_html_tables(context.paths["page"], spans=True), year)
    by_name = {row["name"].upper(): postal for postal, row in states().items()}
    by_postal = set(states())
    normalized = Normalized()
    with transaction(connection):
        event = election_event(connection, year)
        registrar = Registrar(connection)
        people: list[str | None] = []
        for label, role in recipients:
            if label.lower().startswith("other") or not label:
                people.append(None)
                continue
            key = match_ticket(year, label, role=role) or match_ticket(
                year, label, role="vice_president" if role == "president" else "president")
            if key is None:
                key = f"nara:{normalize_name(label)}"
                connection.execute("INSERT OR IGNORE INTO person VALUES (?,?,?,1)", (person_id(key), label, key))
            people.append(key)
        sums = [0] * len(recipients)
        elector_total = 0
        seen = set()
        for label, electors, votes, index in rows:
            name = label.rstrip("0123456789 *").strip().upper()
            postal = label if label in by_postal else by_name.get(name) or STATE_ALIASES.get(name)
            if postal is None or postal in seen:
                raise ReviewRequired(f"{year}: row {index} ({label!r}) does not map to one state.")
            seen.add(postal)
            if postal not in registrar.states:
                raise ReviewRequired(f"{year}: state geography for {postal} is missing.")
            version = registrar.states[postal]["geo_version_id"]
            allocation = stable_id("allocation", event, postal, "state_total")
            president_votes = sum(value for value, (_, role) in zip(votes, recipients) if role == "president")
            count = electors if electors is not None else president_votes // 2  # 1789: two votes per elector
            elector_total += count
            normalized.other.append(("INSERT OR IGNORE INTO electoral_allocation VALUES (?,?,?,?,?,?,?)",
                                     (allocation, event, version, "state_total", "state", count, artifact_id)))
            for position, (value, (recipient, role)) in enumerate(zip(votes, recipients)):
                sums[position] += value
                if not value:
                    continue
                locator = canonical_json({"table": f"electoral-college-{year}", "row": index, "state": label,
                                          "recipient": recipient, "column": position + 1,
                                          **({"two_votes_per_elector": True} if transposed or year < 1804 else {})})
                key = people[position]
                normalized.other.append(("INSERT INTO electoral_vote VALUES (?,?,?,?,?,?,?,?)", (
                    stable_id("ev", context.identity, allocation, position, role), allocation,
                    person_id(key) if key else None, role, value, artifact_id, context.run_id, locator)))
            if year == 2024:
                components = next(d["scope"] for d in curation_2024["decisions"]
                                  if d["decision_type"] == "electoral_components")
                if postal in components:
                    if sum(part["electors"] for part in components[postal].values()) != count:
                        raise ReviewRequired(f"{postal} component electors do not sum to the NARA total {count}.")
                    for key, part in components[postal].items():
                        kind = "statewide_at_large" if key == "statewide_at_large" else "congressional_district"
                        normalized.other.append(("INSERT OR IGNORE INTO electoral_allocation VALUES (?,?,?,?,?,?,?)", (
                            stable_id("allocation", event, postal, kind, key), event, version, kind, key,
                            part["electors"], artifact_id)))
    if reported is not None:
        expected = reported[1:] if not transposed else reported
        if expected != sums or (not transposed and reported[0] != elector_total):
            raise ReviewRequired(f"{year}: state rows sum to {[elector_total] + sums}, but the total row reports {reported}.")
    normalized.accounting = {"states": len(seen), "recipients": len(recipients), "electors": elector_total,
                             "total_row_checked": int(reported is not None)}
    normalized.diagnostics.append({"recipients": [f"{role}:{label}" for label, role in recipients], "sums": sums})
    return complete_run(connection, context, normalized)


def election_day_known(year: int) -> str:
    return general_election_date(year).isoformat() if year >= 1848 else f"{year}"
