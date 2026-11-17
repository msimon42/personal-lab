"""Derive the committed federal reference table from archived, hash-pinned sources.

Usage: uv run python scripts/election_data_build_reference.py --data-dir <root>
House seats come from the Census Bureau's historical apportionment CSV. Senate classes are
derived from regular (non-special) general elections in the MIT Senate file and must satisfy
the structural invariants (two classes per state; 33/33/34 seats).
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "packages/election_data/src/election_data/manifests/reference-federal.json"
CLASS_BY_REMAINDER = {2: 1, 4: 2, 0: 3}  # 2018 -> class I, 2020 -> class II, 2022 -> class III


def artifact(root: Path, key: str) -> tuple[Path, str]:
    with sqlite3.connect(root / "db" / "elections.sqlite") as connection:
        row = connection.execute(
            "SELECT a.archive_path, a.sha256 FROM artifact_retrieval r JOIN source_artifact a USING (artifact_id) "
            "WHERE r.artifact_key=? ORDER BY r.retrieved_at DESC LIMIT 1", (key,)).fetchone()
    if row is None:
        raise SystemExit(f"Fetch {key} first.")
    return root / row[0], row[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    arguments = parser.parse_args()
    apportionment_path, apportionment_hash = artifact(arguments.data_dir, "apportionment.csv")
    senate_path, senate_hash = artifact(arguments.data_dir, "1976-2024-senate-state.tab")
    states: dict[str, dict] = {}
    with senate_path.open(encoding="utf-8", newline="") as handle:
        classes = defaultdict(set)
        for row in csv.DictReader(handle, delimiter="\t"):
            states[row["state"].title()] = {"postal": row["state_po"], "fips": row["state_fips"].zfill(2)}
            if row["special"] == "False" and row["stage"].lower() == "gen" and int(row["year"]) % 2 == 0:
                classes[row["state_po"]].add(CLASS_BY_REMAINDER[int(row["year"]) % 6])
    # Georgia codes its decisive runoffs as "gen"; both rounds belong to the same class year.
    if any(len(value) != 2 for value in classes.values()) or len(classes) != 50:
        raise SystemExit(f"Senate classes are not two per state: {dict(classes)}")
    counts = {number: sum(number in value for value in classes.values()) for number in (1, 2, 3)}
    if counts != {1: 33, 2: 33, 3: 34}:
        raise SystemExit(f"Senate class sizes {counts} are not 33/33/34.")
    states["District Of Columbia"] = {"postal": "DC", "fips": "11"}
    seats: dict[str, dict[str, int]] = defaultdict(dict)
    with apportionment_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            name = row["Name"].title()
            if row["Geography Type"] == "State" and name in states and row["Number of Representatives"].strip().isdigit():
                seats[states[name]["postal"]][row["Year"]] = int(row["Number of Representatives"])
    for census in ("1970", "1980", "1990", "2000", "2010", "2020"):
        total = sum(value.get(census, 0) for value in seats.values())
        if total != 435:
            raise SystemExit(f"Apportionment for {census} sums to {total}, not 435.")
    reference = {
        "reference_version": 1,
        "derived_from": {"census_apportionment_csv_sha256": apportionment_hash, "mit_senate_file_sha256": senate_hash},
        "states": [{"postal": value["postal"], "fips": value["fips"], "name": name.replace(" Of ", " of ")}
                   for name, value in sorted(states.items(), key=lambda item: item[1]["fips"])],
        "house_seats_by_census": {postal: dict(sorted(value.items())) for postal, value in sorted(seats.items())},
        "senate_classes": {postal: sorted(value) for postal, value in sorted(classes.items())},
    }
    TARGET.write_text(json.dumps(reference, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {TARGET}")


if __name__ == "__main__":
    main()
