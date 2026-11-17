"""Read-only service over published releases. No request downloads or rebuilds data."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .contracts import (CandidateResult, CompareReport, CompareRequest, ComparedUnit, ContestsReport, ContestsRequest,
                        ContestSummary, CoverageCell, CoverageReport, CoverageRequest, CoverageRow, CoverageSummary,
                        ElectoralRecipient, EvidenceItem, Issue, IssuesReport, IssuesRequest, MapAsset, MapFeature,
                        MapReport, MapRequest, NationalCandidate, Outcome, PresidentSummaryReport,
                        PresidentSummaryRequest, ProvenanceReport, ProvenanceRequest, ReleaseSummary, ResultsReport,
                        ResultsRequest, SourceSummary, StatusReport, UnitResult)
from .core import ElectionDataError, InvalidInput, connect
from .federal import congress_for_election
from .pipeline import party_id


class NotFound(InvalidInput):
    code = "NOT_FOUND"


class IncompatibleGeography(InvalidInput):
    code = "INCOMPATIBLE_GEOGRAPHY"


LIMITATIONS = [
    "Contest totals: President, Senate and House general elections 1976-2024 from MIT Election Lab state-level "
    "files (secondary compilations), except 2024 President, which uses the FEC official compilation.",
    "County detail exists only for President 2000-2024 (MIT, secondary); differences from state totals are disclosed "
    "per contest. Congressional county and precinct detail is not researched.",
    "Winners are not sourced for Senate and House: outcomes there are derived plurality leaders, not certified winners.",
    "Electoral votes are as cast per NARA for 1789-2024 except 1800, 1868 and 1936, whose pages await review.",
    "2026 returns and all popular-vote returns before 1976 are not researched; the coverage inventory says so per cycle.",
    "Maine and Nebraska district popular votes are unavailable; Alaska county-level data is by state house district "
    "without geometry.",
    "Shares are fractions of all reported options in the selected unit; margins are percentage points.",
]
NAMED_KINDS = ("ticket", "candidate")


def _page(total: int, cursor: int, limit: int) -> int | None:
    return cursor + limit if cursor + limit < total else None


class ElectionDataReader:
    def __init__(self, data_root: Path | None):
        self.database = Path(data_root) / "db" / "elections.sqlite" if data_root else None

    def _connect(self) -> sqlite3.Connection:
        if self.database is None or not self.database.exists():
            raise InvalidInput("No election data root is configured or initialized.")
        return connect(self.database, read_only=True)

    @staticmethod
    def _release(connection: sqlite3.Connection, release_id: str | None) -> str:
        if release_id is None:
            row = connection.execute("SELECT release_id FROM active_release WHERE singleton=1").fetchone()
            if row is None:
                raise NotFound("No release has been published yet.")
            return row[0]
        row = connection.execute("SELECT status FROM data_release WHERE release_id=?", (release_id,)).fetchone()
        if row is None or row[0] != "published":
            raise NotFound(f"Release {release_id} is not a published release.")
        return release_id

    @staticmethod
    def _contest(connection: sqlite3.Connection, contest_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT k.*, o.code AS office, e.cycle_year FROM contest k JOIN office o USING (office_id) "
            "JOIN election_event e USING (election_id) WHERE k.contest_id=?", (contest_id,)).fetchone()
        if row is None:
            raise NotFound(f"Unknown contest {contest_id}.")
        return row

    def status(self, _request=None) -> StatusReport:
        if self.database is None or not self.database.exists():
            return StatusReport(configured=False, data_root_note="No initialized election data root is configured.",
                                active_release_id=None, releases=[], sources=[], coverage=[], limitations=LIMITATIONS)
        with closing(self._connect()) as connection:
            active = connection.execute("SELECT release_id FROM active_release WHERE singleton=1").fetchone()
            active_id = active[0] if active else None
            releases = []
            for row in connection.execute("SELECT * FROM data_release ORDER BY created_at DESC LIMIT 20"):
                counts = {severity: count for severity, count in connection.execute(
                    "SELECT severity, count(*) FROM validation_issue WHERE release_id=? GROUP BY severity",
                    (row["release_id"],))}
                releases.append(ReleaseSummary(release_id=row["release_id"], status=row["status"],
                                               created_at=row["created_at"], published_at=row["published_at"],
                                               active=row["release_id"] == active_id,
                                               parent_release_id=row["parent_release_id"], issue_counts=counts))
            sources = []
            for row in connection.execute(
                    "SELECT s.*, l.name AS license_name, l.redistribution_status FROM source s "
                    "JOIN license_record l USING (license_id) ORDER BY source_id"):
                latest = connection.execute(
                    "SELECT a.sha256, r.retrieved_at, r.status FROM artifact_retrieval r "
                    "LEFT JOIN source_artifact a USING (artifact_id) WHERE r.source_id=? "
                    "ORDER BY r.retrieved_at DESC, r.rowid DESC LIMIT 1", (row["source_id"],)).fetchone()
                sources.append(SourceSummary(
                    source_id=row["source_id"], publisher=row["publisher"], authority_kind=row["authority_kind"],
                    license=row["license_name"], redistribution_status=row["redistribution_status"],
                    attribution=row["attribution"], access_conditions=row["access_conditions"],
                    latest_artifact_sha256=latest["sha256"] if latest else None,
                    latest_retrieved_at=latest["retrieved_at"] if latest else None,
                    latest_retrieval_status=latest["status"] if latest else None))
            coverage = [CoverageSummary(geography_level=level, coverage_status=status, contests=count)
                        for level, status, count in connection.execute(
                            "SELECT geography_level, coverage_status, count(*) FROM coverage_cell WHERE release_id=? "
                            "GROUP BY 1, 2 ORDER BY 1, 2", (active_id,))] if active_id else []
        return StatusReport(configured=True, data_root_note="Configured local election data root.",
                            active_release_id=active_id, releases=releases, sources=sources, coverage=coverage,
                            limitations=LIMITATIONS)

    @staticmethod
    def _coverage(connection, release: str, contest: str, level: str | None = None) -> list[CoverageCell]:
        query = "SELECT * FROM coverage_cell WHERE release_id=? AND contest_id=?"
        values = [release, contest]
        if level:
            query += " AND geography_level=?"
            values.append(level)
        return [CoverageCell(geography_level=row["geography_level"], coverage_status=row["coverage_status"],
                             expected_units=row["expected_units"], available_units=row["available_units"],
                             missing_units=json.loads(row["missing_units_json"])[:50],
                             boundary_status=row["boundary_status"], notes=row["notes"])
                for row in connection.execute(query + " ORDER BY geography_level", values)]

    def contests(self, request: ContestsRequest) -> ContestsReport:
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)
            query = ("SELECT k.*, o.code AS office, e.cycle_year FROM contest k JOIN office o USING (office_id) "
                     "JOIN election_event e USING (election_id) WHERE k.contest_id IN "
                     "(SELECT contest_id FROM coverage_cell WHERE release_id=?)")
            values: list = [release]
            if request.office:
                query += " AND o.code=?"
                values.append(request.office)
            if request.year:
                query += " AND e.cycle_year=?"
                values.append(request.year)
            if request.state:
                query += " AND k.seat_key LIKE ?"
                values.append(f"%:{request.state}%")
            rows = connection.execute(query + " ORDER BY e.cycle_year DESC, o.code, k.seat_key, k.stage, k.contest_type",
                                      values).fetchall()
            if request.state:
                rows = [row for row in rows if row["seat_key"].split(":")[1] == request.state]
            total = len(rows)
            contests = []
            for row in rows[request.cursor:request.cursor + request.limit]:
                outcomes = [Outcome(outcome_type=item["outcome_type"], candidacy_id=item["candidacy_id"],
                                    display_name=item["display_name"], basis=item["basis"], notes=item["notes"])
                            for item in connection.execute(
                                "SELECT o.*, c.display_name FROM published_outcome o LEFT JOIN candidacy c "
                                "USING (candidacy_id) WHERE o.release_id=? AND o.contest_id=? ORDER BY outcome_type",
                                (release, row["contest_id"]))]
                electoral: dict[str, int] = {}
                if row["office"] == "president" and ":cd" not in row["seat_key"]:
                    for item in connection.execute(
                            "SELECT coalesce(p.canonical_name, 'Other recipients') AS name, ev.vote_role, sum(ev.votes) AS votes "
                            "FROM published_electoral_vote pe JOIN electoral_vote ev USING (electoral_vote_id) "
                            "JOIN electoral_allocation ea USING (allocation_id) "
                            "LEFT JOIN person p ON p.person_id = ev.recipient_person_id "
                            "WHERE pe.release_id=? AND ea.geo_version_id=? AND ea.election_id=? "
                            "AND ea.allocation_kind='state_total' GROUP BY 1, 2",
                            (release, row["scope_geo_version_id"], row["election_id"])):
                        electoral[f"{item['vote_role']}:{item['name']}"] = item["votes"]
                contests.append(ContestSummary(
                    contest_id=row["contest_id"], label=row["label"], office=row["office"], cycle_year=row["cycle_year"],
                    predecessor_contest_id=row["predecessor_contest_id"], seat_key=row["seat_key"], stage=row["stage"],
                    contest_type=row["contest_type"], voting_system=row["voting_system"],
                    selection_method=row["selection_method"], outcomes=outcomes,
                    coverage=self._coverage(connection, release, row["contest_id"]), electoral_votes=electoral))
        return ContestsReport(release_id=release, contests=contests, next_cursor=_page(total, request.cursor,
                                                                                         request.limit), total=total)

    def results(self, request: ResultsRequest) -> ResultsReport:
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)
            contest = self._contest(connection, request.contest_id)
            level = request.level or ("district" if contest["office"] in ("house", "house_delegate") else "state")
            query = ("SELECT s.*, u.label, u.kind FROM published_unit_status s JOIN reporting_unit u USING (unit_id) "
                     "WHERE s.release_id=? AND s.contest_id=? AND s.geography_level=?")
            values = [release, request.contest_id, level]
            if request.unit_id:
                if connection.execute("SELECT 1 FROM reporting_unit WHERE unit_id=?", (request.unit_id,)).fetchone() is None:
                    raise NotFound(f"Unknown reporting unit {request.unit_id}.")
                query += " AND s.unit_id=?"
                values.append(request.unit_id)
            statuses = connection.execute(query + " ORDER BY u.label, s.unit_id", values).fetchall()
            total = len(statuses)
            units = [self._unit(connection, release, request.contest_id, row)
                     for row in statuses[request.cursor:request.cursor + request.limit]]
            coverage = self._coverage(connection, release, request.contest_id, level)
        return ResultsReport(release_id=release, contest_id=request.contest_id, contest_label=contest["label"],
                             level=level, coverage=coverage[0] if coverage else None, units=units,
                             next_cursor=_page(total, request.cursor, request.limit), total=total)

    @staticmethod
    def _unit(connection, release: str, contest: str, status) -> UnitResult:
        rows = connection.execute(
            "SELECT r.*, c.display_name, c.candidacy_kind, p.canonical_name AS party FROM published_result r "
            "JOIN candidacy c USING (candidacy_id) LEFT JOIN party p ON p.party_id = c.primary_party_id "
            "WHERE r.release_id=? AND r.contest_id=? AND r.unit_id=? ORDER BY r.votes DESC, c.display_name",
            (release, contest, status["unit_id"])).fetchall()
        denominator = rows[0]["denominator"] if rows else None
        results = [CandidateResult(
            candidacy_id=row["candidacy_id"], display_name=row["display_name"], candidacy_kind=row["candidacy_kind"],
            party=row["party"], votes=row["votes"],
            share=round(row["votes"] / denominator, 6) if denominator and row["votes"] is not None else None,
            result_status=row["result_status"]) for row in rows]
        margin = None
        named = [row for row in results if row.candidacy_kind in NAMED_KINDS and row.share is not None]
        if len(named) >= 2:
            margin = {"kind": "top_two_percentage_points", "value": round((named[0].share - named[1].share) * 100, 4),
                      "leader_candidacy_id": named[0].candidacy_id}
        elif len(named) == 1 and denominator:
            margin = {"kind": "sole_named_candidate_percentage_points", "value": round(named[0].share * 100, 4),
                      "leader_candidacy_id": named[0].candidacy_id}
        return UnitResult(
            unit_id=status["unit_id"], label=status["label"], unit_kind=status["kind"],
            geography_level=status["geography_level"], boundary_feature_id=status["boundary_feature_id"],
            boundary_status=status["boundary_status"], result_status=status["result_status"],
            source_status=status["source_status"], round=rows[0]["round_key"] if rows else None, voting_mode="all",
            denominator={"kind": rows[0]["denominator_kind"], "value": denominator} if denominator else None,
            margin=margin, results=results, notes=json.loads(status["notes_json"]),
            provenance_ids=sorted({row["derivation_id"] for row in rows}))

    def provenance(self, request: ProvenanceRequest) -> ProvenanceReport:
        with closing(self._connect()) as connection:
            derivation = connection.execute("SELECT * FROM derivation WHERE derivation_id=?",
                                            (request.derivation_id,)).fetchone()
            if derivation is None:
                raise NotFound(f"Unknown derivation {request.derivation_id}.")
            inputs = connection.execute("SELECT * FROM derivation_input WHERE derivation_id=? ORDER BY input_kind, input_id",
                                        (request.derivation_id,)).fetchall()
            items = []
            runs: dict[str, dict] = {}
            for row in inputs[request.cursor:request.cursor + request.limit]:
                if row["input_kind"] == "vote_observation":
                    fact = connection.execute(
                        "SELECT o.observation_id AS id, coalesce(o.raw_candidate_label, b.label) AS label, o.votes, "
                        "o.voting_mode, o.batch_key, o.value_status, o.set_id FROM vote_observation o "
                        "JOIN ballot_option b USING (option_id) WHERE o.observation_id=?", (row["input_id"],)).fetchone()
                else:
                    fact = connection.execute(
                        "SELECT metric_id AS id, metric_code AS label, integer_value AS votes, voting_mode, batch_key, "
                        "value_status, set_id FROM metric_observation WHERE metric_id=?", (row["input_id"],)).fetchone()
                context = connection.execute(
                    "SELECT rs.source_id, rs.source_status, rs.revision_label, rs.run_id, ir.adapter_id, ir.adapter_version, "
                    "ir.code_revision, ir.import_identity, l.name AS license FROM source_result_set rs "
                    "JOIN import_run ir USING (run_id) JOIN source s ON s.source_id = rs.source_id "
                    "JOIN license_record l ON l.license_id = s.license_id WHERE rs.set_id=?", (fact["set_id"],)).fetchone()
                evidence = connection.execute(
                    "SELECT e.locator_json, a.sha256, a.artifact_id FROM observation_evidence e "
                    "JOIN source_artifact a USING (artifact_id) WHERE e.observation_id=?", (fact["id"],)).fetchone()
                retrieval = connection.execute(
                    "SELECT requested_url FROM artifact_retrieval WHERE artifact_id=? ORDER BY retrieved_at LIMIT 1",
                    (evidence["artifact_id"],)).fetchone()
                runs[context["run_id"]] = {"run_id": context["run_id"], "adapter_id": context["adapter_id"],
                                           "adapter_version": context["adapter_version"],
                                           "code_revision": context["code_revision"],
                                           "import_identity": context["import_identity"]}
                items.append(EvidenceItem(
                    input_kind=row["input_kind"], input_id=row["input_id"], label=fact["label"] or "(blank)",
                    votes=fact["votes"], voting_mode=fact["voting_mode"], batch_key=fact["batch_key"],
                    value_status=fact["value_status"], source_id=context["source_id"],
                    source_status=context["source_status"], revision_label=context["revision_label"],
                    artifact_sha256=evidence["sha256"], locator=json.loads(evidence["locator_json"]),
                    retrieved_from=retrieval["requested_url"] if retrieval else None, license=context["license"]))
        return ProvenanceReport(derivation_id=derivation["derivation_id"], rule_version=derivation["rule_version"],
                                parameters=json.loads(derivation["parameters_json"]),
                                output_hash=derivation["output_hash"], import_runs=list(runs.values()), inputs=items,
                                next_cursor=_page(len(inputs), request.cursor, request.limit), total=len(inputs))

    # --- maps ----------------------------------------------------------------------------------
    @staticmethod
    def _asset(connection, release: str, kind: str, legacy_name: str) -> sqlite3.Row:
        asset = connection.execute("SELECT * FROM release_asset WHERE release_id=? AND kind=?", (release, kind)).fetchone()
        if asset is None:  # releases built before content-addressed assets
            asset = connection.execute("SELECT * FROM release_asset WHERE release_id=? AND path LIKE ?",
                                       (release, f"%/{legacy_name}")).fetchone()
        if asset is None:
            raise IncompatibleGeography(f"Release {release} has no map asset for {kind.split(':')[-1]}.")
        return asset

    def map(self, request: MapRequest) -> MapReport:
        if (request.contest_id is None) == (request.office is None or request.year is None):
            raise InvalidInput("Pass either contest_id, or office and year for a national map.")
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)
            if request.contest_id:
                contest = self._contest(connection, request.contest_id)
                office, cycle = contest["office"], contest["cycle_year"]
                postal = contest["seat_key"].split(":")[1]
                default = "district" if office in ("house", "house_delegate") else "county"
            else:
                contest, office, cycle, postal, default = None, request.office, request.year, None, \
                    "district" if request.office in ("house", "house_delegate") else "state"
            level = request.level or default
            if level == "district" and office not in ("house", "house_delegate"):
                raise IncompatibleGeography("District maps exist only for House contests.")
            if level == "county" and contest is None:
                raise IncompatibleGeography("County maps are served one state contest at a time; pass contest_id.")
            notes = ["no_data: no published result is joined to this feature (distinct from zero votes or ties)",
                     "margin is top-two percentage points among named candidates"]
            if level == "district":
                congress = congress_for_election(cycle)
                drawn = 116 if congress == 117 else congress
                asset = self._asset(connection, release, f"geojson:districts-{drawn}", f"districts-{drawn}.geojson")
                boundary_set = connection.execute("SELECT boundary_set_id FROM boundary_set WHERE vintage=?",
                                                  (f"congress-{drawn}",)).fetchone()[0]
                pattern = f"{postal}:%" if postal else "%"
                notes.append(f"geometry: districts of the {drawn}th Congress" + (
                    " shown for the 2020 election; North Carolina was redistricted and has no geometry" if congress == 117 else ""))
            else:
                name = "states" if level == "state" else "counties"
                asset = self._asset(connection, release, f"geojson:{name}", f"{name}.geojson")
                boundary_set = connection.execute(
                    "SELECT boundary_set_id FROM boundary_set WHERE layer=?",
                    ("cb_2020_us_state_20m" if level == "state" else "cb_2020_us_county_20m",)).fetchone()[0]
                fips = connection.execute(
                    "SELECT value FROM geographic_identifier WHERE scheme='state_fips' AND geo_version_id=?",
                    (contest["scope_geo_version_id"],)).fetchone()[0] if contest is not None else None
                pattern = "%" if fips is None else f"{fips}%" if level == "county" else fips
                notes.append("geometry: Census 2020 1:20M cartographic boundaries (display generalization)")
            features = connection.execute(
                "SELECT feature_id, source_feature_key FROM boundary_feature WHERE boundary_set_id=? "
                "AND source_feature_key LIKE ? ORDER BY source_feature_key", (boundary_set, pattern)).fetchall()
            if request.contest_id:
                statuses = connection.execute(
                    "SELECT contest_id, unit_id, boundary_feature_id, result_status FROM published_unit_status "
                    "WHERE release_id=? AND contest_id=? AND geography_level=?", (release, request.contest_id, level)).fetchall()
            else:
                statuses = connection.execute(
                    "SELECT s.contest_id, s.unit_id, s.boundary_feature_id, s.result_status FROM published_unit_status s "
                    "JOIN contest k USING (contest_id) JOIN office o USING (office_id) JOIN election_event e USING (election_id) "
                    "WHERE s.release_id=? AND s.geography_level=? AND o.code=? AND e.cycle_year=? "
                    "AND k.stage IN ('general', 'primary') ORDER BY k.contest_type DESC",
                    (release, level, office, cycle)).fetchall()
                if not statuses:
                    raise NotFound(f"No published {office} contests for {cycle}.")
                notes.append("national view shows general-election stages; runoffs and concurrent specials are separate contests")
            by_feature = {row["boundary_feature_id"]: row for row in statuses if row["boundary_feature_id"]}
            tabular = sorted(row["unit_id"] for row in statuses if not row["boundary_feature_id"])
            output = []
            for feature in features[request.cursor:request.cursor + request.limit]:
                status = by_feature.get(feature["feature_id"])
                leader = margin = None
                if status and status["result_status"] == "complete":
                    unit = self._unit(connection, release, status["contest_id"], {
                        "unit_id": status["unit_id"], "label": "", "kind": "", "geography_level": level,
                        "boundary_feature_id": feature["feature_id"], "boundary_status": "matched",
                        "result_status": status["result_status"], "source_status": "", "notes_json": "[]"})
                    if unit.margin:
                        leader, margin = unit.margin["leader_candidacy_id"], unit.margin["value"]
                output.append(MapFeature(
                    feature_id=feature["feature_id"], feature_key=feature["source_feature_key"],
                    contest_id=status["contest_id"] if status else None, unit_id=status["unit_id"] if status else None,
                    result_status=status["result_status"] if status else "no_data",
                    leader_candidacy_id=leader, leader_margin_points=margin))
        return MapReport(
            release_id=release, contest_id=request.contest_id, office=office, cycle_year=cycle, level=level,
            asset=MapAsset(asset_id=asset["asset_id"], path=asset["path"], sha256=asset["sha256"],
                           byte_length=asset["byte_length"], kind=asset["kind"], join_property="feature_id"),
            features=output, tabular_only_units=tabular[:50],
            next_cursor=_page(len(features), request.cursor, request.limit), total=len(features), legend_notes=notes)

    # --- coverage, national summary, comparison ------------------------------------------------
    @staticmethod
    def _president_contests(connection, year: int) -> dict[str, str]:
        """contest_id -> seat_key for one cycle's presidential contests (small, index-backed)."""
        return {row[0]: row[1] for row in connection.execute(
            "SELECT k.contest_id, k.seat_key FROM contest k JOIN election_event e USING (election_id) "
            "JOIN office o USING (office_id) WHERE e.cycle_year=? AND o.code='president'", (year,))}

    def coverage(self, request: CoverageRequest) -> CoverageReport:
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)
            query = "SELECT * FROM coverage_inventory WHERE release_id=? AND cycle_year BETWEEN ? AND ?"
            values: list = [release, request.from_year, request.to_year]
            for column, value in (("office_code", request.office), ("series", request.series)):
                if value:
                    query += f" AND {column}=?"
                    values.append(value)
            if request.only_gaps:
                query += " AND coverage_status <> 'complete'"
            rows = connection.execute(query + " ORDER BY cycle_year DESC, office_code, series", values).fetchall()
            page = []
            for row in rows[request.cursor:request.cursor + request.limit]:
                missing = json.loads(row["missing_json"])
                page.append(CoverageRow(office=row["office_code"], cycle_year=row["cycle_year"], series=row["series"],
                                        coverage_status=row["coverage_status"], expected=row["expected"],
                                        available=row["available"], missing=missing[:15], missing_total=len(missing),
                                        extra=json.loads(row["extra_json"]), notes=row["notes"]))
        return CoverageReport(release_id=release, rows=page, next_cursor=_page(len(rows), request.cursor, request.limit),
                              total=len(rows))

    def president_summary(self, request: PresidentSummaryRequest) -> PresidentSummaryReport:
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)
            contests = self._president_contests(connection, request.year)
            marks = ",".join("?" * len(contests))
            rows = connection.execute(
                "SELECT r.contest_id, c.display_name, c.candidacy_kind, c.natural_key, p.canonical_name AS party, r.votes, "
                "r.denominator FROM published_unit_status s JOIN published_result r ON r.release_id=s.release_id "
                "AND r.contest_id=s.contest_id AND r.unit_id=s.unit_id JOIN candidacy c USING (candidacy_id) "
                "LEFT JOIN party p ON p.party_id = c.primary_party_id "
                f"WHERE s.release_id=? AND s.geography_level='state' AND s.contest_id IN ({marks})",
                (release, *contests)).fetchall()
            electoral = connection.execute(
                "SELECT coalesce(p.canonical_name, 'Other recipients') AS name, ev.vote_role, sum(ev.votes) AS votes "
                "FROM published_electoral_vote pe JOIN electoral_vote ev USING (electoral_vote_id) "
                "JOIN electoral_allocation ea USING (allocation_id) JOIN election_event e USING (election_id) "
                "LEFT JOIN person p ON p.person_id = ev.recipient_person_id "
                "WHERE pe.release_id=? AND e.cycle_year=? AND ea.allocation_kind='state_total' "
                "GROUP BY 1, 2 ORDER BY ev.vote_role, votes DESC", (release, request.year)).fetchall()
            inventory = {row["series"]: row["coverage_status"] for row in connection.execute(
                "SELECT series, coverage_status FROM coverage_inventory WHERE release_id=? AND office_code='president' "
                "AND cycle_year=?", (release, request.year))}
        if not rows and not electoral and not inventory:
            raise NotFound(f"{request.year} is not a presidential election year in this release.")
        tickets: dict[str, dict] = {}
        leaders: dict[str, tuple[int, str]] = {}
        contests, total, other = set(), 0, 0  # contests with a published state total
        for row in rows:
            contests.add(row["contest_id"])
            votes = row["votes"] or 0
            total += votes
            if row["candidacy_kind"] != "ticket":
                other += votes
            else:
                entry = tickets.setdefault(row["natural_key"], {"name": row["display_name"], "party": row["party"],
                                                                 "votes": 0, "states": 0, "led": 0})
                entry["votes"] += votes
                entry["states"] += 1
            if row["candidacy_kind"] in NAMED_KINDS and votes > leaders.get(row["contest_id"], (-1, ""))[0]:
                leaders[row["contest_id"]] = (votes, row["natural_key"])
        for _, key in leaders.values():
            if key in tickets:
                tickets[key]["led"] += 1
        candidates = [NationalCandidate(display_name=entry["name"], party=entry["party"], votes=entry["votes"],
                                        share=round(entry["votes"] / total, 6) if total else 0.0,
                                        states_led=entry["led"], states_reported=entry["states"])
                      for entry in sorted(tickets.values(), key=lambda item: -item["votes"])]
        notes = ["Popular totals sum each state's selected contest total; 'other_votes' covers candidates outside the "
                 "curated tickets, write-ins and source aggregates.",
                 "states_led counts plurality leaders by popular vote and is not an electoral outcome."]
        return PresidentSummaryReport(
            release_id=release, year=request.year,
            popular_vote_status=inventory.get("contest_totals", "not_researched"), states_with_totals=len(contests),
            total_votes=total or None, candidates=candidates, other_votes=other if rows else None,
            electoral_vote_status=inventory.get("electoral_votes", "not_researched"),
            electoral_votes=[ElectoralRecipient(name=row["name"], role=row["vote_role"], electoral_votes=row["votes"])
                             for row in electoral], notes=notes)

    def compare(self, request: CompareRequest) -> CompareReport:
        """Democratic-minus-Republican margin change between two cycles on identical reporting units."""
        if request.year_a == request.year_b:
            raise InvalidInput("Choose two different cycles.")
        if request.level == "county" and request.state is None:
            raise InvalidInput("County comparisons are served one state at a time; pass state.")
        democratic, republican = party_id("democratic"), party_id("republican")
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)

            def margins(year: int) -> dict[str, tuple[float, int, str]]:
                contests = {contest: seat for contest, seat in self._president_contests(connection, year).items()
                            if request.state is None or seat.split(":")[1] == request.state}
                marks = ",".join("?" * len(contests))
                found: dict[str, dict] = {}
                for row in connection.execute(
                        "SELECT r.unit_id, u.label, c.primary_party_id AS party, r.votes, r.denominator "
                        "FROM published_unit_status s JOIN published_result r ON r.release_id=s.release_id "
                        "AND r.contest_id=s.contest_id AND r.unit_id=s.unit_id JOIN candidacy c USING (candidacy_id) "
                        "JOIN reporting_unit u ON u.unit_id = r.unit_id "
                        f"WHERE s.release_id=? AND s.geography_level=? AND s.result_status='complete' "
                        f"AND c.candidacy_kind='ticket' AND s.contest_id IN ({marks})",
                        (release, request.level, *contests)):
                    entry = found.setdefault(row["unit_id"], {"label": row["label"], "total": row["denominator"]})
                    if row["party"] in (democratic, republican):
                        entry[row["party"]] = row["votes"]
                return {unit: ((entry[democratic] - entry[republican]) / entry["total"] * 100, entry["total"], entry["label"])
                        for unit, entry in found.items() if democratic in entry and republican in entry and entry["total"]}

            first, second = margins(request.year_a), margins(request.year_b)
        if not first or not second:
            raise NotFound("One of the cycles has no complete published results at this level.")
        shared = sorted(set(first) & set(second), key=lambda unit: (first[unit][2], unit))
        if not shared:
            raise IncompatibleGeography("The two cycles share no identical reporting unit at this level.")
        excluded = len(set(first) ^ set(second))
        units = [ComparedUnit(unit_id=unit, label=first[unit][2], margin_a_points=round(first[unit][0], 4),
                              margin_b_points=round(second[unit][0], 4),
                              swing_points=round(second[unit][0] - first[unit][0], 4), total_a=first[unit][1],
                              total_b=second[unit][1]) for unit in shared[request.cursor:request.cursor + request.limit]]
        exclusions = ["units present in only one cycle are excluded (for example Alaska house districts, which change "
                      "with each plan, or counties created, renamed or merged between the cycles)"] if excluded else []
        if request.level == "county":
            exclusions.append("county identity is the FIPS-coded unit; boundary changes that kept the code are not detected")
        return CompareReport(
            release_id=release, office="president", year_a=request.year_a, year_b=request.year_b, level=request.level,
            method="Democratic ticket share minus Republican ticket share, in percentage points of all reported options, "
                   "on units with the same identifier in both cycles; no crosswalk or vote reallocation is applied",
            units=units, excluded_units=excluded, exclusions=exclusions,
            next_cursor=_page(len(shared), request.cursor, request.limit), total=len(shared))

    def issues(self, request: IssuesRequest) -> IssuesReport:
        with closing(self._connect()) as connection:
            release = request.release_id or self._release(connection, None)
            if connection.execute("SELECT 1 FROM data_release WHERE release_id=?", (release,)).fetchone() is None:
                raise NotFound(f"Unknown release {release}.")
            query = "SELECT * FROM validation_issue WHERE release_id=?"
            values = [release]
            if request.severity:
                query += " AND severity=?"
                values.append(request.severity)
            rows = connection.execute(query + " ORDER BY severity, rule_id, entity_id", values).fetchall()
            issues = [Issue(rule_id=row["rule_id"], rule_version=row["rule_version"], severity=row["severity"],
                            entity_type=row["entity_type"], entity_id=row["entity_id"], message=row["message"],
                            expected=json.loads(row["expected_json"]) if row["expected_json"] else None,
                            actual=json.loads(row["actual_json"]) if row["actual_json"] else None,
                            resolution_status=row["resolution_status"], decision_id=row["decision_id"])
                      for row in rows[request.cursor:request.cursor + request.limit]]
        return IssuesReport(release_id=release, issues=issues, next_cursor=_page(len(rows), request.cursor,
                                                                                 request.limit), total=len(rows))


__all__ = ["ElectionDataError", "ElectionDataReader", "IncompatibleGeography", "NotFound"]
