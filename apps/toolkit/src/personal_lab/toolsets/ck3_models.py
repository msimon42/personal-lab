"""Concrete transport views for existing CK3 projections.

Known fields are validated; additional versioned domain evidence is retained as
JSON rather than flattened or silently discarded by the transport migration.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel, model_serializer

JSON = dict[str, JsonValue]


class Projection(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    __pydantic_extra__: dict[str, JsonValue] = Field(init=False)

    @model_serializer(mode="wrap")
    def retain_domain_presence(self, handler):
        data = handler(self)
        return {key: value for key, value in data.items()
                if key in self.model_fields_set or key in (self.__pydantic_extra__ or {})}


class ContextData(Projection):
    id: str
    workspace_id: str | None
    game_id: Literal["ck3"]
    target_platform: str
    state: Literal["ready", "needs_selection", "setup_needed"]
    source_status: JSON
    capabilities: JSON
    next_step: str


class SessionData(Projection):
    state: str
    workspace_id: str
    id: str | None = None
    notes: str | None = None
    goal: str | None = None
    next_step: str | None = None
    text_disclosure: JSON | None = None


class QueuedWork(Projection):
    job_id: str
    workspace_id: str | None = None
    context_id: str | None = None


class CheckUnavailable(Projection):
    state: str
    context: ContextData


class CheckData(RootModel[QueuedWork | ContextData | CheckUnavailable]):
    pass


class WorkData(Projection):
    run_id: str
    state: str
    instance_id: str
    result: JSON | None = None


class MissingModsData(Projection):
    context_id: str
    total: int = Field(ge=0)
    mods: list[JSON]
    next_offset: int | None


class KnowledgeSearchData(Projection):
    revision: str
    library_sha256: str
    applicability: JSON
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    entries: list[JSON]


class KnowledgeEntryData(Projection):
    id: str
    title: str
    summary: str
    topic: str
    sources: list[JSON]
    claims: list[JSON]
    grants_repair_capability: Literal[False]


class SavedEvidence(Projection):
    workspace_id: str
    index_id: str
    scan_id: str
    game_id: Literal["ck3"]
    target_platform: str
    freshness: JSON


class SearchData(SavedEvidence):
    kind: Literal["text", "path", "symbol"]
    results: list[JSON]
    total: int | None
    offset: int = Field(ge=0)
    next_offset: int | None
    coverage: str | JSON


class ExcludedEvidence(Projection):
    excluded: Literal[True]


class ContributionPage(SavedEvidence):
    target: str
    occurrence_count: int = Field(ge=0)
    occurrences: list[JSON]
    offset: int = Field(ge=0)
    next_offset: int | None
    continuation: str | None
    occurrences_continuation: JSON | None
    status: Literal["indexed", "no_matching_contributions", "not_in_available_index"]
    coverage: JSON


class ContributionData(RootModel[ContributionPage | ExcludedEvidence]):
    pass


class SourceMetadata(Projection):
    source_id: str
    owner: str
    path: str
    sha256: str
    size: int


class SourceAvailable(SavedEvidence):
    source: SourceMetadata
    status: Literal["available"]
    span: list[int] = Field(min_length=2, max_length=2)
    text: str
    next_offset: int | None


class SourceUnavailable(SavedEvidence):
    source: SourceMetadata
    status: Literal["unavailable"]
    reason: str


class SourceData(RootModel[SourceAvailable | SourceUnavailable | ExcludedEvidence]):
    pass


class SourcesData(SavedEvidence):
    results: list[SourceAvailable | SourceUnavailable | ExcludedEvidence]
    requested_bytes: int = Field(ge=0)
    source_byte_limit: int


class RelatedPage(SavedEvidence):
    definition: JSON
    edges: list[JSON]
    total: int = Field(ge=0)
    next_offset: int | None


class RelatedData(RootModel[RelatedPage | ExcludedEvidence]):
    pass


class BriefData(Projection):
    issue_id: str
    excluded: bool | None = None
    title: str | None = None
    mods: list[JSON] | None = None
    known_unknowns: list[str] | None = None


class ConcernsData(SavedEvidence):
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    concerns: list[JSON]
    summary: JSON


class ConcernEvidenceData(SavedEvidence):
    concern_id: str
    contributions: list[JSON]
    comparisons: list[JSON]
    descriptors: list[JSON]
    evidence_refs: list[JSON]
    coverage: JSON
    continuation: str | None
    next_offset: int | None


class ReviewData(SavedEvidence):
    review: JSON
    summary: JSON


class ReportData(ConcernsData):
    narrative: str
    missing_mod_count: int = Field(ge=0)
    missing_mods: list[JSON]
    coverage: JSON


class CandidateSearchData(MissingModsData):
    search_id: str
    coverage: JSON
    scope: Literal["candidates_only_launcher_unchanged"]


class VersionPreferenceData(Projection):
    id: str
    preference: Literal["preserve_current", "open_to_change", "undecided"]
    trust: str


class ImportData(Projection):
    workspace_id: str
    workspace: JSON


class CancellationData(Projection):
    job_id: str
    cancellation_requested: bool


class IssuePage(Projection):
    index_id: str
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int | None
    issues: list[JSON]
    summary: JSON
    coverage: JSON


class IssueData(Projection):
    id: str | None = None
    issue_id: str | None = None
    excluded: bool | None = None
    kind: str | None = None
    target: str | None = None
    sources: list[JSON] | None = None


class ChoiceData(Projection):
    state: str
    proposal_id: str | None = None
    question_id: str | None = None
    selected_option: str | None = None
    enabled: bool | None = None
    options: list[JSON] | None = None


class ResumeData(RootModel[ContextData | SessionData]):
    pass


class ChoiceOutcome(RootModel[ChoiceData | QueuedWork]):
    pass
