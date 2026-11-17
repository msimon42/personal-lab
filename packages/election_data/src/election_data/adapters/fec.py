"""FEC official 2024 presidential general results workbook -> state-level observations."""
from __future__ import annotations

import re
import sqlite3

from ..core import DataRoot, ReviewRequired, canonical_json, stable_id, transaction
from ..pipeline import (Normalized, begin_run, complete_run, ensure_aggregate, ensure_contests, ensure_ticket,
                        existing_result, load_curation, round_key_for, state_versions, statewide_contest, unit_id)

ADAPTER_ID = "fec-president-workbook"
ADAPTER_VERSION = "1"
SOURCE_ID = "fec-federal-elections-2024-president"
SHEET = "OFFICIAL 2024 PRES GE RESULTS"
FIXED = {"A": "STATE", "B": "ELECTORAL VOTES", "C": "ELECTORAL VOTE: TRUMP (R)", "D": "ELECTORAL VOTE: HARRIS (D)"}
TOTAL_LABEL = "TOTAL VOTES"


def provisional_key(label: str) -> str:
    return "fec-label-" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")


def import_workbook(root: DataRoot, connection: sqlite3.Connection, artifact_id: str) -> dict:
    from ..readers import read_xlsx

    curation, curation_hash = load_curation()
    context = begin_run(root, connection, adapter_id=ADAPTER_ID, adapter_version=ADAPTER_VERSION,
                        artifacts={"workbook": artifact_id}, mapping_version=curation["mapping_version"],
                        mapping_hash=curation_hash, config={"sheet": SHEET})
    if context.existing:
        return existing_result(connection, context)
    rows = read_xlsx(context.paths["workbook"])
    sheets = {row["sheet"] for row in rows}
    if sheets != {SHEET}:
        raise ReviewRequired(f"FEC workbook layout changed: sheets {sorted(sheets)}.")
    header = rows[0]["cells"]
    if rows[0]["row"] != 1 or any(header.get(column) != label for column, label in FIXED.items()):
        raise ReviewRequired("FEC workbook header changed.")
    total_column = next((column for column, label in header.items() if label == TOTAL_LABEL), None)
    if total_column is None:
        raise ReviewRequired("FEC workbook lacks a TOTAL VOTES column.")
    labels = curation["source_labels"][SOURCE_ID]
    candidate_columns = {column: label for column, label in header.items()
                         if column not in FIXED and column != total_column}
    ensure_contests(connection, curation)
    states = state_versions(connection)
    normalized = Normalized()
    accounting = {"rows_read": len(rows), "header_rows": 1, "state_rows": 0, "summary_or_note_rows": 0,
                  "candidate_cells": 0, "blank_candidate_cells": 0}
    electoral_cross_check = []
    with transaction(connection):
        for row in rows[1:]:
            cells = row["cells"]
            postal = cells.get("A")
            if postal not in curation["states_with_electors"]:
                accounting["summary_or_note_rows"] += 1
                continue
            accounting["state_rows"] += 1
            contest = statewide_contest(postal, curation)
            set_id = stable_id("set", context.identity, contest, "state")
            normalized.result_sets.append((
                set_id, SOURCE_ID, contest, "FEC Official 2024 Presidential General Election Results (2025-01-16)",
                "official_compilation_of_certified", "2025-01-16T00:00:00Z", None, "official_state_totals", "state",
                "Write-in versus ballot-line status (italics in the workbook) is not captured."))
            unit = unit_id(f"state:{states[postal]['fips']}")
            round_key = round_key_for(postal, curation)
            for column, label in sorted(candidate_columns.items(), key=lambda item: (len(item[0]), item[0])):
                raw = cells.get(column)
                if raw is None:
                    accounting["blank_candidate_cells"] += 1
                    continue
                if not raw.isdigit():
                    raise ReviewRequired(f"Row {row['row']} column {column}: {raw!r} is not a count.")
                if label in labels["candidates"]:
                    option = ensure_ticket(connection, curation, contest, labels["candidates"][label])
                elif label in labels["aggregates"]:
                    option = ensure_aggregate(connection, contest, labels["aggregates"][label], label.title())
                else:
                    option = ensure_ticket(connection, curation, contest, provisional_key(label),
                                           provisional_label=f"{label.title()} (FEC surname label, unresolved)")
                observation = stable_id("vobs", set_id, unit, option, round_key, "all", "b1")
                normalized.votes.append((observation, set_id, unit, option, round_key, "all", "b1", int(raw), "reported",
                                         f"row:{row['row']}:{column}", label, None))
                normalized.evidence.append((observation, artifact_id, canonical_json(
                    {"sheet": SHEET, "cell": f"{column}{row['row']}"})))
                accounting["candidate_cells"] += 1
            raw_total = cells.get(total_column)
            if raw_total is None or not raw_total.isdigit():
                raise ReviewRequired(f"Row {row['row']}: missing TOTAL VOTES.")
            metric = stable_id("mobs", set_id, unit, round_key, "all", "total_votes_all_options")
            normalized.metrics.append((metric, set_id, unit, round_key, "all", "b1", "total_votes_all_options",
                                       int(raw_total), "reported", f"row:{row['row']}:{total_column}"))
            normalized.evidence.append((metric, artifact_id, canonical_json(
                {"sheet": SHEET, "cell": f"{total_column}{row['row']}"})))
            electoral_cross_check.append({"state": postal, "electors": cells.get("B"), "trump": cells.get("C"),
                                          "harris": cells.get("D")})
    normalized.accounting = accounting
    normalized.diagnostics.append({"fec_electoral_columns": electoral_cross_check})
    if accounting["state_rows"] != len(curation["states_with_electors"]):
        raise ReviewRequired(f"FEC workbook has {accounting['state_rows']} state rows; expected 51.")
    return complete_run(connection, context, normalized)
