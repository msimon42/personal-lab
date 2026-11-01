"""Verify retained public candidate-party joins against exact official FEC bytes.

Only public candidate identity, office and affiliation fields are projected. ZIP
members are read in memory; addresses are neither returned nor extracted.
"""
from __future__ import annotations

from collections import defaultdict
import csv
import hashlib
import io
import json
import re
import zipfile
from typing import Literal

from pydantic import Field

from .contracts import Contract
from .votehub import VoteHubRecord


FEC_2026_URL = "https://www.fec.gov/files/bulk-downloads/2026/cn26.zip"
STATE_CODES = dict(pair.split(":") for pair in (
    "Alabama:AL|Alaska:AK|Arizona:AZ|Arkansas:AR|California:CA|Colorado:CO|Connecticut:CT|Delaware:DE|"
    "Florida:FL|Georgia:GA|Hawaii:HI|Idaho:ID|Illinois:IL|Indiana:IN|Iowa:IA|Kansas:KS|Kentucky:KY|"
    "Louisiana:LA|Maine:ME|Maryland:MD|Massachusetts:MA|Michigan:MI|Minnesota:MN|Mississippi:MS|"
    "Missouri:MO|Montana:MT|Nebraska:NE|Nevada:NV|New Hampshire:NH|New Jersey:NJ|New Mexico:NM|"
    "New York:NY|North Carolina:NC|North Dakota:ND|Ohio:OH|Oklahoma:OK|Oregon:OR|Pennsylvania:PA|"
    "Rhode Island:RI|South Carolina:SC|South Dakota:SD|Tennessee:TN|Texas:TX|Utah:UT|Vermont:VT|"
    "Virginia:VA|Washington:WA|West Virginia:WV|Wisconsin:WI|Wyoming:WY"
).split("|"))


class FecCandidate(Contract):
    candidate_id: str
    name: str
    party: str
    election_year: int
    state: str
    office: Literal["H", "S"]
    district: str
    candidate_status: str


class RetainedMapping(Contract):
    subject: str
    answer: str
    chamber: Literal["house", "senate"]
    state: str
    district: str
    match_method: str
    candidate_id: str
    fec_name: str
    party: str
    election_year: int
    candidate_status: str
    source_url: str
    source_bulk_url: str
    usable_dr_label: bool


class CandidateEvidenceVerification(Contract):
    status: Literal["verified"] = "verified"
    source_zip_sha256: str
    votehub_sha256: str
    evidence_sha256: str
    source_fec_rows: int
    target_fec_rows: int
    subject_answer_pairs: int
    verified_mappings: int
    usable_dem_rep_labels: int
    unmatched_answers: int
    limitations: list[str] = Field(default_factory=list)


def normalize_full_name(name: str) -> str:
    """Reorder one surname comma and remove punctuation/titles; no fuzzy aliases."""
    if "," in name:
        surname, given = name.split(",", 1)
        name = given + " " + surname
    name = re.sub(r"\b(?:DR|MR|MRS|MS)\b", " ", name.upper())
    return re.sub(r"[^A-Z0-9]", "", name)


def read_fec_candidates(raw_zip: bytes) -> tuple[list[FecCandidate], int]:
    if not raw_zip or len(raw_zip) > 4 * 1024 * 1024:
        raise ValueError("FEC archive must contain 1 byte to 4 MiB.")
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as archive:
        entries = archive.infolist()
        if len(entries) != 1 or entries[0].filename != "cn.txt" or entries[0].file_size > 4 * 1024 * 1024:
            raise ValueError("FEC archive must contain only a bounded cn.txt member.")
        text = archive.read("cn.txt").decode("utf-8-sig")
    candidates = []
    row_count = 0
    for row in csv.reader(io.StringIO(text), delimiter="|"):
        row_count += 1
        if row_count > 20_000 or len(row) != 15:
            raise ValueError("FEC file must contain at most 20,000 rows with 15 columns.")
        if row[3] != "2026" or row[5] not in {"H", "S"}:
            continue
        candidates.append(FecCandidate(
            candidate_id=row[0], name=row[1], party=row[2], election_year=int(row[3]), state=row[4],
            office="H" if row[5] == "H" else "S", district=row[6], candidate_status=row[8]))
    return candidates, row_count


def _contest(subject: str, poll_type: str) -> tuple[str, str, str]:
    if poll_type == "us-senator":
        state = STATE_CODES.get(subject.removeprefix("2026 ")) if subject.startswith("2026 ") else None
        if state is None:
            raise ValueError("Unrecognized Senate subject in retained candidate evidence.")
        return state, "S", "00"
    match = re.fullmatch(r"2026 ([A-Z]{2})-(\d{2})", subject)
    if poll_type != "us-representative" or match is None:
        raise ValueError("Unrecognized House subject in retained candidate evidence.")
    state, district = match.groups()
    # The retained snapshot's two at-large labels use API01 and FEC00.
    if state in {"AK", "SD"} and district == "01":
        district = "00"
    return state, "H", district


