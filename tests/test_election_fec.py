"""Temporary synthetic candidate masters test exact public-party join verification."""
import hashlib
import io
import json
from pathlib import Path
import runpy
import sys
import zipfile

import pytest

from election_model.fec import FEC_2026_URL, normalize_full_name, read_fec_candidates, verify_candidate_evidence


def sources(tmp_path):
    rows = [
        "H6PA01234|EXAMPLE, ALICE DR.|DEM|2026|PA|H|01|O|C|C00000001|PRIVATE_ADDRESS_SENTINEL||CITY|PA|00000",
        "H6PA01235|OTHER, BOB|REP|2026|PA|H|01|O|C|C00000002|PRIVATE_ADDRESS_SENTINEL||CITY|PA|00000",
    ]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("cn.txt", "\n".join(rows) + "\n")
    raw_zip = output.getvalue()
    poll = {"id": "synthetic1", "poll_type": "us-representative", "sample_size": 700, "population": "lv",
            "url": "https://example.org/synthetic-poll", "created_at": "2026-10-25",
            "start_date": "2026-10-20", "end_date": "2026-10-22", "pollster": "Synthetic Firm",
            "answers": [{"choice": "Alice Example", "pct": 43.0}, {"choice": "Bob Other", "pct": 47.0},
                        {"choice": "Jamie Mystery", "pct": 1.0}], "seat_name": None, "sponsors": [],
            "internal": False, "partisan": None, "subject": "2026 PA-01"}
    raw_poll = json.dumps([poll]).encode()
    mappings = []
    for candidate_id, name, answer, party in [("H6PA01234", "EXAMPLE, ALICE DR.", "Alice Example", "DEM"),
                                              ("H6PA01235", "OTHER, BOB", "Bob Other", "REP")]:
        mappings.append({"subject": "2026 PA-01", "answer": answer, "chamber": "house", "state": "PA",
                         "district": "01", "match_method": "full_name_exact_after_comma_reordering_punctuation_and_titles_only",
                         "candidate_id": candidate_id, "fec_name": name, "party": party, "election_year": 2026,
                         "candidate_status": "C", "source_url": f"https://www.fec.gov/data/candidate/{candidate_id}/?cycle=2026",
                         "source_bulk_url": FEC_2026_URL, "usable_dr_label": True})
    evidence = {"source_zip": {"url": FEC_2026_URL, "sha256": hashlib.sha256(raw_zip).hexdigest(),
                               "byte_size": len(raw_zip)}, "mappings": mappings,
                "unmatched_answers": [{"subject": "2026 PA-01", "answer": "Jamie Mystery"}],
                "counts": {"unique_subject_answers": 3, "matched_full_names": 2, "usable_dem_rep": 2, "unmatched": 1}}
    (tmp_path / "fec.zip").write_bytes(raw_zip)
    (tmp_path / "polls.json").write_bytes(raw_poll)
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    return raw_zip, raw_poll, evidence


def test_exact_join_verifies_all_source_fields_and_omits_addresses(tmp_path):
    raw_zip, raw_poll, evidence = sources(tmp_path)
    report = verify_candidate_evidence(raw_zip, raw_poll, json.dumps(evidence).encode())
    assert report.verified_mappings == 2
    assert report.usable_dem_rep_labels == 2
    assert report.unmatched_answers == 1
    assert "PRIVATE_ADDRESS_SENTINEL" not in report.model_dump_json()
    candidates, _ = read_fec_candidates(raw_zip)
    assert all("PRIVATE_ADDRESS_SENTINEL" not in candidate.model_dump_json() for candidate in candidates)


def test_changed_retained_party_fails_against_raw_fec_master(tmp_path):
    raw_zip, raw_poll, evidence = sources(tmp_path)
    evidence["mappings"][0]["party"] = "REP"
    with pytest.raises(ValueError, match="field party disagrees with raw FEC"):
        verify_candidate_evidence(raw_zip, raw_poll, json.dumps(evidence).encode())


def test_candidate_id_office_and_district_must_match_exact_source(tmp_path):
    raw_zip, raw_poll, evidence = sources(tmp_path)
    for field, changed in [("candidate_id", "H6PA01999"), ("district", "02"), ("chamber", "senate")]:
        changed_evidence = json.loads(json.dumps(evidence))
        changed_evidence["mappings"][0][field] = changed
        with pytest.raises(ValueError, match="disagrees with raw FEC"):
            verify_candidate_evidence(raw_zip, raw_poll, json.dumps(changed_evidence).encode())


def test_source_hash_and_answer_identity_are_pinned(tmp_path):
    raw_zip, raw_poll, evidence = sources(tmp_path)
    with pytest.raises(ValueError, match="source bytes or provenance"):
        verify_candidate_evidence(raw_zip + b"changed", raw_poll, json.dumps(evidence).encode())
    evidence["mappings"][0]["answer"] = "Al Example"
    with pytest.raises(ValueError, match="do not exactly match"):
        verify_candidate_evidence(raw_zip, raw_poll, json.dumps(evidence).encode())


def test_only_punctuation_titles_and_comma_order_are_normalized():
    assert normalize_full_name("EXAMPLE, ALICE DR.") == normalize_full_name("Alice Example")
    assert normalize_full_name("Mike Example") != normalize_full_name("EXAMPLE, MICHAEL")
    assert normalize_full_name("Alice Example") != normalize_full_name("EXAMPLE, ALICE MIDDLE")


def test_zip_members_never_extract_arbitrary_paths(tmp_path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("../cn.txt", "untrusted")
    sentinel = tmp_path / "cn.txt"
    sentinel.write_text("unchanged")
    with pytest.raises(ValueError, match="only a bounded cn.txt"):
        read_fec_candidates(output.getvalue())
    assert sentinel.read_text() == "unchanged"


@pytest.mark.parametrize("changed_field,changed_value", [("party", "REP"), ("chamber", "senate"),
                                                         ("district", "02")])
def test_importer_refuses_tampered_candidate_evidence_before_any_write(tmp_path, monkeypatch,
                                                                     changed_field, changed_value):
    _, raw_poll, evidence = sources(tmp_path)
    evidence["mappings"][0][changed_field] = changed_value
    (tmp_path / "evidence.json").write_text(json.dumps(evidence))
    manifest = {"records": [{"path": "polls.json", "sha256": hashlib.sha256(raw_poll).hexdigest()}]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    output = tmp_path / "outputs" / "votehub-v2.json"
    script = Path(__file__).resolve().parents[1] / "scripts/election_import_votehub.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--source", str(tmp_path / "polls.json"),
                                      "--candidate-evidence", str(tmp_path / "evidence.json"),
                                      "--fec", str(tmp_path / "fec.zip"),
                                      "--manifest", str(tmp_path / "manifest.json"),
                                      "--primary-snapshot", str(tmp_path / "missing-primary.json"),
                                      "--output", str(output)])
    with pytest.raises(ValueError, match="disagrees with raw FEC"):
        runpy.run_path(str(script), run_name="__main__")
    assert not output.exists()
    assert not output.parent.exists()
