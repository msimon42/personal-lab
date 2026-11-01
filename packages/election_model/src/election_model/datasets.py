"""Read only trusted, packaged public snapshots with pinned content hashes."""
from __future__ import annotations

import hashlib
from importlib.resources import files
import json
from typing import Any

from .contracts import HistoricalRace


def load_document(name: str) -> dict[str, Any]:
    if name not in {"history.json", "history-v2.json", "history-v3.json", "current.json", "current-v2.json", "research.json", "votehub.json", "votehub-v2.json"}:
        raise ValueError("Unknown trusted election dataset.")
    root = files("election_model").joinpath("data")
    manifest_bytes = root.joinpath("manifest.json").read_bytes()
    if len(manifest_bytes) > 8192:
        raise ValueError("Election manifest exceeds its byte budget.")
    manifest = json.loads(manifest_bytes)
    record = manifest["files"][name]
    raw = root.joinpath(name).read_bytes()
    if len(raw) > 2 * 1024 * 1024 or len(raw) != record["bytes"]:
        raise ValueError("Election evidence size differs from the pinned manifest.")
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ValueError("Election evidence differs from the pinned content hash.")
    return json.loads(raw)


def load_history() -> tuple[list[HistoricalRace], dict]:
    document = load_document("history-v3.json")
    if not 1 <= len(document["races"]) <= 3000:
        raise ValueError("Packaged historical coverage exceeds its contract.")
    return [HistoricalRace.model_validate(row) for row in document["races"]], document["metadata"]


def has_document(name: str) -> bool:
    manifest = json.loads(files("election_model").joinpath("data/manifest.json").read_bytes())
    return name in manifest["files"]
