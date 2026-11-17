"""Election data pipeline contracts on a synthetic offline universe (plus one real CC0 excerpt)."""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing

import pytest

from election_data.backup import create_backup, restore_backup, verify_backup
from election_data.contracts import ContestsRequest, IssuesRequest, MapRequest, ProvenanceRequest, ResultsRequest
from election_data.core import ElectionDataError, ReviewRequired, connect, writer_lock
from election_data.query import ElectionDataReader, NotFound
from election_data.release import activate_release, build_release, diff_releases, publish_release
from election_data_support import build_universe, load_universe, mit_tsv, write_sources


@pytest.fixture(scope="module")
def published(tmp_path_factory):
    base = tmp_path_factory.mktemp("election-data")
    sources = write_sources(base / "sources")
    root, imports = load_universe(base / "root", sources)
    with closing(connect(root.database)) as connection, writer_lock(root):
        built = build_release(root, connection)
        publish_release(root, connection, built["release_id"])
    return root, imports, built, sources


def _reader(root):
    return ElectionDataReader(root.path)


def _contest(reader, postal, index=0):
    return reader.contests(ContestsRequest(state=postal)).contests[index]


def _units(reader, contest_id, level="county"):
    units, cursor = [], 0
    while cursor is not None:
        page = reader.results(ResultsRequest(contest_id=contest_id, level=level, cursor=cursor, limit=15))
        units += page.units
        cursor = page.next_cursor
    return units


def _issues(reader, release_id):
    issues, cursor = [], 0
    while cursor is not None:
        page = reader.issues(IssuesRequest(release_id=release_id, cursor=cursor, limit=15))
        issues += page.issues
        cursor = page.next_cursor
    return issues


def test_imports_account_for_every_source_row(published):
    _, imports, _, _ = published
    mit = imports["mit-county-president"]["accounting"]
    assert mit["rows_read"] == mit["vote_rows"] + mit["metric_rows"] + mit["rows_other_office"]
    assert mit["cycles"] == 1 and mit["metric_rows"] == 1 and mit["missing_counts"] == 1
    # The real Alamance excerpt has three candidates with four unlabeled rows each.
    assert mit["repeated_grain_rows"] == 9
    assert imports["fec-president-2024"]["accounting"]["state_rows"] == 51
    assert imports["census-county-2020"]["accounting"]["features_rejected"] == 0


def test_release_is_staged_without_blocking_errors_and_published(published):
    root, _, built, _ = published
    assert built["status"] == "staged" and built["blocking_errors"] == 0
    status = _reader(root).status()
    assert status.active_release_id == built["release_id"]
    assert {(cell.geography_level, cell.coverage_status) for cell in status.coverage} >= {
        ("state", "complete"), ("county", "complete"), ("county", "partial"), ("district", "unavailable")}


def test_state_results_use_official_totals_with_fraction_shares_and_point_margins(published):
    reader = _reader(published[0])
    contest = _contest(reader, "GA")
    unit = reader.results(ResultsRequest(contest_id=contest.contest_id, level="state")).units[0]
    assert unit.source_status == "official_compilation_of_certified" and unit.result_status == "complete"
    total = unit.denominator["value"]
    assert total == sum(result.votes for result in unit.results)
    top, second = unit.results[0], unit.results[1]
    assert 0 < top.share < 1 and top.share == round(top.votes / total, 6)
    assert unit.margin["kind"] == "top_two_percentage_points"
    assert unit.margin["value"] == pytest.approx((top.share - second.share) * 100, abs=1e-3)


def test_real_nc_batches_are_summed_only_after_matching_totalvotes(published):
    reader = _reader(published[0])
    units = {unit.label: unit for unit in _units(reader, _contest(reader, "NC").contest_id)}
    alamance = {result.display_name: result.votes for result in units["Alamance"].results}
    assert alamance == {"Kamala D. Harris": 40624, "Donald J. Trump": 47937, "Chase Oliver": 305,
                        "Other (MIT aggregate)": 965}
    assert any("summed" in note for note in units["Alamance"].notes)
    provenance = reader.provenance(ProvenanceRequest(derivation_id=units["Alamance"].provenance_ids[0], limit=15))
    assert provenance.total == 14  # 13 vote rows + the totalvotes check
    assert {item.batch_key for item in provenance.inputs if item.input_kind == "vote_observation"} >= {"b1", "b2"}
    assert all(item.locator.get("line") for item in provenance.inputs)
    assert provenance.parameters["decisions"] == ["dec-fed-county-total-selection"]


