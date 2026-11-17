"""Transport-independent read contracts for released election results.

Shares are fractions in [0, 1]; margins are percentage points. Vote counts are exact
integers (2024 county and state counts are far below 2**53, so JSON numbers are exact;
a future schema version would serialize larger counts as decimal strings).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "1"
Level = Literal["state", "county"]


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
    level: Level = "state"
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
    contest_id: str = Field(..., max_length=64, pattern=r"^contest_[0-9a-f]{24}$")
    level: Level = "county"
    limit: int = Field(50, ge=1, le=100)


class MapAsset(Contract):
    asset_id: str
    path: str
    sha256: str
    byte_length: int
    kind: str
    join_property: str


class MapFeature(Contract):
    feature_id: str
    unit_id: str | None
    result_status: str
    leader_candidacy_id: str | None
    leader_margin_points: float | None


class MapReport(Contract):
    api_version: str = API_VERSION
    release_id: str
    contest_id: str
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
