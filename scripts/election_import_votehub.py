"""Build an opt-in, party-verified VoteHub research sensitivity snapshot.

This script performs no network access. API dates, samples and values remain
provider-reported unless separately checked against primary poll releases.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path

from election_model.contracts import Race
from election_model.fec import verify_candidate_evidence
from election_model.votehub import CandidateEvidence, ExistingSurvey, audit_votehub


def write_immutable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise ValueError(f"Refusing to overwrite different evidence: {path}")
        return
    with path.open("x", encoding="utf-8") as output:
        output.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path,
                        default=Path("data/elections/sources/votehub-2026-10-01_to_2026-11-01.json"))
    parser.add_argument("--candidate-evidence", type=Path,
                        default=Path("data/elections/sources/fec-votehub-candidate-evidence.json"))
    parser.add_argument("--fec", type=Path, default=Path("data/elections/sources/fec-cn26.zip"))
    parser.add_argument("--primary-snapshot", type=Path,
                        default=Path("packages/election_model/src/election_model/data/current-v2.json"))
    parser.add_argument("--manifest", type=Path, default=Path("data/elections/sources/votehub-manifest.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("packages/election_model/src/election_model/data/votehub-v2.json"))
    args = parser.parse_args()
    raw = args.source.read_bytes()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source = next(row for row in manifest["records"] if row["path"] == args.source.name)
    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
        raise ValueError("VoteHub bytes differ from their immutable source manifest.")
    evidence_raw = args.candidate_evidence.read_bytes()
    # Evidence is input data, not authority. Reconstruct every exact public-party
    # join from retained FEC bytes before labels can enter QA or any output write.
    verification = verify_candidate_evidence(args.fec.read_bytes(), raw, evidence_raw)
    evidence_data = json.loads(evidence_raw)
    candidate_date = date.fromisoformat(evidence_data["source_zip"]["retrieved_at"][:10])
    candidates = []
    for mapping in evidence_data["mappings"]:
        race_id = (f"2026-{mapping['state']}-SEN" if mapping["chamber"] == "senate" else
                   f"2026-{mapping['state']}-{mapping['district'] if mapping['district'] != '00' else '01'}")
        candidates.append(CandidateEvidence(
            subject=mapping["subject"], choice=mapping["answer"], race_id=race_id,
            chamber=mapping["chamber"], state=mapping["state"], party=mapping["party"],
            candidate_id=mapping["candidate_id"], source_name=mapping["fec_name"], evidence_date=candidate_date,
            source_url=mapping["source_url"]))
    primary_raw = args.primary_snapshot.read_bytes()
    primary = json.loads(primary_raw)
    existing = [ExistingSurvey.model_validate({key: poll[key] for key in
                ("race_id", "pollster", "start_date", "end_date", "poll_id")}) for poll in primary["polls"]]
    aliases = {"Emerson College": "Emerson College Polling"}
    audit = audit_votehub(raw, as_of=date(2026, 11, 1), candidate_evidence=candidates,
                          existing_surveys=existing, pollster_aliases=aliases)
    selected = audit.candidate_verified_polls
    mapping_by_race = {item.race_id: item for item in candidates}
    races = [Race(
        race_id=race_id, state=mapping_by_race[race_id].state, chamber=mapping_by_race[race_id].chamber,
        cycle=2026, election_date=date(2026, 11, 3), voting_system="plurality", poll_round="decisive",
        map_vintage="2026 poll district label; boundary vintage independently unverified"
        if mapping_by_race[race_id].chamber == "house" else None)
        for race_id in sorted({poll.race_id for poll in selected})]
    metadata = {
        "schema_version": "2.0", "dataset_id": "votehub-2026-11-01", "as_of": "2026-11-01",
        "kind": "provider_sourced_party_verified_sensitivity",
        "retrieved_at": source["retrieved_at"],
        "coverage": f"{len(selected)} provider-sourced polls across {len(races)} races; incomplete convenience sample",
        "coverage_counts": {"raw_records": audit.source_records, "house_senate_records": audit.target_records,
                            "selected_polls": len(selected), "selected_races": len(races),
                            "polling_firms": len({poll.pollster for poll in selected}),
                            "chambers": dict(Counter(race.chamber for race in races)),
                            "rejection_reasons": audit.rejection_counts},
        "candidate_evidence_sha256": hashlib.sha256(evidence_raw).hexdigest(),
        "candidate_verification": verification.model_dump(mode="json"),
        "primary_snapshot_sha256": hashlib.sha256(primary_raw).hexdigest(),
        "candidate_matching_policy": evidence_data["matching_policy"],
        "sources": [
            {"source_id": "votehub-2026-11-01", "title": "VoteHub October 2026 public polling API snapshot",
             "publisher": "VoteHub", "url": source["url"], "retrieved_at": source["retrieved_at"],
             "published_date": None, "sha256": source["sha256"],
             "coverage": f"{audit.source_records} feed records; {audit.target_records} House/Senate questions before conservative QA.",
             "limitations": ["CC-BY-4.0; API contains no per-answer party metadata.",
                             "created_at is a provider-added date, not a verified publisher release."]},
            {"source_id": "fec-candidate-master-2026", "title": "FEC 2026 candidate master snapshot",
             "publisher": "Federal Election Commission", "url": evidence_data["source_zip"]["url"],
             "retrieved_at": evidence_data["source_zip"]["retrieved_at"], "published_date": None,
             "sha256": evidence_data["source_zip"]["sha256"],
             "coverage": "Exact full-name, office, state and district matches restricted to election year 2026.",
             "limitations": evidence_data["limitations"]},
            {"source_id": "votehub-api-documentation", "title": "VoteHub public API documentation and license",
             "publisher": "VoteHub", "url": "https://votehub.com/polls/api/",
             "retrieved_at": source["retrieved_at"], "published_date": None, "sha256": None,
             "coverage": "CC-BY-4.0 and end-date filter definitions verified with web retrieval.",
             "limitations": ["Direct shell HTML retrieval returned HTTP403; original documentation HTML not archived."]},
        ],
        "limitations": audit.limitations + [
            "Every included sample count remains unverified; the engine uses placeholder n=1000 rather than claiming source subgroup n.",
            "Some provider samples may refer to registered-voter totals rather than the reported likely-voter subset.",
            "No pollster mode, likely-voter screen or weighting adjustment is inferred from an absent API field.",
            "Current House geography follows the poll label; no previous-district baseline or map transfer is used.",
            "Alaska ranked-choice races are excluded because first-choice D/R margins do not identify final-round outcomes.",
            "Primary snapshot matching firm/contest/field windows take precedence over provider variants, including leaned-voter shares.",
        ],
    }
    result = {"metadata": metadata, "races": [race.model_dump(mode="json") for race in races],
              "polls": [poll.model_dump(mode="json") for poll in sorted(selected, key=lambda poll: poll.poll_id)],
              "rejections": [row.model_dump(mode="json") for row in audit.rows if row.status == "rejected"],
              "qa": [row.model_dump(mode="json") for row in audit.rows if row.status != "rejected"]}
    write_immutable(args.output, json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "polls": len(selected), "races": len(races),
                      "sha256": hashlib.sha256(args.output.read_bytes()).hexdigest()}, sort_keys=True))


if __name__ == "__main__":
    main()