def test_missing_counts_are_not_zero_and_withhold_shares(published):
    reader = _reader(published[0])
    units = _units(reader, _contest(reader, "NM").contest_id)
    partial = next(unit for unit in units if unit.result_status == "partial")
    oliver = next(result for result in partial.results if result.display_name == "Chase Oliver")
    assert oliver.votes is None and oliver.result_status == "missing" and oliver.share is None
    assert partial.denominator is None and partial.margin is None


def test_modes_without_total_or_decision_are_withheld_without_fabricated_rows(published):
    reader = _reader(published[0])
    contest = _contest(reader, "OR")
    withheld = next(unit for unit in _units(reader, contest.contest_id) if unit.result_status == "unavailable")
    assert withheld.results == [] and any("no all-mode total" in note for note in withheld.notes)
    county = next(cell for cell in contest.coverage if cell.geography_level == "county")
    assert county.coverage_status == "partial"


def test_kansas_city_merge_and_fips_crosswalk_join_county_polygons(published):
    reader = _reader(published[0])
    jackson = next(unit for unit in _units(reader, _contest(reader, "MO").contest_id) if "Jackson" in unit.label)
    assert jackson.boundary_status == "matched" and jackson.boundary_feature_id
    harris = next(result.votes for result in jackson.results if result.display_name == "Kamala D. Harris")
    assert harris == (1000 + 37 * (2 + 25)) + (1000 + 37 * 9)  # Jackson authority + Kansas City
    sd = _units(reader, _contest(reader, "SD").contest_id)
    oglala = next(unit for unit in sd if unit.label == "Oglala Lakota")
    assert oglala.boundary_status == "matched" and any("46113" in note for note in oglala.notes)
    county = next(cell for cell in _contest(reader, "SD").coverage if cell.geography_level == "county")
    assert county.coverage_status == "complete" and county.missing_units == []


def test_alaska_districts_are_tabular_and_map_shows_no_data(published):
    reader = _reader(published[0])
    contest = _contest(reader, "AK")
    units = _units(reader, contest.contest_id)
    assert {unit.unit_kind for unit in units} == {"state_legislative_district", "nongeographic"}
    assert all(unit.boundary_status == "boundary_unavailable" for unit in units)
    assert all(unit.round == "round_1" for unit in units)
    view = reader.map(MapRequest(contest_id=contest.contest_id))
    assert {feature.result_status for feature in view.features} == {"no_data"}
    assert len(view.tabular_only_units) == 3 and view.asset.join_property == "feature_id"


def test_partition_differences_are_disclosed_not_hidden(published):
    root, _, built, _ = published
    reader = _reader(root)
    pa = next(cell for cell in _contest(reader, "PA").coverage if cell.geography_level == "county")
    assert pa.coverage_status == "partial" and "differs" in pa.notes
    report = json.loads((root.path / "reports" / built["release_id"] / "reconciliation.json").read_text())
    harris = next(value for key, value in report["states"]["PA"]["candidacies"].items() if value["difference"])
    assert harris["difference"] == -100
    assert report["states"]["MO"]["status"] == "reconciled" and report["states"]["NC"]["status"] == "reconciled"


def test_district_electors_and_rcv_outcomes_are_sourced_separately(published):
    root, _, built, _ = published
    reader = _reader(root)
    maine = {contest.seat_key: contest for contest in reader.contests(ContestsRequest(state="ME")).contests}
    assert set(maine) == {"president:ME:at-large", "president:ME:cd1", "president:ME:cd2"}
    cd2 = maine["president:ME:cd2"]
    assert [(o.outcome_type, o.display_name) for o in cd2.outcomes] == [("won_electors", "Donald J. Trump")]
    assert cd2.coverage[0].coverage_status == "unavailable"
    at_large = maine["president:ME:at-large"]
    assert at_large.voting_system == "ranked_choice"
    assert at_large.electoral_votes == {"president:Kamala D. Harris": 3, "president:Donald J. Trump": 1,
                                        "vice_president:Tim Walz": 3, "vice_president:JD Vance": 1}
    rules = {issue.rule_id for issue in _issues(reader, built["release_id"]) if issue.severity == "info"}
    assert {"rcv_first_round_majority", "electoral_components_match", "unit_merge_requires_reconciliation"} <= rules


def test_unknown_identifiers_are_not_found_and_known_missing_unit_is_explicit(published):
    reader = _reader(published[0])
    with pytest.raises(NotFound):
        reader.results(ResultsRequest(contest_id="contest_" + "0" * 24))
    with pytest.raises(NotFound):
        reader.results(ResultsRequest(contest_id=_contest(reader, "OR").contest_id, unit_id="unit_" + "0" * 24))
    with pytest.raises(NotFound):
        reader.contests(ContestsRequest(release_id="release_" + "0" * 20))


