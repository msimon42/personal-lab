"""Normalize already downloaded public polling bytes into immutable local artifacts.

Run from the repository with:
  uv run python scripts/election_import_history.py --source data/elections/sources/538-raw-polls.csv

No network call is made; source hashes identify the exact archive used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from election_model.historical import aggregate_history, read_538_archive


def write_immutable(path: Path, content: str) -> None:
    """Repeat identical normalization safely; never overwrite a different snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError(f"Refusing to overwrite different evidence: {path}")
        return
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def retrieval_time(source: Path, raw: bytes, explicit: str | None) -> datetime:
    """Reuse the immutable manifest timestamp so fresh checkouts reproduce output."""
    if explicit:
        return datetime.fromisoformat(explicit)
    manifest = source.parent / "manifest.json"
    if manifest.is_file():
        records = json.loads(manifest.read_text(encoding="utf-8")).get("records", [])
        match = next((record for record in records if record.get("path") == source.name), None)
        if match:
            if match["sha256"] != hashlib.sha256(raw).hexdigest():
                raise ValueError("Source bytes no longer match their immutable retrieval manifest.")
            return datetime.fromisoformat(match["retrieved_at"])
    return datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path,
                        default=Path("packages/election_model/src/election_model/data/history-v3.json"))
    parser.add_argument("--normalized-polls", type=Path, default=Path("data/elections/normalized/archive-polls-v3.json"))
    parser.add_argument("--retrieved-at", default=None,
                        help="ISO8601 retrieval time; defaults to matching source manifest, then source mtime in UTC.")
    args = parser.parse_args()
    raw = args.source.read_bytes()
    retrieved = retrieval_time(args.source, raw, args.retrieved_at)
    archive = read_538_archive(raw, retrieved_at=retrieved)
    history, metadata = aggregate_history(archive)
    snapshot = {"metadata": metadata, "races": [row.model_dump(mode="json") for row in history]}
    outputs = {args.output: json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
               args.normalized_polls: archive.model_dump_json(indent=2) + "\n"}
    for path, content in outputs.items():
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise ValueError(f"Refusing to overwrite different evidence: {path}")
    for path, content in outputs.items():
        write_immutable(path, content)
    print(json.dumps({"history_path": str(args.output), "normalized_polls_path": str(args.normalized_polls),
                      "race_count": len(history), "cycles": sorted({r.cycle for r in history}),
                      "source_sha256": archive.source_sha256}, sort_keys=True))


if __name__ == "__main__":
    main()
