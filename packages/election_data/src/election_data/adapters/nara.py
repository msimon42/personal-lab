"""NARA 2024 Electoral College results page -> allocations and actual electoral votes.

Electoral votes are stored separately from popular votes. Maine and Nebraska district
components come from reviewed curation of the page notes and must sum to the table.
"""
from __future__ import annotations

import sqlite3

from ..core import DataRoot, ReviewRequired, canonical_json, stable_id
from ..pipeline import (Normalized, begin_run, complete_run, election_id, existing_result, load_curation,
                        person_id, state_versions)

ADAPTER_ID = "nara-electoral-college-page"
ADAPTER_VERSION = "1"
SOURCE_ID = "nara-electoral-college-2024"
HEADER_FIRST = ["State", "Number of Electoral Votes for Each State", "For President", "For Vice-President"]
RECIPIENTS = [("Kamala D. Harris, of California", "kamala-d-harris", "president"),
              ("Donald J. Trump, of Florida", "donald-j-trump", "president"),
              ("Tim Walz, of Minnesota", "tim-walz", "vice_president"),
              ("JD Vance, of Ohio", "jd-vance", "vice_president")]


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _count(text: str) -> int:
    text = text.strip()
    if text in ("-", "", "—"):
        return 0
    if not text.isdigit():
        raise ReviewRequired(f"Electoral vote cell {text!r} is not a count.")
    return int(text)


def import_page(root: DataRoot, connection: sqlite3.Connection, artifact_id: str) -> dict:
    from ..readers import read_html_tables

    curation, curation_hash = load_curation()
    context = begin_run(root, connection, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                        artifacts={"page": artifact_id}, mapping_version=curation["mapping_version"],
                        mapping_hash=curation_hash, config={})
    if context.existing:
        return existing_result(connection, context)
    tables = read_html_tables(context.paths["page"])
    table = next((rows for rows in tables if rows and rows[0] == HEADER_FIRST), None)
    if table is None or [_clean(cell) for cell in table[1]] != [name for name, _, _ in RECIPIENTS]:
        raise ReviewRequired("NARA electoral table layout changed.")
    names = {row["name"]: postal for postal, row in state_versions(connection).items()}
    event = election_id(curation["election"]["natural_key"])
    components = next(d["scope"] for d in curation["decisions"] if d["decision_type"] == "electoral_components")
    normalized = Normalized()
    totals = {"president": {}, "vice_president": {}}
    seen = set()
    for index, row in enumerate(table[2:], start=3):
        cells = [_clean(cell) for cell in row]
        if not cells or cells[0] in ("Total", "Notes"):
            if cells and cells[0] == "Total":
                totals["reported_total"] = [_count(cell) for cell in cells[1:6]]
            continue
        name = cells[0].rstrip("0123456789 ").strip()
        postal = names.get(name)
        if postal is None or len(cells) != 6:
            raise ReviewRequired(f"NARA row {index} ({cells[0]!r}) does not map to a state.")
        seen.add(postal)
        state_version = state_versions(connection)[postal]["geo_version_id"]
        allocation = stable_id("allocation", event, postal, "state_total")
        electors = _count(cells[1])
        normalized.other.append(("INSERT OR IGNORE INTO electoral_allocation VALUES (?,?,?,?,?,?,?)",
                                 (allocation, event, state_version, "state_total", "state", electors, artifact_id)))
        locator = canonical_json({"table": "electoral-college-2024", "row": index, "state": name})
        for (label, person_key, role), cell in zip(RECIPIENTS, cells[2:6]):
            votes = _count(cell)
            totals[role][person_key] = totals[role].get(person_key, 0) + votes
            if votes:
                identifier = stable_id("ev", context.identity, allocation, person_key, role)
                normalized.other.append(("INSERT INTO electoral_vote VALUES (?,?,?,?,?,?,?,?)",
                                         (identifier, allocation, person_id(person_key), role, votes, artifact_id,
                                          context.run_id, locator)))
        if postal in components:
            component_sum = sum(part["electors"] for part in components[postal].values())
            if component_sum != electors:
                raise ReviewRequired(f"{postal} component electors {component_sum} != NARA total {electors}.")
            for key, part in components[postal].items():
                kind = "statewide_at_large" if key == "statewide_at_large" else "congressional_district"
                normalized.other.append(("INSERT OR IGNORE INTO electoral_allocation VALUES (?,?,?,?,?,?,?)",
                                         (stable_id("allocation", event, postal, kind, key), event, state_version,
                                          kind, key, part["electors"], artifact_id)))
    if seen != set(curation["states_with_electors"]):
        raise ReviewRequired(f"NARA table states differ from expected: missing {sorted(set(curation['states_with_electors']) - seen)}.")
    reported = totals.get("reported_total")
    electors_total = sum(values[5] for statement, values in normalized.other
                         if "electoral_allocation" in statement and values[3] == "state_total")
    computed = [electors_total, totals["president"].get("kamala-d-harris", 0),
                totals["president"].get("donald-j-trump", 0), totals["vice_president"].get("tim-walz", 0),
                totals["vice_president"].get("jd-vance", 0)]
    if reported != computed:
        raise ReviewRequired(f"NARA state rows sum to {computed}, but the Total row reports {reported}.")
    normalized.accounting = {"table_rows": len(table), "states": len(seen)}
    normalized.diagnostics.append({"computed_totals": {role: totals[role] for role in ("president", "vice_president")},
                                   "reported_total_row": reported})
    return complete_run(connection, context, normalized)
