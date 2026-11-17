"""Reviewed adapter registry and dispatch. Adapter IDs never come from source metadata."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from .core import DataRoot, InvalidInput, transaction, utc_now
from .adapters import census, fec, mit, nara


@dataclass(frozen=True)
class AdapterEntry:
    adapter_id: str
    source_id: str
    artifact_key: str
    run: Callable[[DataRoot, sqlite3.Connection, str], dict]
    order: int


ADAPTERS = {
    "census-state-2020": AdapterEntry("census-state-2020", "census-cb-2020-20m", "cb_2020_us_state_20m.zip",
                                      lambda r, c, a: census.import_layer(r, c, a, "cb_2020_us_state_20m"), 1),
    "census-county-2020": AdapterEntry("census-county-2020", "census-cb-2020-20m", "cb_2020_us_county_20m.zip",
                                       lambda r, c, a: census.import_layer(r, c, a, "cb_2020_us_county_20m"), 2),
    "fec-president-2024": AdapterEntry("fec-president-2024", fec.SOURCE_ID, "2024presgeresults.xlsx",
                                       fec.import_workbook, 3),
    "mit-county-president-2024": AdapterEntry("mit-county-president-2024", mit.SOURCE_ID,
                                              "countypres_2000-2024.tab",
                                              lambda r, c, a: mit.import_cycle(r, c, a, year=2024), 4),
    "nara-electoral-2024": AdapterEntry("nara-electoral-2024", nara.SOURCE_ID, "electoral-college-2024.html",
                                        nara.import_page, 5),
}


def run_adapter(root: DataRoot, connection: sqlite3.Connection, adapter: str, artifact_id: str) -> dict:
    if adapter not in ADAPTERS:
        raise InvalidInput(f"Unknown adapter {adapter}; choose one of {sorted(ADAPTERS)}.")
    try:
        result = ADAPTERS[adapter].run(root, connection, artifact_id)
    except BaseException:
        # The writer lock guarantees any still-running run belongs to this process.
        with transaction(connection):
            connection.execute("UPDATE import_run SET status='failed', finished_at=? WHERE status='running'",
                               (utc_now(),))
        raise
    return {"adapter": adapter, "artifact_id": artifact_id, **result}
