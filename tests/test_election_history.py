"""Synthetic CSV cases establish parser contracts, not real election findings."""
import csv
from datetime import date, datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import runpy

import pytest

from election_model.historical import aggregate_history, november_general_date, read_538_archive


def csv_fixture(*changes):
    row = {"poll_id": "p1", "question_id": "q1", "race_id": "r1", "cycle": "2022",
           "location": "PA", "type_simple": "Sen-G", "polldate": "2022-10-24",
           "electiondate": "2022-11-08", "pollster": "Synthetic pollster", "methodology": "Online Panel",
           "partisan": "NA", "samplesize": "442.5", "cand1_party": "DEM", "cand2_party": "REP",
           "cand1_pct": "48", "cand2_pct": "45", "margin_poll": "3", "margin_actual": "2.4"}
    rows = [row | change for change in changes] if changes else [row]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(row))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def parse(raw):
    return read_538_archive(raw, retrieved_at=datetime(2026, 11, 1, tzinfo=timezone.utc))


def test_preserves_source_and_archive_date_semantics():
    archive = parse(csv_fixture())
    assert archive.selected_rows == 1
    assert archive.polls[0].publication_date is None
    assert archive.polls[0].median_field_date == date(2022, 10, 24)
    assert archive.polls[0].sample_size == 442.5
    assert archive.polls[0].race_id == "538-2022-r1"
    assert len(archive.source_sha256) == 64
    assert any("not a vintage" in limitation for limitation in archive.limitations)


def test_democrat_minus_republican_sign_when_republican_listed_first():
    archive = parse(csv_fixture({"cand1_party": "REP", "cand2_party": "DEM",
                                 "cand1_pct": "45", "cand2_pct": "48", "margin_poll": "-3",
                                 "margin_actual": "-2.4"}))
    assert archive.polls[0].dem_pct == 48
    assert archive.polls[0].rep_pct == 45
    assert archive.polls[0].actual_margin == 2.4


def test_filters_presidential_cycles_offdate_and_non_dr_rows_with_counts():
    archive = parse(csv_fixture(
        {}, {"poll_id": "p2", "cycle": "2020"},
        {"poll_id": "p3", "electiondate": "2022-12-06"},
        {"poll_id": "p4", "cand2_party": "IND"},
        {"poll_id": "p5", "type_simple": "Pres-G"},
        {"poll_id": "p6", "polldate": "2022-11-09"}))
    assert archive.selected_rows == 1
    assert archive.excluded_counts == {"not_midterm_cycle": 1, "not_november_general_date": 1,
                                       "not_democrat_republican_top_two": 1, "other_race_type": 1,
                                       "field_date_after_election": 1}


def test_separate_source_races_at_same_place_date():
    archive = parse(csv_fixture({}, {"race_id": "r2", "poll_id": "p2", "margin_actual": "8"}))
    assert archive.selected_races == 2


def test_bad_margin_and_nonfinite_values_fail_closed():
    for change in ({"margin_poll": "4"}, {"cand1_pct": "nan"}, {"margin_actual": "inf"}):
        with pytest.raises(ValueError, match="Invalid historical CSV row"):
            parse(csv_fixture(change))


def test_duplicate_rows_deduplicate_but_conflicting_identity_rejected():
    archive = parse(csv_fixture({}, {}))
    assert archive.selected_rows == 1
    assert archive.excluded_counts["exact_duplicate"] == 1
    with pytest.raises(ValueError, match="Duplicate poll/question identity"):
        parse(csv_fixture({}, {"cand1_pct": "49", "margin_poll": "4"}))


def test_observation_without_actual_is_not_evaluated():
    assert parse(csv_fixture({"margin_actual": "NA"})).selected_rows == 0


def test_aggregate_uses_declared_availability_proxy_and_fixed_sample():
    archive = parse(csv_fixture({}, {"poll_id": "p2", "question_id": "q2", "polldate": "2022-11-01"}))
    history, metadata = aggregate_history(archive)
    assert len(history) == 1
    assert history[0].as_of == date(2022, 11, 6)
    assert history[0].poll_count == 1  # Nov1 median + seven-day lag misses Nov6 cutoff.
    assert history[0].outcome_available_date == date(2022, 12, 8)
    assert metadata["normalization"]["sample_size"] == 1000
    assert metadata["normalization"]["publication_lag_days"] == 7
    assert metadata["schema_version"] == "2.0"
    assert metadata["normalization"]["poll_margin_decimal_places"] == 12


def test_normalization_twice_produces_identical_immutable_artifact_bytes(tmp_path):
    script = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/election_import_history.py"))
    raw = csv_fixture({}, {"poll_id": "p2", "question_id": "q2", "polldate": "2022-10-20",
                           "cand1_pct": "47", "margin_poll": "2", "pollster": "Second synthetic pollster"})
    snapshots = []
    for _ in range(2):
        history, metadata = aggregate_history(parse(raw))
        snapshots.append(json.dumps({"metadata": metadata,
                                     "races": [row.model_dump(mode="json") for row in history]}, sort_keys=True))
    assert snapshots[0] == snapshots[1]
    assert history[0].poll_margin == round(history[0].poll_margin, 12)
    output = tmp_path / "history-v2.json"
    script["write_immutable"](output, snapshots[0])
    original = output.read_bytes()
    script["write_immutable"](output, snapshots[1])
    assert output.read_bytes() == original


def test_general_election_date_uses_tuesday_after_first_monday():
    assert november_general_date(2022) == date(2022, 11, 8)
    assert november_general_date(2026) == date(2026, 11, 3)
    assert november_general_date(2016) == date(2016, 11, 8)


def test_import_script_reuses_pinned_retrieval_and_rejects_changed_evidence(tmp_path):
    script = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/election_import_history.py"))
    source = tmp_path / "source.csv"
    raw = csv_fixture()
    source.write_bytes(raw)
    (tmp_path / "manifest.json").write_text(json.dumps({"records": [{
        "path": "source.csv", "sha256": hashlib.sha256(raw).hexdigest(),
        "retrieved_at": "2026-11-01T20:00:00+00:00"}]}))
    pinned = script["retrieval_time"](source, raw, None)
    assert pinned == datetime(2026, 11, 1, 20, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="no longer match"):
        script["retrieval_time"](source, raw + b" ", None)
    output = tmp_path / "normalized.json"
    script["write_immutable"](output, "same\n")
    script["write_immutable"](output, "same\n")
    with pytest.raises(ValueError, match="Refusing to overwrite"):
        script["write_immutable"](output, "changed\n")
    assert output.read_text() == "same\n"
