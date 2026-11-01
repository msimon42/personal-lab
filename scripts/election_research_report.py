"""Reproduce immutable research outputs from trusted or explicitly supplied inputs.

No network access is performed. Existing different evidence is never overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from election_model.contracts import BacktestRequest, ForecastRequest
from election_model.service import ElectionService


def write_immutable(path: Path, raw: bytes) -> None:
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"Refusing to replace different research evidence: {path}")
        return
    with path.open("xb") as output:
        output.write(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--forecast-input", type=Path,
                        help="A bounded ForecastRequest JSON file; default is the four-race primary snapshot.")
    args = parser.parse_args()
    if args.forecast_input:
        with args.forecast_input.open("rb") as handle:
            raw = handle.read(262145)
        if len(raw) > 262144:
            raise ValueError("Forecast input exceeds 262144 bytes.")
        request = ForecastRequest.model_validate_json(raw)
    else:
        request = ForecastRequest.model_validate({"dataset_id": "us-2026-11-01",
                                                 "config": {"as_of": "2026-11-01"}})
    service = ElectionService()
    negative = request.model_copy(update={"config": request.config.model_copy(update={"reversal_shift": -3.0})})
    outputs = {
        "research-status.json": service.status(),
        "race-scenarios.json": service.forecast(request),
        "symmetric-republican-stress.json": service.forecast(negative),
        "historical-evaluation.json": service.backtest(BacktestRequest()),
    }
    encoded = {name: (value.model_dump_json(indent=2) + "\n").encode("utf-8") for name, value in outputs.items()}
    manifest = {"model_version": "0.1.0", "as_of": request.config.as_of.isoformat(),
                "interpretation": "Research scenarios and retrospective diagnostics; no chamber forecast.",
                "files": {name: {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in encoded.items()}}
    encoded["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    # Check every collision before writing any output, so a failed rerun cannot
    # mix generations in an existing directory.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name, raw in encoded.items():
        path = args.output_dir / name
        if path.exists() and path.read_bytes() != raw:
            raise ValueError(f"Refusing to replace different research evidence: {path}")
    for name, raw in encoded.items():
        write_immutable(args.output_dir / name, raw)
    print(json.dumps({"output_dir": str(args.output_dir), "files": sorted(encoded)}))


if __name__ == "__main__":
    main()