def test_reimport_and_rebuild_are_idempotent(published, tmp_path):
    root, _, built, sources = published
    copy = tmp_path / "copy"
    shutil.copytree(root.path, copy)
    from election_data.core import DataRoot
    clone = DataRoot.open(copy)
    with closing(connect(clone.database)) as connection:
        before = [connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("vote_observation", "metric_observation", "source_artifact", "published_result")]
    _, again = load_universe(copy, sources)
    assert {result["status"] for result in again.values()} == {"already_imported"}
    with closing(connect(clone.database)) as connection, writer_lock(clone):
        rebuilt = build_release(clone, connection)
        after = [connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                 for table in ("vote_observation", "metric_observation", "source_artifact", "published_result")]
    assert rebuilt["release_id"] == built["release_id"] and rebuilt["build"] == "unchanged_inputs"
    assert before == after


def test_source_correction_creates_new_release_and_rollback_restores_previous(published, tmp_path):
    root, _, built, sources = published
    copy = tmp_path / "corrected"
    shutil.copytree(root.path, copy)
    universe = build_universe()
    for row in universe.mit_rows:  # A correction that reduces previously reported votes.
        if row[3] == "GA" and row[6] == "DONALD J TRUMP":
            row[8] = str(int(row[8]) - 50)
            row[9] = str(int(row[9]) - 50)
            break
    corrected = tmp_path / "mit-corrected.tab"
    corrected.write_bytes(mit_tsv(universe))
    from election_data.acquire import add_manual_artifact
    from election_data.core import DataRoot
    from election_data.importer import run_adapter

    clone = DataRoot.open(copy)
    with closing(connect(clone.database)) as connection, writer_lock(clone):
        artifact = add_manual_artifact(clone, connection, "mit-county-president", "countypres_2000-2024.tab", corrected)
        assert artifact["status"] == "new_artifact"
        assert run_adapter(clone, connection, "mit-county-president", artifact["artifact_id"])["status"] == "completed"
        second = build_release(clone, connection)
        assert second["release_id"] != built["release_id"] and second["parent_release_id"] == built["release_id"]
        publish_release(clone, connection, second["release_id"])
        changes = diff_releases(connection, built["release_id"], second["release_id"])
        reduced = [change for change in changes["results"] if change["before"] and change["after"]]
        assert len(reduced) == 1 and reduced[0]["before"] - reduced[0]["after"] == 50
        assert any(change["after"] == "partial" for change in changes["coverage"])  # GA no longer reconciles
        activate_release(clone, connection, built["release_id"])
        active = connection.execute("SELECT release_id FROM active_release").fetchone()[0]
        old_runs = connection.execute("SELECT count(*) FROM import_run WHERE adapter_id='mit-county-president'").fetchone()[0]
    assert active == built["release_id"] and old_runs == 2
    reader = ElectionDataReader(copy)
    pinned = reader.results(ResultsRequest(contest_id=_contest(reader, "GA").contest_id, level="county",
                                           release_id=second["release_id"]))
    assert pinned.release_id == second["release_id"]


def test_layout_change_fails_closed_without_facts(published, tmp_path):
    _, _, _, sources = published
    broken = tmp_path / "sources"
    shutil.copytree(sources["mit-county-president"].parent, broken)
    path = broken / "mit.tab"
    text = path.read_text(encoding="utf-8").replace("candidatevotes", "votes", 1)
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ReviewRequired, match="layout changed"):
        load_universe(tmp_path / "root", {**sources, "mit-county-president": path})
    with closing(sqlite3.connect(tmp_path / "root" / "db" / "elections.sqlite")) as connection:
        status = connection.execute("SELECT status FROM import_run WHERE adapter_id='mit-county-president'").fetchone()[0]
        mit_votes = connection.execute(
            "SELECT count(*) FROM vote_observation v JOIN source_result_set s USING (set_id) "
            "WHERE s.source_id='mit-county-president'").fetchone()[0]
    assert status == "failed" and mit_votes == 0


def test_negative_counts_require_review_and_unlisted_labels_stay_source_names(published, tmp_path):
    import re
    _, _, _, sources = published
    directory = tmp_path / "negative"
    shutil.copytree(sources["mit-county-president"].parent, directory)
    path = directory / "mit.tab"
    text, count = re.subn(r'("KAMALA D HARRIS"\t"DEMOCRAT"\t)\d+', r"\g<1>-5", path.read_text(encoding="utf-8"), count=1)
    assert count == 1
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ReviewRequired, match="nonnegative integer"):
        load_universe(tmp_path / "root-negative", {**sources, "mit-county-president": path})
    # A label outside the reviewed classes is kept exactly as a candidate name, never merged or dropped.
    directory = tmp_path / "label"
    shutil.copytree(sources["mit-county-president"].parent, directory)
    path = directory / "mit.tab"
    text, count = re.subn(r'"OTHER"\t"OTHER"', '"WRITE IN PERSON"\t"OTHER"', path.read_text(encoding="utf-8"), count=1)
    assert count == 1
    path.write_text(text, encoding="utf-8")
    root, _ = load_universe(tmp_path / "root-label", {**sources, "mit-county-president": path})
    with closing(connect(root.database)) as connection:
        kinds = connection.execute("SELECT candidacy_kind, status FROM candidacy WHERE display_name='Write In Person'").fetchall()
    assert [tuple(row) for row in kinds] == [("candidate", "source_label_unresolved_person")]