def verify_candidate_evidence(raw_zip: bytes, votehub_raw: bytes,
                              evidence_raw: bytes) -> CandidateEvidenceVerification:
    """Reconstruct all exact joins, then reject any missing, altered or extra mapping."""
    if len(votehub_raw) > 4 * 1024 * 1024 or len(evidence_raw) > 2 * 1024 * 1024:
        raise ValueError("Candidate verification source bytes exceed declared bounds.")
    document = json.loads(evidence_raw)
    source = document["source_zip"]
    digest = hashlib.sha256(raw_zip).hexdigest()
    if source["sha256"] != digest or source["byte_size"] != len(raw_zip) or source["url"] != FEC_2026_URL:
        raise ValueError("FEC source bytes or provenance disagree with the retained evidence.")
    fec, row_count = read_fec_candidates(raw_zip)
    index: dict[tuple[str, str, str, str], list[FecCandidate]] = defaultdict(list)
    for candidate in fec:
        index[(candidate.state, candidate.office, candidate.district,
               normalize_full_name(candidate.name))].append(candidate)
    raw_polls = json.loads(votehub_raw)
    if not isinstance(raw_polls, list) or len(raw_polls) > 5000:
        raise ValueError("VoteHub input must contain at most 5,000 rows.")
    polls = [VoteHubRecord.model_validate_json(json.dumps(row)) for row in raw_polls]
    pairs: dict[tuple[str, str], str] = {}
    for poll in polls:
        if poll.poll_type in {"us-senator", "us-representative"}:
            for answer in poll.answers:
                key = poll.subject, answer.choice
                if key in pairs and pairs[key] != poll.poll_type:
                    raise ValueError("Subject/answer pair ambiguously identifies multiple offices.")
                pairs[key] = poll.poll_type
    expected: dict[tuple[str, str], FecCandidate] = {}
    unmatched: set[tuple[str, str]] = set()
    for key, poll_type in pairs.items():
        state, office, district = _contest(key[0], poll_type)
        matches = index.get((state, office, district, normalize_full_name(key[1])), [])
        if len(matches) == 1:
            expected[key] = matches[0]
        else:
            unmatched.add(key)
    raw_mappings = document["mappings"]
    if not isinstance(raw_mappings, list) or len(raw_mappings) > 2000:
        raise ValueError("Retained candidate mapping count exceeds declared bounds.")
    mappings = [RetainedMapping.model_validate(row) for row in raw_mappings]
    retained = {(row.subject, row.answer): row for row in mappings}
    if len(retained) != len(mappings) or set(retained) != set(expected):
        raise ValueError("Retained mappings do not exactly match reconstructed unique full-name joins.")
    for key, mapping in retained.items():
        candidate = expected[key]
        state, office, district = _contest(key[0], pairs[key])
        expected_values = dict(
            candidate_id=candidate.candidate_id, fec_name=candidate.name, party=candidate.party,
            election_year=candidate.election_year, state=state, district=district,
            chamber="house" if office == "H" else "senate", candidate_status=candidate.candidate_status,
            source_url=f"https://www.fec.gov/data/candidate/{candidate.candidate_id}/?cycle=2026",
            source_bulk_url=FEC_2026_URL, usable_dr_label=candidate.party in {"DEM", "REP"},
            match_method="full_name_exact_after_comma_reordering_punctuation_and_titles_only")
        for field, value in expected_values.items():
            if getattr(mapping, field) != value:
                raise ValueError(f"Retained candidate {mapping.candidate_id} field {field} disagrees with raw FEC data.")
    retained_unmatched = {(row["subject"], row["answer"]) for row in document["unmatched_answers"]}
    if retained_unmatched != unmatched:
        raise ValueError("Retained unmatched answers differ from reconstructed full-name joins.")
    counts = {"unique_subject_answers": len(pairs), "matched_full_names": len(expected),
              "usable_dem_rep": sum(row.party in {"DEM", "REP"} for row in mappings), "unmatched": len(unmatched)}
    if document["counts"] != counts:
        raise ValueError("Retained candidate evidence coverage counts disagree with reconstructed joins.")
    return CandidateEvidenceVerification(
        source_zip_sha256=digest, votehub_sha256=hashlib.sha256(votehub_raw).hexdigest(),
        evidence_sha256=hashlib.sha256(evidence_raw).hexdigest(), source_fec_rows=row_count,
        target_fec_rows=len(fec), subject_answer_pairs=len(pairs), verified_mappings=len(mappings),
        usable_dem_rep_labels=counts["usable_dem_rep"], unmatched_answers=len(unmatched), limitations=[
            "Exact candidate-reported FEC affiliation is verified; nomination and ballot access remain unverified.",
            "No nickname, omitted-middle-name, fuzzy, answer-order or other-party recoding is used.",
            "Only public candidate identity and office fields are projected; addresses are not extracted or returned.",
        ])
