"""Typed local connector requests. Approval answers are not public model tools."""
from typing import Literal

from pydantic import Field, model_validator

from backend.app.contracts import DiscoverCK3, StrictModel


class Context(StrictModel):
    selection: Literal["current", "resume"] = "current"
    workspace_id: str | None = None
    playset_id: str | None = None
    locations: DiscoverCK3 | None = None


class Check(StrictModel):
    context_id: str
    goal: str | None = Field(default=None, max_length=2000)


class MissingMods(StrictModel):
    context_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class Remember(Check):
    notes: str | None = Field(default=None, max_length=6000)
    next_step: str | None = Field(default=None, max_length=1000)
    reviewed_issue_ids: list[str] | None = Field(default=None, max_length=100)


class KnowledgeSearch(StrictModel):
    query: str = Field(default="", max_length=500)
    game_version: str | None = Field(default=None, max_length=100)
    topic: str | None = Field(default=None, max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=20)


class KnowledgeRead(StrictModel):
    entry_id: str
    game_version: str | None = Field(default=None, max_length=100)


class Workspace(StrictModel):
    workspace_id: str


class Index(Workspace):
    index_id: str


class Issues(Index):
    query: str = Field(default="", max_length=500)
    area: str | None = None
    include_equal: bool = False
    include_internal: bool = False
    include_metadata: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class Inspect(Index):
    issue_id: str
    source_number: int | None = Field(default=None, ge=0)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8192, ge=1, le=8192)


class SourceSearch(Index):
    query: str = Field(max_length=500)
    kind: Literal["text", "path", "symbol"] = "text"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    path_prefix: str | None = Field(default=None, max_length=500)
    owner: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def nonempty_text_or_symbol(self):
        if self.kind != "path" and not self.query:
            raise ValueError("Text and symbol searches need a nonempty query; an empty path query lists captured paths.")
        return self


class SourceRead(Index):
    source_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8192, ge=1, le=8192)


class Related(Index):
    target: str = Field(min_length=1, max_length=500)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)


class DefinitionContributions(Index):
    target: str = Field(min_length=1, max_length=4096)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    path_prefix: str | None = Field(default=None, max_length=500)
    owner: str | None = Field(default=None, max_length=500)
    continuation: str | None = Field(default=None, max_length=4096)


class Brief(Index):
    issue_id: str


class SourceExcerpt(StrictModel):
    source_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8192, ge=1, le=8192)


class SourcesRead(Index):
    requests: list[SourceExcerpt] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def byte_budget(self):
        if sum(item.limit for item in self.requests) > 65536:
            raise ValueError("Request at most 65536 source bytes in one batch.")
        return self


ReviewState = Literal["pending", "in_progress", "explained_from_files", "needs_user_choice",
                      "needs_game_test", "blocked_by_missing_information"]


class Concerns(Index):
    topic: str | None = Field(default=None, max_length=200)
    state: ReviewState | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class ConcernEvidence(Index):
    concern_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4, ge=1, le=4)
    continuation: str | None = Field(default=None, max_length=4096)


class Review(Index):
    concern_id: str
    state: ReviewState
    explanation: str = Field(default="", max_length=6000)
    player_impact: str = Field(default="", max_length=2000)
    evidence_refs: list[dict] = Field(default_factory=list, max_length=4096)
    uncertainties: list[str] = Field(default_factory=list, max_length=50)
    next_step: str = Field(default="", max_length=1000)
    user_question: str | None = Field(default=None, max_length=1000)
    unresolved_reason: Literal["missing_source", "unsupported_content", "excluded_content", "missing_documentation",
                               "unresolved_dependency", "unknown_game_behavior"] | None = None
    expected_revision: int = Field(default=0, ge=0)


class InvestigationReport(Index):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)
    missing_offset: int = Field(default=0, ge=0)


class CandidateInspection(StrictModel):
    context_id: str
    candidate_id: str


class VersionPreference(StrictModel):
    context_id: str
    preference: Literal["preserve_current", "open_to_change", "undecided"]


class Proposal(Workspace):
    proposal_id: str


class Challenge(Proposal):
    purpose: Literal["choose", "test", "enable", "undo"] = "choose"


class Answer(StrictModel):
    answer: dict
    signature: str = Field(min_length=64, max_length=64)


MODELS = {"status": StrictModel, "release": StrictModel, "session": Workspace, "issues": Issues, "inspect": Inspect,
          "proposal": Proposal, "challenge": Challenge, "answer": Answer,
          "apply": Proposal, "test": Proposal, "enable": Proposal, "undo": Proposal,
          "context": Context, "check": Check, "remember": Remember, "capabilities": StrictModel, "missing_mods": MissingMods,
          "search_knowledge": KnowledgeSearch, "read_knowledge": KnowledgeRead,
          "search_sources": SourceSearch, "read_source": SourceRead, "related_definitions": Related,
          "definition_contributions": DefinitionContributions, "brief": Brief,
          "read_sources": SourcesRead, "concerns": Concerns, "concern_evidence": ConcernEvidence,
          "record_review": Review, "investigation_report": InvestigationReport,
          "missing_candidates": MissingMods, "inspect_candidate": CandidateInspection,
          "version_preference": VersionPreference}


class AgentRequest(StrictModel):
    operation: Literal["status", "release", "session", "issues", "inspect", "proposal", "challenge", "answer",
                       "apply", "test", "enable", "undo", "context", "check", "remember", "capabilities",
                       "search_knowledge", "read_knowledge", "search_sources", "read_source", "related_definitions",
                       "definition_contributions", "brief", "missing_mods",
                       "read_sources", "concerns", "concern_evidence", "record_review", "investigation_report",
                       "missing_candidates", "inspect_candidate", "version_preference"]
    payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self):
        model = MODELS[self.operation].model_validate(self.payload)
        self.payload = model.model_dump()
        if self.operation == "context" and model.locations is not None:
            self.payload["locations"] = model.locations.model_dump(exclude_unset=True)
        return self