def test_publication_requires_verified_assets_and_keeps_previous_release_active(published, tmp_path):
    root, _, built, sources = published
    copy = tmp_path / "crash"
    shutil.copytree(root.path, copy)
    from election_data.core import DataRoot
    clone = DataRoot.open(copy)
    universe = build_universe()
    universe.fec_adjust[("GA", "KAMALA D HARRIS")] = 7
    alternate = tmp_path / "fec.xlsx"
    from election_data_support import fec_workbook
    alternate.write_bytes(fec_workbook(universe))
    from election_data.acquire import add_manual_artifact
    from election_data.importer import run_adapter
    with closing(connect(clone.database)) as connection, writer_lock(clone):
        artifact = add_manual_artifact(clone, connection, "fec-federal-elections-2024-president",
                                       "2024presgeresults.xlsx", alternate)
        run_adapter(clone, connection, "fec-president-2024", artifact["artifact_id"])
        candidate = build_release(clone, connection)
        asset = connection.execute("SELECT path FROM release_asset WHERE release_id=? LIMIT 1",
                                   (candidate["release_id"],)).fetchone()[0]
        (copy / asset).write_bytes(b"{}")  # Simulated corruption before the pointer switch.
        with pytest.raises(ElectionDataError, match="assets failed verification"):
            publish_release(clone, connection, candidate["release_id"])
        active = connection.execute("SELECT release_id FROM active_release").fetchone()[0]
        status = connection.execute("SELECT status FROM data_release WHERE release_id=?",
                                    (candidate["release_id"],)).fetchone()[0]
    assert active == built["release_id"] and status == "staged"


def test_curation_decisions_are_append_only(published, monkeypatch):
    root, _, _, _ = published
    from election_data import pipeline
    original, digest = pipeline.load_curation()
    edited = json.loads(json.dumps(original))
    edited["decisions"][0]["rationale"] += " (silently edited)"
    with closing(connect(root.database)) as connection:
        with pytest.raises(ReviewRequired, match="append-only"):
            pipeline.ensure_reference(connection, edited)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE curation_decision SET rationale='x'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE source_artifact SET byte_length=0")


def test_backup_verify_and_restore_round_trip(published, tmp_path):
    root, _, built, _ = published
    with writer_lock(root):
        summary = create_backup(root, tmp_path / "backup")
    assert summary["active_release_id"] == built["release_id"]
    manifest = json.loads((tmp_path / "backup" / "backup-manifest.json").read_text())
    assert not any("local.toml" in item["path"] for item in manifest["files"])
    assert verify_backup(tmp_path / "backup")["verified"] is True
    restored = restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert restored["active_release_id"] == built["release_id"] and restored["sample_query"]
    (tmp_path / "backup" / manifest["files"][0]["path"]).write_bytes(b"tampered")
    with pytest.raises(ElectionDataError, match="corrupted"):
        verify_backup(tmp_path / "backup")
    with pytest.raises(Exception, match="empty"):
        restore_backup(tmp_path / "backup", tmp_path / "restored")


def test_cli_uses_documented_exit_codes_and_json(published, tmp_path):
    import os
    import subprocess
    import sys

    root = published[0]
    environment = {**os.environ, "PYTHONPATH": str(root.path.parents[0])}
    environment.pop("ELECTION_DATA_DIR", None)

    def cli(*arguments):
        return subprocess.run([sys.executable, "-m", "election_data", "--json", *arguments], capture_output=True,
                              text=True, timeout=60)

    ok = cli("--data-dir", str(root.path), "results", "--state", "GA")
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["units"][0]["source_status"] == "official_compilation_of_certified"
    missing = cli("--data-dir", str(tmp_path / "absent"), "coverage")
    assert missing.returncode == 2 and json.loads(missing.stderr)["error"]["code"] == "INVALID_INPUT"
    unknown = cli("--data-dir", str(root.path), "import", "--adapter", "made-up")
    assert unknown.returncode == 2
    doctor = cli("--data-dir", str(root.path), "doctor")
    assert doctor.returncode == 0 and json.loads(doctor.stdout)["ok"] is True
