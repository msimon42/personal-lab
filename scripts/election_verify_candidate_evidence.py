"""Read-only check of exact retained candidate-party joins against official FEC bytes."""
from __future__ import annotations

import argparse
from pathlib import Path

from election_model.fec import verify_candidate_evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify sources without writing files (the default).")
    parser.add_argument("--fec", type=Path, default=Path("data/elections/sources/fec-cn26.zip"))
    parser.add_argument("--votehub", type=Path,
                        default=Path("data/elections/sources/votehub-2026-10-01_to_2026-11-01.json"))
    parser.add_argument("--evidence", type=Path,
                        default=Path("data/elections/sources/fec-votehub-candidate-evidence.json"))
    args = parser.parse_args()
    report = verify_candidate_evidence(args.fec.read_bytes(), args.votehub.read_bytes(), args.evidence.read_bytes())
    print(report.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
