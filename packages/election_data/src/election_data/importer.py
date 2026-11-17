"""Reviewed adapter registry and dispatch. Adapter IDs never come from source metadata."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from .adapters import census, fec, mit, mit_state, nara
from .core import DataRoot, InvalidInput, canonical_json, sha256_text, transaction, utc_now


@dataclass(frozen=True)
class AdapterEntry:
    name: str
    source_id: str
    artifact_key: str
    adapter_id: str
    config: dict[str, Any]
    run: Callable[[DataRoot, sqlite3.Connection, str], dict]
    order: int
    group: str

    @property
    def config_hash(self) -> str:
        return sha256_text(canonical_json(self.config))


def _entries() -> dict[str, AdapterEntry]:
    entries = [
        AdapterEntry("census-state-2020", "census-cb-2020-20m", "cb_2020_us_state_20m.zip", census.ADAPTER_ID,
                     {"layer": "cb_2020_us_state_20m"},
                     lambda r, c, a: census.import_layer(r, c, a, "cb_2020_us_state_20m"), 1, "boundaries"),
        AdapterEntry("census-county-2020", "census-cb-2020-20m", "cb_2020_us_county_20m.zip", census.ADAPTER_ID,
                     {"layer": "cb_2020_us_county_20m"},
                     lambda r, c, a: census.import_layer(r, c, a, "cb_2020_us_county_20m"), 2, "boundaries"),
        AdapterEntry("fec-president-2024", fec.SOURCE_ID, "2024presgeresults.xlsx", fec.ADAPTER_ID,
                     {"sheet": fec.SHEET}, fec.import_workbook, 10, "results"),
        AdapterEntry("mit-state-president", "mit-state-president", "1976-2024-president.csv", "mit-state-president",
                     {"dataset": "president"}, lambda r, c, a: mit_state.import_dataset(r, c, a, "president"), 11,
                     "results"),
        AdapterEntry("mit-senate", "mit-senate", "1976-2024-senate-state.tab", "mit-senate", {"dataset": "senate"},
                     lambda r, c, a: mit_state.import_dataset(r, c, a, "senate"), 12, "results"),
        AdapterEntry("mit-house", "mit-house", "1976-2024-house.tab", "mit-house", {"dataset": "house"},
                     lambda r, c, a: mit_state.import_dataset(r, c, a, "house"), 13, "results"),
        AdapterEntry("mit-county-president", mit.SOURCE_ID, "countypres_2000-2024.tab", mit.ADAPTER_ID,
                     {"years": "all"}, mit.import_file, 14, "results"),
    ]
    for congress in sorted([*census.UCLA_CONGRESSES, *census.CENSUS_CD]):
        ucla = congress in census.UCLA_CONGRESSES
        key = f"districts{congress:03d}.zip" if ucla else f"{census.CENSUS_CD[congress]}.zip"
        entries.append(AdapterEntry(
            f"districts-congress-{congress}",
            "ucla-congressional-districts" if ucla else "census-cb-congressional-districts", key,
            "congressional-district-boundaries", {"congress": congress},
            lambda r, c, a, n=congress: census.import_districts(r, c, a, n), 3, "boundaries"))
    for year in nara.YEARS:
        entries.append(AdapterEntry(
            f"nara-electoral-{year}", nara.source_for(year), f"electoral-college-{year}.html", nara.ADAPTER_ID,
            {"year": year}, lambda r, c, a, y=year: nara.import_year(r, c, a, y), 20, "electoral"))
    return {entry.name: entry for entry in entries}


ADAPTERS = _entries()
# Imports a release cannot be built without; everything else is used when present.
REQUIRED = ("census-state-2020", "census-county-2020")


def ordered() -> list[AdapterEntry]:
    return sorted(ADAPTERS.values(), key=lambda entry: (entry.order, entry.name))


def run_adapter(root: DataRoot, connection: sqlite3.Connection, adapter: str, artifact_id: str) -> dict:
    if adapter not in ADAPTERS:
        raise InvalidInput(f"Unknown adapter {adapter}; see 'election-data adapters'.")
    try:
        result = ADAPTERS[adapter].run(root, connection, artifact_id)
    except BaseException:
        # The writer lock guarantees any still-running run belongs to this process.
        with transaction(connection):
            connection.execute("UPDATE import_run SET status='failed', finished_at=? WHERE status='running'",
                               (utc_now(),))
        raise
    return {"adapter": adapter, "artifact_id": artifact_id, **result}


def latest_runs(connection: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Latest completed run per registered adapter (by adapter id and configuration)."""
    runs = {}
    for entry in ADAPTERS.values():
        row = connection.execute(
            "SELECT * FROM import_run WHERE adapter_id=? AND config_hash=? AND status='completed' "
            "ORDER BY finished_at DESC, rowid DESC LIMIT 1", (entry.adapter_id, entry.config_hash)).fetchone()
        if row is not None:
            runs[entry.name] = row
    return runs
