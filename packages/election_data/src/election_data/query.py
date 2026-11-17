"""Read-only service over published releases. No request downloads or rebuilds data."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .contracts import (CandidateResult, ContestsReport, ContestsRequest, ContestSummary, CoverageCell,
                        CoverageSummary, EvidenceItem, Issue, IssuesReport, IssuesRequest, MapAsset, MapFeature,
                        MapReport, MapRequest, Outcome, ProvenanceReport, ProvenanceRequest, ReleaseSummary,
                        ResultsReport, ResultsRequest, SourceSummary, StatusReport, UnitResult)
from .core import ElectionDataError, InvalidInput, connect
from .pipeline import state_versions


class NotFound(InvalidInput):
    code = "NOT_FOUND"


LIMITATIONS = [
    "Phase 1 covers the 2024 presidential general election: state totals (FEC official compilation), county "
    "detail (MIT Election Lab, secondary) and actual electoral votes (NARA).",
    "County detail differs from official state totals in some states; each difference is disclosed per contest.",
    "Maine and Nebraska district popular votes are unavailable; their district elector outcomes come from NARA.",
    "Alaska is reported by state house district without district geometry (tabular only).",
    "Shares are fractions of all reported options in the selected unit; margins are percentage points.",
]


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
            query = ("SELECT c.* FROM contest c JOIN coverage_cell cc ON cc.contest_id=c.contest_id AND cc.release_id=? "
                     "GROUP BY c.contest_id")
            values: list = [release]
            rows = connection.execute(query + " ORDER BY c.seat_key", values).fetchall()
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
                electoral = {}
                if row["seat_key"].count(":") == 1 or row["seat_key"].endswith(":at-large"):
                    for item in connection.execute(
                            "SELECT p.canonical_name, ev.vote_role, ev.votes FROM published_electoral_vote pe "
                            "JOIN electoral_vote ev USING (electoral_vote_id) JOIN electoral_allocation ea USING "
                            "(allocation_id) JOIN person p ON p.person_id = ev.recipient_person_id "
                            "WHERE pe.release_id=? AND ea.geo_version_id=? AND ea.allocation_kind='state_total'",
                            (release, row["scope_geo_version_id"])):
                        electoral[f"{item['vote_role']}:{item['canonical_name']}"] = item["votes"]
                contests.append(ContestSummary(
                    contest_id=row["contest_id"], label=row["label"], seat_key=row["seat_key"], stage=row["stage"],
                    contest_type=row["contest_type"], voting_system=row["voting_system"],
                    selection_method=row["selection_method"], outcomes=outcomes,
                    coverage=self._coverage(connection, release, row["contest_id"]), electoral_votes=electoral))
        return ContestsReport(release_id=release, contests=contests, next_cursor=_page(total, request.cursor,
                                                                                         request.limit), total=total)

    def results(self, request: ResultsRequest) -> ResultsReport:
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)
            contest = connection.execute("SELECT label FROM contest WHERE contest_id=?", (request.contest_id,)).fetchone()
            if contest is None:
                raise NotFound(f"Unknown contest {request.contest_id}.")
            query = ("SELECT s.*, u.label, u.kind FROM published_unit_status s JOIN reporting_unit u USING (unit_id) "
                     "WHERE s.release_id=? AND s.contest_id=? AND s.geography_level=?")
            values = [release, request.contest_id, request.level]
            if request.unit_id:
                if connection.execute("SELECT 1 FROM reporting_unit WHERE unit_id=?", (request.unit_id,)).fetchone() is None:
                    raise NotFound(f"Unknown reporting unit {request.unit_id}.")
                query += " AND s.unit_id=?"
                values.append(request.unit_id)
            statuses = connection.execute(query + " ORDER BY u.label, s.unit_id", values).fetchall()
            total = len(statuses)
            units = [self._unit(connection, release, request.contest_id, row)
                     for row in statuses[request.cursor:request.cursor + request.limit]]
            coverage = self._coverage(connection, release, request.contest_id, request.level)
        return ResultsReport(release_id=release, contest_id=request.contest_id, contest_label=contest["label"],
                             level=request.level, coverage=coverage[0] if coverage else None, units=units,
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
        named = [row for row in results if row.candidacy_kind == "ticket" and row.share is not None]
        if len(named) >= 2:
            margin = {"kind": "top_two_percentage_points", "value": round((named[0].share - named[1].share) * 100, 4),
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
                    input_kind=row["input_kind"], input_id=row["input_id"], label=fact["label"], votes=fact["votes"],
                    voting_mode=fact["voting_mode"], batch_key=fact["batch_key"], value_status=fact["value_status"],
                    source_id=context["source_id"], source_status=context["source_status"],
                    revision_label=context["revision_label"], artifact_sha256=evidence["sha256"],
                    locator=json.loads(evidence["locator_json"]),
                    retrieved_from=retrieval["requested_url"] if retrieval else None, license=context["license"]))
        return ProvenanceReport(derivation_id=derivation["derivation_id"], rule_version=derivation["rule_version"],
                                parameters=json.loads(derivation["parameters_json"]),
                                output_hash=derivation["output_hash"], import_runs=list(runs.values()), inputs=items,
                                next_cursor=_page(len(inputs), request.cursor, request.limit), total=len(inputs))

    def map(self, request: MapRequest) -> MapReport:
        name = "states.geojson" if request.level == "state" else "counties.geojson"
        with closing(self._connect()) as connection:
            release = self._release(connection, request.release_id)
            contest = connection.execute("SELECT scope_geo_version_id FROM contest WHERE contest_id=?",
                                         (request.contest_id,)).fetchone()
            if contest is None:
                raise NotFound(f"Unknown contest {request.contest_id}.")
            asset = connection.execute("SELECT * FROM release_asset WHERE release_id=? AND path LIKE ?",
                                       (release, f"%/{name}")).fetchone()
            if asset is None:
                raise ElectionDataError(f"Release {release} lacks its {name} asset.")
            statuses = connection.execute(
                "SELECT unit_id, boundary_feature_id, result_status FROM published_unit_status WHERE release_id=? "
                "AND contest_id=? AND geography_level=? ORDER BY unit_id", (release, request.contest_id, request.level)
            ).fetchall()
            fips = next(row["fips"] for row in state_versions(connection).values()
                        if row["geo_version_id"] == contest["scope_geo_version_id"])
            boundary_set = connection.execute(
                "SELECT boundary_set_id FROM boundary_set WHERE layer=?",
                ("cb_2020_us_state_20m" if request.level == "state" else "cb_2020_us_county_20m",)).fetchone()[0]
            prefix = fips if request.level == "county" else ""
            features = connection.execute(
                "SELECT feature_id FROM boundary_feature WHERE boundary_set_id=? AND source_feature_key LIKE ? "
                "AND (? = 'county' OR source_feature_key = ?) ORDER BY source_feature_key",
                (boundary_set, f"{prefix}%", request.level, fips)).fetchall()
            by_feature = {row["boundary_feature_id"]: row for row in statuses if row["boundary_feature_id"]}
            tabular = sorted(row["unit_id"] for row in statuses if not row["boundary_feature_id"])
            page = features[request.cursor:request.cursor + request.limit]
            output = []
            for feature in page:
                status = by_feature.get(feature["feature_id"])
                leader = margin = None
                if status and status["result_status"] == "complete":
                    unit = self._unit(connection, release, request.contest_id, {
                        "unit_id": status["unit_id"], "label": "", "kind": "", "geography_level": request.level,
                        "boundary_feature_id": feature["feature_id"], "boundary_status": "matched",
                        "result_status": status["result_status"], "source_status": "", "notes_json": "[]"})
                    if unit.margin:
                        leader, margin = unit.margin["leader_candidacy_id"], unit.margin["value"]
                output.append(MapFeature(feature_id=feature["feature_id"], unit_id=status["unit_id"] if status else None,
                                         result_status=status["result_status"] if status else "no_data",
                                         leader_candidacy_id=leader, leader_margin_points=margin))
        return MapReport(
            release_id=release, contest_id=request.contest_id, level=request.level,
            asset=MapAsset(asset_id=asset["asset_id"], path=asset["path"], sha256=asset["sha256"],
                           byte_length=asset["byte_length"], kind=asset["kind"], join_property="feature_id"),
            features=output, tabular_only_units=tabular[:100],
            next_cursor=_page(len(features), request.cursor, request.limit), total=len(features),
            legend_notes=["no_data: no published result is joined to this feature (distinct from zero votes or ties)",
                          "margin is top-two percentage points among named tickets",
                          "geometry: Census 2020 1:20M cartographic boundaries (display generalization)"])

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
