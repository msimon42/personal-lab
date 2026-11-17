"""Federal baseline contracts (President/Senate/House state-level, districts, electoral history).

Everything here is synthetic: invented counts in the real source layouts, including the
House file's wrapped encoding. Synthetic fixtures establish synthetic contracts only.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from importlib import resources

import pytest

from election_data.contracts import (CompareRequest, ContestsRequest, CoverageRequest, MapRequest,
                                     PresidentSummaryRequest, ProvenanceRequest, ResultsRequest)
from election_data.core import DataRoot, ReviewRequired, connect, migrate, writer_lock
from election_data.federal import (classify_label, congress_for_election, expected_contests, general_election_date,
                                   house_seats, match_ticket, normalize_name, senate_seat)
from election_data.query import ElectionDataReader, IncompatibleGeography, NotFound
from election_data.release import build_release, publish_release
from election_data_support import (build_universe, census_cd_zip, house_wrapped, load_universe,
                                   nara_modern_page, president_csv, senate_tsv, ucla_zip, write_sources)


def _county(universe, year, postal, name, fips, votes, *, mode="TOTAL", total=None):
    labels = {2016: ("HILLARY CLINTON", "DONALD TRUMP"), 2020: ("JOSEPH R BIDEN JR", "DONALD J TRUMP")}[year]
    state = {"GA": "GEORGIA"}[postal]
    for candidate, party, value in zip(labels, ("DEMOCRAT", "REPUBLICAN"), votes):
        universe.mit_rows.append([state, name, str(year), postal, str(int(fips)), "US PRESIDENT", candidate, party,
                                  str(value), str(total if total is not None else sum(votes)), "20220315", mode])


@pytest.fixture(scope="module")
def federal(tmp_path_factory):
    base = tmp_path_factory.mktemp("federal")
    universe = build_universe()
    _county(universe, 2020, "GA", "FIRST GA", "13001", (30, 20), mode="ELECTION DAY", total=90)
    _county(universe, 2020, "GA", "FIRST GA", "13001", (25, 15), mode="ABSENTEE", total=90)
    _county(universe, 2020, "GA", "SECOND GA", "13003", (45, 55))
    _county(universe, 2020, "GA", "GHOST", "13999", (0, 0))
    _county(universe, 2016, "GA", "FIRST GA", "13001", (40, 60))
    _county(universe, 2016, "GA", "SECOND GA", "13003", (50, 50))
    sources = write_sources(base / "sources", universe)
    extra = base / "sources"
    files = {
        "mit-state-president": ("president.csv", president_csv([
            (2020, "GA", "BIDEN, JOSEPH R. JR", "DEMOCRAT", "False", 100, 193),
            (2020, "GA", "TRUMP, DONALD J.", "REPUBLICAN", "False", 90, 193),
            (2020, "GA", "", "", "True", 3, 193),
            (2020, "NY", "BIDEN, JOSEPH R. JR", "DEMOCRAT", "False", 200, 405),
            (2020, "NY", "BIDEN, JOSEPH R. JR", "WORKING FAMILIES", "False", 30, 405),
            (2020, "NY", "TRUMP, DONALD J.", "REPUBLICAN", "False", 150, 405),
            (2020, "NY", "TRUMP, DONALD J.", "CONSERVATIVE", "False", 20, 405),
            (2020, "NY", "BLANK VOTE", "", "False", 5, 405),
            (2020, "ME", "BIDEN, JOSEPH R. JR", "DEMOCRAT", "False", 60, 100),
            (2020, "ME", "TRUMP, DONALD J.", "REPUBLICAN", "False", 40, 100),
            (2016, "GA", "CLINTON, HILLARY", "DEMOCRAT", "False", 90, 200),
            (2016, "GA", "TRUMP, DONALD J.", "REPUBLICAN", "False", 110, 200),
        ])),
        "mit-senate": ("senate.tab", senate_tsv([
            (2020, "GA", "gen", "False", "DAVID A. PERDUE", "REPUBLICAN", "49.0", "100.0"),
            (2020, "GA", "gen", "False", "JON OSSOFF", "DEMOCRAT", "48.0", "100.0"),
            (2020, "GA", "gen", "False", "SHANE HAZEL", "LIBERTARIAN", "3.0", "100.0"),
            (2021, "GA", "runoff", "False", "JON OSSOFF", "DEMOCRAT", "51.0", "100.0"),
            (2021, "GA", "runoff", "False", "DAVID A. PERDUE", "REPUBLICAN", "49.0", "100.0"),
            (2020, "GA", "gen", "True", "RAPHAEL WARNOCK", "DEMOCRAT", "33.0", "100.0"),
            (2020, "GA", "gen", "True", "KELLY LOEFFLER", "REPUBLICAN", "26.0", "100.0"),
            (2020, "GA", "gen", "True", "OTHER", "", "41.0", "100.0"),
            (2021, "GA", "runoff", "True", "RAPHAEL WARNOCK", "DEMOCRAT", "51.0", "100.0"),
            (2021, "GA", "runoff", "True", "KELLY LOEFFLER", "REPUBLICAN", "49.0", "100.0"),
            (2022, "CA", "gen", "False", "ALEX PADILLA", "DEMOCRAT", "61.0", "100.0"),
            (2022, "CA", "gen", "False", "MARK MEUSER", "REPUBLICAN", "39.0", "100.0"),
            (2022, "CA", "gen", "True", "ALEX PADILLA", "DEMOCRAT", "60.0", "100.0"),
            (2022, "CA", "gen", "True", "MARK MEUSER", "REPUBLICAN", "40.0", "100.0"),
            (2020, "ME", "gen", "False", "SUSAN MARGARET COLLINS", "REPUBLICAN", "51.0", "100.0"),
            (2020, "ME", "gen", "False", "SARA I. GIDEON", "DEMOCRAT", "42.0", "100.0"),
            (2020, "ME", "gen", "False", "", "", "7.0", "100.0"),
            (2010, "MA", "gen", "True", "SCOTT P. BROWN", "REPUBLICAN", "52.0", "100.0", "True"),
            (2010, "MA", "gen", "True", "MARTHA COAKLEY", "DEMOCRAT", "48.0", "100.0", "True"),
        ])),
        "mit-house": ("house.tab", house_wrapped([
            (2022, "NY", 1, "GEN", "FALSE", "FALSE", "NICK LALOTA", "REPUBLICAN", 100, 224, "TRUE"),
            (2022, "NY", 1, "GEN", "FALSE", "FALSE", "NICK LALOTA", "CONSERVATIVE", 20, 224, "TRUE"),
            (2022, "NY", 1, "GEN", "FALSE", "FALSE", "BRIDGET FLEMING", "DEMOCRAT", 90, 224, "TRUE"),
            (2022, "NY", 1, "GEN", "FALSE", "FALSE", "BRIDGET FLEMING", "WORKING FAMILIES", 10, 224, "TRUE"),
            (2022, "NY", 1, "GEN", "FALSE", "FALSE", "WRITEIN", "NA", 2, 224, "FALSE"),
            (2022, "NY", 1, "GEN", "FALSE", "FALSE", "WRITEIN", "NA", 2, 224, "FALSE"),
            (2022, "AK", 0, "GEN", "FALSE", "FALSE", "MARY PELTOLA", "DEMOCRAT", 55, 100, "FALSE"),
            (2022, "AK", 0, "GEN", "FALSE", "FALSE", "SARAH PALIN", "REPUBLICAN", 30, 100, "FALSE"),
            (2022, "AK", 0, "GEN", "FALSE", "FALSE", "NICHOLAS BEGICH", "REPUBLICAN", 15, 100, "FALSE"),
            (2022, "AL", 2, "GEN", "FALSE", "FALSE", 'WILLIAM L "BILL" DICKINSON', "REPUBLICAN", 70, 100, "FALSE"),
            (2022, "AL", 2, "GEN", "FALSE", "FALSE", "CHESTER TODD, JR.", "DEMOCRAT", 30, 100, "FALSE"),
            (2022, "LA", 5, "GEN", "FALSE", "FALSE", "ALPHA ONE", "REPUBLICAN", 40, 100, "FALSE"),
            (2022, "LA", 5, "GEN", "FALSE", "FALSE", "BETA TWO", "DEMOCRAT", 35, 100, "FALSE"),
            (2022, "LA", 5, "GEN", "FALSE", "FALSE", "GAMMA THREE", "REPUBLICAN", 25, 100, "FALSE"),
            (2022, "LA", 5, "GEN", "TRUE", "FALSE", "ALPHA ONE", "REPUBLICAN", 55, 100, "FALSE"),
            (2022, "LA", 5, "GEN", "TRUE", "FALSE", "BETA TWO", "DEMOCRAT", 45, 100, "FALSE"),
            (1996, "TX", 3, "PRI", "FALSE", "TRUE", "SAM X", "REPUBLICAN", 60, 100, "FALSE"),
            (1996, "TX", 3, "PRI", "FALSE", "TRUE", "LEE Y", "DEMOCRAT", 40, 100, "FALSE"),
            (2020, "DC", 0, "GEN", "FALSE", "FALSE", "ELEANOR HOLMES NORTON", "DEMOCRAT", 80, 80, "FALSE"),
            (2020, "FL", 25, "GEN", "FALSE", "FALSE", "MARIO DIAZ-BALART", "REPUBLICAN", -1, -1, "FALSE"),
            (2020, "NC", 1, "GEN", "FALSE", "FALSE", "G K BUTTERFIELD", "DEMOCRAT", 54, 100, "FALSE"),
            (2020, "NC", 1, "GEN", "FALSE", "FALSE", "SANDY SMITH", "REPUBLICAN", 46, 100, "FALSE"),
            (2024, "AK", 0, "GEN", "FALSE", "FALSE", "NICHOLAS BEGICH", "REPUBLICAN", 51, 100, "FALSE"),
            (2024, "AK", 0, "GEN", "FALSE", "FALSE", "MARY PELTOLA", "DEMOCRAT", 49, 100, "FALSE"),
        ])),
        "nara-electoral-2020": ("nara-2020.html", nara_modern_page(
            2020, [("Joseph R. Biden Jr., of Delaware", "p"), ("Donald J. Trump, of Florida", "p"),
                   ("Kamala D. Harris, of California", "v"), ("Michael R. Pence, of Indiana", "v")],
            [("Georgia", 16, [16, 0, 16, 0]), ("New York", 29, [29, 0, 29, 0]), ("Maine", 4, [3, 1, 3, 1])],
            footnote=True)),
        "nara-electoral-1789": ("nara-1789.html", (
            "<table><tr><th>Name of candidate</th><th>GA</th><th>MD</th><th>Total</th></tr>"
            "<tr><td>George Washington, Esq</td><td>5</td><td>6</td><td>11</td></tr>"
            "<tr><td>John Milton, Esq</td><td>5</td><td>-</td><td>5</td></tr>"
            "<tr><td>Robert H. Harrison, Esq</td><td>-</td><td>6</td><td>6</td></tr></table>").encode()),
        "districts-congress-118": ("cd118.zip", census_cd_zip(118, "cb_2022_us_cd118_20m",
                                                              [("NY", "01"), ("AK", "00"), ("AL", "02"), ("LA", "05")])),
        "districts-congress-116": ("cd116.zip", census_cd_zip(116, "cb_2018_us_cd116_20m",
                                                              [("FL", "25"), ("NC", "01"), ("DC", "98")])),
        "districts-congress-105": ("districts105.zip", ucla_zip(105, [("Texas", 3, 105, 107), ("Texas", 3, 103, 105)])),
    }
    for name, (filename, payload) in files.items():
        (extra / filename).write_bytes(payload)
        sources[name] = extra / filename
    root, imports = load_universe(base / "root", sources)
    with closing(connect(root.database)) as connection, writer_lock(root):
        built = build_release(root, connection)
        publish_release(root, connection, built["release_id"])
    return root, imports, built, sources


def _one(reader, **filters):
    contests = reader.contests(ContestsRequest(limit=15, **filters)).contests
    return {(contest.seat_key, contest.stage, contest.contest_type): contest for contest in contests}


def _unit(reader, contest, level=None):
    return reader.results(ResultsRequest(contest_id=contest.contest_id, level=level)).units[0]


def _votes(unit):
    return {result.display_name: result.votes for result in unit.results}


def test_reference_rules_are_deterministic():
    assert general_election_date(2024).isoformat() == "2024-11-05" and general_election_date(2000).isoformat() == "2000-11-07"
    assert congress_for_election(2024) == 119 and congress_for_election(1976) == 95
    assert house_seats("CA", 2020) == 53 and house_seats("CA", 2022) == 52 and house_seats("AK", 2022) == 1
    assert len(expected_contests("house", 2022)) == 435 and len(expected_contests("senate", 2022)) == 34
    assert len(expected_contests("president", 2024)) == 56 and expected_contests("president", 2022) == []
    assert senate_seat("GA", 2020, False) == "senate:GA:c2" and senate_seat("GA", 2020, True) == "senate:GA:c3"
    assert senate_seat("CA", 2022, True) == "senate:CA:c3"  # same seat as that day's regular election
    assert senate_seat("MA", 2010, True) == "senate:MA:class-unresolved"  # not in the reviewed table: never guessed
    assert normalize_name("BUSH, GEORGE W.") == normalize_name("George W Bush")
    assert match_ticket(2000, "Albert Gore, Jr., of Tennessee") == "al-gore" and match_ticket(2000, "GEORGE W. BUSH") == "george-w-bush"
    assert match_ticket(1992, "BUSH, GEORGE H.W.") == "george-h-w-bush" and match_ticket(2016, "BILL CLINTON") is None
    assert classify_label("BLANK VOTE/SCATTERING") == ("metric", "blank_and_scattering_combined")
    assert classify_label("", True) == ("aggregate", "aggregate-write-in") and classify_label("JANE DOE") == ("candidate", "")


def test_curated_reference_files_are_internally_consistent():
    reference = json.loads(resources.files("election_data.manifests").joinpath("reference-federal.json").read_text())
    for census in ("1970", "1980", "1990", "2000", "2010", "2020"):
        assert sum(seats[census] for seats in reference["house_seats_by_census"].values()) == 435
    classes = reference["senate_classes"]
    assert all(len(value) == 2 for value in classes.values())
    assert [sum(number in value for value in classes.values()) for number in (1, 2, 3)] == [33, 33, 34]
    curation = json.loads(resources.files("election_data.manifests").joinpath("curation-federal.json").read_text())
    people = {person["key"] for person in curation["persons"]}
    for year, tickets in curation["tickets"].items():
        assert int(year) % 4 == 0 and all({ticket["president"], ticket["vice_president"]} <= people for ticket in tickets)
    table = next(d for d in curation["decisions"] if d["decision_id"] == "dec-fed-senate-special-seats")["scope"]
    assert all(number in classes[key.split(":")[0]] for key, number in table["special_seat_classes"].items())


def test_wrapped_house_records_decode_exactly_and_bad_wrapping_fails_closed(federal, tmp_path):
    root, imports, _, sources = federal
    assert imports["mit-house"]["accounting"]["rows_read"] == 24
    with closing(connect(root.database)) as connection:
        labels = {row[0] for row in connection.execute("SELECT raw_candidate_label FROM vote_observation")}
    assert 'WILLIAM L "BILL" DICKINSON' in labels and "CHESTER TODD, JR." in labels
    broken = tmp_path / "house.tab"
    broken.write_text(sources["mit-house"].read_text(encoding="utf-8").replace('"2022,NEW YORK', "2022,NEW YORK", 1),
                      encoding="utf-8")
    with pytest.raises(ReviewRequired, match="not wrapped"):
        load_universe(tmp_path / "root", {key: value for key, value in sources.items()
                                          if key.startswith("census")} | {"mit-house": broken})


def test_fusion_lines_are_summed_once_and_blank_votes_are_not_candidate_votes(federal):
    reader = ElectionDataReader(federal[0].path)
    contest = _one(reader, office="president", year=2020, state="NY")[("president:NY", "general", "regular")]
    unit = _unit(reader, contest)
    assert _votes(unit) == {"Joseph R. Biden Jr.": 230, "Donald J. Trump": 170}
    assert unit.denominator["value"] == 400 and "party lines summed per candidate" in unit.notes
    trace = reader.provenance(ProvenanceRequest(derivation_id=unit.provenance_ids[0], limit=15))
    assert trace.total == 5 and trace.parameters["fusion"] == "sum_party_lines_per_candidacy"
    house = _one(reader, office="house", year=2022, state="NY")[("house:NY:01", "general", "regular")]
    district = _unit(reader, house)
    assert _votes(district) == {"Nick Lalota": 120, "Bridget Fleming": 100, "Write-ins and scattering (source aggregate)": 4}
    assert district.geography_level == "district" and district.boundary_status == "matched"
    assert {result.display_name: result.party for result in district.results}["Nick Lalota"] == "Republican Party"
    assert "repeated source rows summed" in district.notes


def test_runoffs_and_specials_are_separate_linked_contests(federal):
    reader = ElectionDataReader(federal[0].path)
    georgia = _one(reader, office="senate", year=2020, state="GA")
    assert set(georgia) == {("senate:GA:c2", "general", "regular"), ("senate:GA:c2", "runoff", "regular"),
                            ("senate:GA:c3", "general", "special"), ("senate:GA:c3", "runoff", "special")}
    general, runoff = georgia[("senate:GA:c2", "general", "regular")], georgia[("senate:GA:c2", "runoff", "regular")]
    assert runoff.predecessor_contest_id == general.contest_id and general.voting_system == "majority_runoff"
    leader = next(outcome for outcome in general.outcomes if outcome.outcome_type == "plurality_leader")
    assert leader.display_name == "David A. Perdue" and "proceeded to a runoff" in leader.notes
    assert _votes(_unit(reader, runoff)) == {"Jon Ossoff": 51, "David A. Perdue": 49}
    assert _votes(_unit(reader, general))["David A. Perdue"] == 49  # stages are never summed
    california = _one(reader, office="senate", year=2022, state="CA")
    assert set(california) == {("senate:CA:c3", "general", "regular"), ("senate:CA:c3", "general", "special")}
    massachusetts = _one(reader, office="senate", year=2010, state="MA")
    unresolved = massachusetts[("senate:MA:class-unresolved", "general", "special")]
    assert _unit(reader, unresolved).source_status == "unofficial"
    louisiana = _one(reader, office="house", year=2022, state="LA")
    assert louisiana[("house:LA:05", "runoff", "regular")].predecessor_contest_id == \
        louisiana[("house:LA:05", "general", "regular")].contest_id


def test_ranked_choice_rounds_missing_counts_and_delegates_are_explicit(federal):
    reader = ElectionDataReader(federal[0].path)
    collins = _unit(reader, _one(reader, office="senate", year=2020, state="ME")[("senate:ME:c2", "general", "regular")])
    assert collins.round == "unknown" and any("ranked-choice" in note for note in collins.notes)
    president = _unit(reader, _one(reader, office="president", year=2020, state="ME")[
        ("president:ME:at-large", "general", "regular")])
    assert president.round == "round_1"
    alaska = _one(reader, office="house", year=2022, state="AK")[("house:AK:00", "general", "regular")]
    assert alaska.voting_system == "ranked_choice" and "round is unknown" in alaska.outcomes[0].notes
    florida = _unit(reader, _one(reader, office="house", year=2020, state="FL")[("house:FL:25", "general", "regular")])
    assert florida.result_status == "partial" and florida.results[0].votes is None and florida.denominator is None
    assert florida.boundary_status == "matched"  # 2020 uses the 116th Congress file outside North Carolina
    carolina = _unit(reader, _one(reader, office="house", year=2020, state="NC")[("house:NC:01", "general", "regular")])
    assert carolina.boundary_status == "boundary_unavailable"
    delegate = _one(reader, office="house_delegate", year=2020)
    assert list(delegate) == [("delegate:DC", "general", "regular")]


def test_county_rule_sums_modes_only_when_consistent_and_labels_reference_vintage(federal):
    reader = ElectionDataReader(federal[0].path)
    contest = _one(reader, office="president", year=2020, state="GA")[("president:GA", "general", "regular")]
    units = {unit.label: unit for unit in reader.results(ResultsRequest(contest_id=contest.contest_id, level="county")).units}
    assert _votes(units["First Ga"]) == {"Joseph R. Biden Jr.": 55, "Donald J. Trump": 35}
    assert any("voting modes summed" in note for note in units["First Ga"].notes)
    assert units["First Ga"].boundary_status == "matched"
    assert units["Ghost"].boundary_status == "boundary_unavailable" and any("13999" in note for note in units["Ghost"].notes)
    county = next(cell for cell in contest.coverage if cell.geography_level == "county")
    assert county.coverage_status == "partial" and "named tickets match" in county.notes
    earlier = _one(reader, office="president", year=2016, state="GA")[("president:GA", "general", "regular")]
    old_units = reader.results(ResultsRequest(contest_id=earlier.contest_id, level="county")).units
    assert {unit.boundary_status for unit in old_units} == {"reference_vintage"}
    assert next(cell for cell in earlier.coverage if cell.geography_level == "county").coverage_status == "complete"


def test_electoral_votes_are_as_cast_and_history_has_no_invented_popular_vote(federal):
    root, imports, _, _ = federal
    reader = ElectionDataReader(root.path)
    assert imports["nara-electoral-2020"]["accounting"] == {"electors": 49, "recipients": 4, "states": 3,
                                                            "total_row_checked": 1}
    georgia = _one(reader, office="president", year=2020, state="GA")[("president:GA", "general", "regular")]
    won = next(outcome for outcome in georgia.outcomes if outcome.outcome_type == "won_electors")
    assert won.display_name == "Joseph R. Biden Jr." and georgia.electoral_votes["president:Joseph R. Biden Jr."] == 16
    maine = _one(reader, office="president", year=2020, state="ME")[("president:ME:at-large", "general", "regular")]
    split = next(outcome for outcome in maine.outcomes if outcome.outcome_type == "won_electors")
    assert "Electoral votes as cast" in split.notes and maine.electoral_votes["president:Donald J. Trump"] == 1
    first = reader.president_summary(PresidentSummaryRequest(year=1789))
    assert first.popular_vote_status == "not_researched" and first.total_votes is None and first.candidates == []
    assert {(row.name, row.electoral_votes) for row in first.electoral_votes} == {
        ("George Washington", 11), ("John Milton", 5), ("Robert H. Harrison", 6)}
    summary = reader.president_summary(PresidentSummaryRequest(year=2020))
    assert summary.states_with_totals == 3 and summary.candidates[0].votes == 390 and summary.other_votes == 3
    assert summary.candidates[0].states_led == 3
    with pytest.raises(NotFound):
        reader.president_summary(PresidentSummaryRequest(year=2021))


def test_inconsistent_nara_page_is_left_for_review(federal, tmp_path):
    _, _, _, sources = federal
    page = tmp_path / "nara-1936.html"
    page.write_bytes(nara_modern_page(1936, [("Franklin D. Roosevelt, of New York", "p"), ("John N. Garner, of Texas", "v")],
                                      [("Georgia", 12, [12, 12])], total=[12, 12, 11]))
    with pytest.raises(ReviewRequired, match="total row reports"):
        load_universe(tmp_path / "root", {key: value for key, value in sources.items() if key.startswith("census")}
                      | {"nara-electoral-1936": page})
    with closing(sqlite3.connect(tmp_path / "root" / "db" / "elections.sqlite")) as connection:
        assert connection.execute("SELECT count(*) FROM electoral_vote").fetchone()[0] == 0


def test_district_boundaries_snap_to_grid_and_prefer_the_plan_in_force(federal):
    root, imports, _, _ = federal
    ucla = imports["districts-congress-105"]["accounting"]
    assert ucla == {"features_read": 2, "features_accepted": 1, "outside_scope": 0, "rejected": 0,
                    "later_plan_same_congress": 1}
    assert imports["districts-congress-118"]["accounting"]["outside_scope"] == 1  # territory delegate district
    with closing(connect(root.database)) as connection:
        version, geometry = connection.execute(
            "SELECT gv.status, bf.geometry_json FROM boundary_feature bf JOIN geographic_version gv USING (geo_version_id) "
            "JOIN boundary_set bs USING (boundary_set_id) WHERE bs.vintage='congress-105'").fetchone()
    assert version == "congress_103_to_105"
    points = [point for ring in json.loads(geometry)["coordinates"] for point in ring]
    assert all(round(value, 2) == value for point in points for value in point) and len(points) == 6
    reader = ElectionDataReader(root.path)
    texas = _one(reader, office="house", year=1996, state="TX")[("house:TX:03", "primary", "special")]
    assert _unit(reader, texas).boundary_status == "matched"


def test_coverage_inventory_states_what_is_missing_and_why(federal):
    reader = ElectionDataReader(federal[0].path)

    def rows(**filters):
        return {(row.office, row.cycle_year, row.series): row for row in reader.coverage(
            CoverageRequest(limit=50, **filters)).rows}

    house = rows(office="house", from_year=1996, to_year=1996)[("house", 1996, "contest_totals")]
    assert house.expected == 435 and house.available == 1 and house.coverage_status == "partial"
    assert "recorded only as special elections" in house.notes and house.extra == {"special_primary": 1}
    recent = rows(office="house", from_year=2022, to_year=2022)
    assert recent[("house", 2022, "contest_totals")].available == 4 and recent[("house", 2022, "contest_totals")].missing_total == 431
    assert recent[("house", 2022, "boundaries")].coverage_status == "complete"
    assert rows(office="house", from_year=2020, to_year=2020)[("house", 2020, "boundaries")].missing == ["house:NC:01"]
    senate = rows(office="senate", from_year=1910, to_year=1914)
    assert senate[("senate", 1912, "contest_totals")].coverage_status == "not_applicable"
    assert senate[("senate", 1914, "contest_totals")].coverage_status == "not_researched"
    future = rows(from_year=2026, to_year=2026)
    assert future[("house", 2026, "contest_totals")].coverage_status == "not_researched"
    assert "still being certified" in future[("senate", 2026, "contest_totals")].notes
    president = rows(office="president", from_year=1936, to_year=1936)
    assert president[("president", 1936, "electoral_votes")].coverage_status == "not_researched"
    assert president[("president", 1936, "contest_totals")].coverage_status == "not_researched"
    modern = rows(office="president", from_year=2020, to_year=2020)
    assert modern[("president", 2020, "contest_totals")].available == 3
    assert "district elector contests" in modern[("president", 2020, "contest_totals")].notes
    assert modern[("president", 2020, "county_detail")].extra["states_where_named_tickets_match"] == 1
    gaps = reader.coverage(CoverageRequest(office="president", from_year=2020, to_year=2024, only_gaps=True, limit=50))
    assert all(row.coverage_status != "complete" for row in gaps.rows)


def test_national_maps_join_by_feature_and_keep_no_data_distinct(federal):
    reader = ElectionDataReader(federal[0].path)
    states = reader.map(MapRequest(office="president", year=2020, limit=80))
    by_status = {}
    for feature in states.features:
        by_status.setdefault(feature.result_status, []).append(feature.feature_key)
    assert states.level == "state" and sorted(by_status["complete"]) == ["13", "23", "36"]
    assert len(by_status["no_data"]) == states.total - 3 and states.asset.kind == "geojson:states"
    districts = reader.map(MapRequest(office="house", year=2022))
    assert districts.asset.kind == "geojson:districts-118" and districts.total == 4
    assert {feature.feature_key: feature.result_status for feature in districts.features} == {
        "AK:00": "complete", "AL:02": "complete", "LA:05": "complete", "NY:01": "complete"}
    assert all(feature.leader_margin_points is not None for feature in districts.features)
    carolina = reader.map(MapRequest(office="house", year=2020))
    assert carolina.asset.kind == "geojson:districts-116" and "North Carolina" in " ".join(carolina.legend_notes)
    assert {feature.feature_key: feature.result_status for feature in carolina.features}["NC:01"] == "no_data"
    with pytest.raises(IncompatibleGeography):
        reader.map(MapRequest(office="senate", year=2020, level="district"))
    with pytest.raises(NotFound):
        reader.map(MapRequest(office="senate", year=1990))


def test_comparisons_use_identical_units_only(federal):
    reader = ElectionDataReader(federal[0].path)
    state = reader.compare(CompareRequest(year_a=2016, year_b=2020, level="state"))
    assert [unit.label for unit in state.units] == ["Georgia"] and state.excluded_units == 2
    assert state.units[0].margin_a_points == -10.0 and state.units[0].swing_points == pytest.approx(15.1813, abs=1e-3)
    county = reader.compare(CompareRequest(year_a=2016, year_b=2020, level="county", state="GA"))
    assert {unit.label: unit.swing_points for unit in county.units} == {"First Ga": pytest.approx(42.2222, abs=1e-3),
                                                                         "Second Ga": -10.0}
    assert county.excluded_units == 0 and "no crosswalk" in county.method  # the zero-vote unit has no margin either year
    with pytest.raises(NotFound):
        reader.compare(CompareRequest(year_a=1980, year_b=2020))


def test_schema_migrates_from_version_one_and_keeps_published_rows(tmp_path):
    root = DataRoot.open(tmp_path / "root", create=True)
    script = resources.files("election_data.migrations").joinpath("0001_initial.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(root.database)) as connection:
        connection.executescript(script + "INSERT INTO schema_migration VALUES (1, '0001_initial.sql', '2026-11-17T00:00:00Z');")
        assert connection.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 1
    with closing(connect(root.database)) as connection:
        assert migrate(connection) == 2
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"coverage_inventory", "published_unit_status"} <= tables and "published_unit_status_v2" not in tables
        definition = connection.execute("SELECT sql FROM sqlite_master WHERE name='published_unit_status'").fetchone()[0]
        assert "reference_vintage" in definition and migrate(connection) == 2


def test_backup_verification_samples_a_contest_at_its_own_level(federal, tmp_path):
    # Regression: the first listed contest is a House district, which has no state-level result.
    from election_data.backup import create_backup, verify_backup

    root = federal[0]
    first = ElectionDataReader(root.path).contests(ContestsRequest(limit=1)).contests[0]
    assert first.office == "house"
    with writer_lock(root):
        create_backup(root, tmp_path / "backup")
    verified = verify_backup(tmp_path / "backup")
    assert verified["verified"] is True and verified["sample_query"] == "Alaska at-large"


def test_same_facts_from_different_bytes_do_not_create_a_new_release(federal, tmp_path):
    import shutil

    from election_data.acquire import add_manual_artifact
    from election_data.importer import run_adapter

    root, imports, built, sources = federal
    copy = tmp_path / "copy"
    shutil.copytree(root.path, copy)
    clone = DataRoot.open(copy)
    variant = tmp_path / "nara-2020-variant.html"
    variant.write_bytes(sources["nara-electoral-2020"].read_bytes().replace(b"<html>", b"<html><!-- served by another node -->"))
    with closing(connect(clone.database)) as connection, writer_lock(clone):
        artifact = add_manual_artifact(clone, connection, "nara-electoral-college", "electoral-college-2020.html", variant)
        assert artifact["status"] == "new_artifact"
        again = run_adapter(clone, connection, "nara-electoral-2020", artifact["artifact_id"])
        assert again["status"] == "completed" and again["output_hash"] == imports["nara-electoral-2020"]["output_hash"]
        rebuilt = build_release(clone, connection)
    assert rebuilt["release_id"] == built["release_id"] and rebuilt["build"] == "unchanged_inputs"


def test_appointed_versus_voting_electors_in_the_total_row(federal, tmp_path):
    _, _, _, sources = federal
    page = tmp_path / "nara-1808.html"
    html = nara_modern_page(1808, [("James Madison, of Virginia", "p"), ("George Clinton, of New York", "v")],
                            [("Georgia", 6, [6, 6]), ("Maine", 4, [3, 3])]).decode()
    page.write_text(html.replace("<td>Total</td><td>10</td>", "<td>Totals</td><td>10 (9)*</td>"), encoding="utf-8")
    root, imports = load_universe(tmp_path / "root", {key: value for key, value in sources.items()
                                                      if key.startswith("census")} | {"nara-electoral-1808": page})
    assert imports["nara-electoral-1808"]["accounting"]["electors"] == 10
    with closing(connect(root.database)) as connection:
        assert connection.execute("SELECT sum(votes) FROM electoral_vote WHERE vote_role='president'").fetchone()[0] == 9
