"""Transport-independent read contracts for released election results.

Shares are fractions in [0, 1]; margins are percentage points. Vote counts are exact
integers (2024 county and state counts are far below 2**53, so JSON numbers are exact;
a future schema version would serialize larger counts as decimal strings).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "1"
Level = Literal["state", "county", "district"]
Office = Literal["president", "senate", "house", "house_delegate"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Page(Contract):
    cursor: int = Field(0, ge=0, le=100_000, description="Offset returned as next_cursor by a previous page.")
    # Bounded so a full page of the largest records stays within a 32 KiB tool response.
    limit: int = Field(10, ge=1, le=15)


class ReleaseRef(Contract):
    release_id: str | None = Field(None, max_length=64, pattern=r"^release_[0-9a-f]{20}$",
                                   description="Pin a published release; defaults to the active release.")


class StatusRequest(Contract):
    pass


class ReleaseSummary(Contract):
    release_id: str
    status: str
    created_at: str
    published_at: str | None
    active: bool
    parent_release_id: str | None
    issue_counts: dict[str, int]


class SourceSummary(Contract):
    source_id: str
    publisher: str
    authority_kind: str
    license: str
    redistribution_status: str
    attribution: str
    access_conditions: str
    latest_artifact_sha256: str | None
    latest_retrieved_at: str | None
    latest_retrieval_status: str | None


class CoverageSummary(Contract):
    geography_level: str
    coverage_status: str
    contests: int


class StatusReport(Contract):
    api_version: str = API_VERSION
    configured: bool
    data_root_note: str
    active_release_id: str | None
    releases: list[ReleaseSummary]
    sources: list[SourceSummary]
    coverage: list[CoverageSummary]
    limitations: list[str]


class ContestsRequest(ReleaseRef, Page):
    office: Office | None = None
    year: int | None = Field(None, ge=1788, le=2100, description="Election cycle year.")
    state: str | None = Field(None, pattern=r"^[A-Z]{2}$", description="USPS postal code.")


class Outcome(Contract):
    outcome_type: str
    candidacy_id: str | None
    display_name: str | None
    basis: str
    notes: str | None


class CoverageCell(Contract):
    geography_level: str
    coverage_status: str
    expected_units: int | None
    available_units: int
    missing_units: list[str]
    boundary_status: str
    notes: str | None


class ContestSummary(Contract):
    contest_id: str
    label: str
    office: str
    cycle_year: int
    predecessor_contest_id: str | None
    seat_key: str
    stage: str
    contest_type: str
    voting_system: str
    selection_method: str
    outcomes: list[Outcome]
    coverage: list[CoverageCell]
    electoral_votes: dict[str, int]


class ContestsReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    contests: list[ContestSummary]
    next_cursor: int | None
    total: int


class ResultsRequest(ReleaseRef, Page):
    contest_id: str = Field(..., max_length=64, pattern=r"^contest_[0-9a-f]{24}$")
    level: Level | None = Field(None, description="Defaults to the contest's own scope: state, or district for House.")
    unit_id: str | None = Field(None, max_length=64, pattern=r"^unit_[0-9a-f]{24}$")


class CandidateResult(Contract):
    candidacy_id: str
    display_name: str
    candidacy_kind: str
    party: str | None
    votes: int | None
    share: float | None = Field(None, description="Fraction of the declared denominator, not a percentage.")
    result_status: str


class UnitResult(Contract):
    unit_id: str
    label: str
    unit_kind: str
    geography_level: str
    boundary_feature_id: str | None
    boundary_status: str
    result_status: str
    source_status: str
    round: str | None
    voting_mode: str
    denominator: dict[str, int | str] | None
    margin: dict[str, float | str] | None = Field(None, description="Top-two margin in percentage points.")
    results: list[CandidateResult]
    notes: list[str]
    provenance_ids: list[str]


class ResultsReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    contest_id: str
    contest_label: str
    level: Level
    coverage: CoverageCell | None
    units: list[UnitResult]
    next_cursor: int | None
    total: int


class ProvenanceRequest(Page):
    derivation_id: str = Field(..., max_length=64, pattern=r"^deriv_[0-9a-f]{24}$")


class EvidenceItem(Contract):
    input_kind: str
    input_id: str
    label: str
    votes: int | None
    voting_mode: str
    batch_key: str
    value_status: str
    source_id: str
    source_status: str
    revision_label: str
    artifact_sha256: str
    locator: dict[str, str | int]
    retrieved_from: str | None
    license: str


class ProvenanceReport(Contract):
    api_version: str = API_VERSION
    derivation_id: str
    rule_version: str
    parameters: dict
    output_hash: str
    import_runs: list[dict[str, str]]
    inputs: list[EvidenceItem]
    next_cursor: int | None
    total: int


class MapRequest(ReleaseRef, Page):
    contest_id: str | None = Field(None, max_length=64, pattern=r"^contest_[0-9a-f]{24}$",
                                   description="One contest's own geography (its state's counties, or its district).")
    office: Office | None = Field(None, description="With year: a national map of that office's general contests.")
    year: int | None = Field(None, ge=1788, le=2100)
    level: Level | None = None
    limit: int = Field(50, ge=1, le=80)


class MapAsset(Contract):
    asset_id: str
    path: str
    sha256: str
    byte_length: int
    kind: str
    join_property: str


class MapFeature(Contract):
    feature_id: str
    feature_key: str
    contest_id: str | None
    unit_id: str | None
    result_status: str
    leader_candidacy_id: str | None
    leader_margin_points: float | None


class MapReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    contest_id: str | None
    office: str
    cycle_year: int
    level: Level
    asset: MapAsset
    features: list[MapFeature]
    tabular_only_units: list[str]
    next_cursor: int | None
    total: int
    legend_notes: list[str]


class IssuesRequest(ReleaseRef, Page):
    severity: Literal["error", "warning", "info"] | None = None


class Issue(Contract):
    rule_id: str
    rule_version: int
    severity: str
    entity_type: str
    entity_id: str
    message: str
    expected: dict | list | int | str | None
    actual: dict | list | int | str | None
    resolution_status: str
    decision_id: str | None


class IssuesReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    issues: list[Issue]
    next_cursor: int | None
    total: int


class CoverageRequest(ReleaseRef, Page):
    office: Literal["president", "senate", "house"] | None = None
    from_year: int = Field(1976, ge=1788, le=2100)
    to_year: int = Field(2100, ge=1788, le=2100)
    series: Literal["contest_totals", "county_detail", "electoral_votes", "boundaries"] | None = None
    only_gaps: bool = Field(False, description="Return only rows whose status is not complete.")
    limit: int = Field(25, ge=1, le=50)


class CoverageRow(Contract):
    office: str
    cycle_year: int
    series: str
    coverage_status: str
    expected: int | None
    available: int
    missing: list[str]
    missing_total: int
    extra: dict
    notes: str | None


class CoverageReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    rows: list[CoverageRow]
    next_cursor: int | None
    total: int


class PresidentSummaryRequest(ReleaseRef):
    year: int = Field(..., ge=1788, le=2100)


class NationalCandidate(Contract):
    display_name: str
    party: str | None
    votes: int
    share: float = Field(..., description="Fraction of all reported options nationwide.")
    states_led: int
    states_reported: int


class ElectoralRecipient(Contract):
    name: str
    role: str
    electoral_votes: int


class PresidentSummaryReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    year: int
    popular_vote_status: str
    states_with_totals: int
    total_votes: int | None
    candidates: list[NationalCandidate]
    other_votes: int | None
    electoral_vote_status: str
    electoral_votes: list[ElectoralRecipient]
    notes: list[str]


class CompareRequest(ReleaseRef, Page):
    year_a: int = Field(..., ge=1976, le=2100)
    year_b: int = Field(..., ge=1976, le=2100)
    state: str | None = Field(None, pattern=r"^[A-Z]{2}$", description="Required for county level.")
    level: Literal["state", "county"] = "state"
    limit: int = Field(25, ge=1, le=60)


class ComparedUnit(Contract):
    unit_id: str
    label: str
    margin_a_points: float
    margin_b_points: float
    swing_points: float = Field(..., description="margin_b - margin_a; positive is toward the Democratic ticket.")
    total_a: int
    total_b: int


class CompareReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    office: str
    year_a: int
    year_b: int
    level: str
    method: str
    units: list[ComparedUnit]
    excluded_units: int
    exclusions: list[str]
    next_cursor: int | None
    total: int
