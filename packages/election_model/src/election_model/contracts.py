"""Transport-independent, bounded aggregate election research contracts.

Margins are Democratic minus Republican percentage points of reported vote share.
No voter-level records or demographic inference are accepted by these contracts.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def json_dates(cls, value):
        # MCP supplies decoded JSON objects; parse only declared ISO date fields.
        # All other fields retain strict Pydantic validation.
        if isinstance(value, dict):
            value = dict(value)
            for name, field in cls.model_fields.items():
                item = value.get(name)
                allowed = (field.annotation, *get_args(field.annotation))
                if isinstance(item, str) and date in allowed:
                    parsed = date.fromisoformat(item)
                    if parsed.isoformat() != item:
                        raise ValueError("Dates must use YYYY-MM-DD.")
                    value[name] = parsed
                elif isinstance(item, str) and datetime in allowed:
                    value[name] = datetime.fromisoformat(item.replace("Z", "+00:00"))
        return value


class Poll(Contract):
    poll_id: str = Field(min_length=1, max_length=120)
    race_id: str = Field(min_length=1, max_length=100)
    pollster: str = Field(min_length=1, max_length=180)
    start_date: date
    end_date: date
    available_date: date
    sample_size: int = Field(ge=1, le=1000000)
    sample_size_verified: bool = False
    availability_basis: Literal["publisher_release", "provider_created_at_proxy", "user_supplied"] = "user_supplied"
    source_url: str | None = Field(default=None, max_length=2000)
    internal: bool | None = None
    partisan: bool | None = None
    sponsors: list[str] = Field(default_factory=list, max_length=20)
    dem_pct: float = Field(ge=0, le=100)
    rep_pct: float = Field(ge=0, le=100)
    population: Literal["lv", "rv", "adults", "unknown"] = "unknown"
    mode: str = Field(default="unknown", max_length=120)
    source_id: str = Field(min_length=1, max_length=100)
    overlap_group: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def coherent(self):
        if self.start_date > self.end_date or self.available_date < self.end_date:
            raise ValueError("Poll availability must follow its field dates.")
        if self.dem_pct + self.rep_pct > 100.000001:
            raise ValueError("Democratic and Republican shares cannot sum above 100.")
        return self


class Race(Contract):
    race_id: str = Field(min_length=1, max_length=100)
    chamber: Literal["house", "senate"]
    state: str = Field(pattern=r"^[A-Z]{2}$")
    election_date: date
    cycle: int = Field(ge=1990, le=2100)
    map_vintage: str | None = Field(default=None, max_length=180)

    @model_validator(mode="after")
    def coherent(self):
        if self.cycle != self.election_date.year:
            raise ValueError("Race cycle must equal its election year.")
        if self.chamber == "house" and not self.map_vintage:
            raise ValueError("House races require an explicit district map vintage.")
        return self


class HistoricalRace(Contract):
    race_id: str = Field(min_length=1, max_length=100)
    chamber: Literal["house", "senate"]
    state: str = Field(pattern=r"^[A-Z]{2}$")
    cycle: int = Field(ge=1990, le=2100)
    election_date: date
    as_of: date
    poll_margin: float = Field(ge=-100, le=100)
    actual_margin: float = Field(ge=-100, le=100)
    poll_count: int = Field(ge=1, le=10000)
    pollster_count: int = Field(ge=1, le=1000)
    source_id: str = Field(min_length=1, max_length=100)
    outcome_available_date: date

    @model_validator(mode="after")
    def coherent(self):
        if self.cycle != self.election_date.year:
            raise ValueError("Historical race cycle must match its election year.")
        if self.as_of >= self.election_date:
            raise ValueError("Historical predictions must precede election day.")
        if self.outcome_available_date <= self.election_date:
            raise ValueError("Outcomes must become available after election day.")
        return self


class ModelConfig(Contract):
    as_of: date
    seed: int = Field(default=20261101, ge=0, le=2**32 - 1)
    draws: int = Field(default=5000, ge=1000, le=20000)
    half_life_days: float = Field(default=14.0, gt=0, le=180)
    max_poll_age_days: int = Field(default=45, ge=1, le=180)
    pollster_cap: float = Field(default=0.35, gt=0, le=1)
    minimum_race_sd: float = Field(default=4.0, gt=0, le=30)
    national_sd: float = Field(default=2.0, ge=0, le=20)
    state_sd: float = Field(default=1.0, ge=0, le=20)
    bias_shrinkage_cycles: float = Field(default=3.0, ge=0, le=100)
    reversal_shift: float = Field(default=3.0, ge=-20, le=20)


class ForecastRequest(Contract):
    dataset_id: Literal["inline", "us-2026-11-01", "votehub-2026-11-01"] = "inline"
    race_ids: list[str] = Field(default_factory=list, max_length=10)
    exclude_partisan: bool = False
    exclude_internal: bool = False
    races: list[Race] = Field(default_factory=list, max_length=10)
    polls: list[Poll] = Field(default_factory=list, max_length=2000)
    history: list[HistoricalRace] = Field(default_factory=list, max_length=3000)
    config: ModelConfig
    synthetic: bool = False

    @model_validator(mode="after")
    def unique_records(self):
        for records, key in ((self.races, "race_id"), (self.polls, "poll_id"),
                             (self.history, "race_id")):
            keys = [getattr(record, key) for record in records]
            if len(keys) != len(set(keys)):
                raise ValueError(f"Duplicate {key} records are not allowed.")
        if self.dataset_id != "inline" and (self.races or self.polls):
            raise ValueError("Bundled dataset selection cannot include inline races or polls.")
        if self.dataset_id == "inline" and self.race_ids:
            raise ValueError("Race ID selection applies only to a bundled dataset.")
        if len(self.race_ids) != len(set(self.race_ids)):
            raise ValueError("Selected race IDs must be unique.")
        known = {r.race_id for r in self.races}
        if any(p.race_id not in known for p in self.polls):
            raise ValueError("Every poll must identify a requested race.")
        if any(r.election_date <= self.config.as_of for r in self.races):
            raise ValueError("Forecast cutoff must precede every target election.")
        return self


class PollAggregate(Contract):
    latest_poll_age_days: int | None = None
    newest_poll_end_date: date | None = None
    oldest_poll_end_date: date | None = None
    race_id: str
    poll_margin: float | None
    sd_sampling: float | None
    poll_count: int
    pollster_count: int
    effective_pollsters: float
    warnings: list[str] = Field(default_factory=list)


class Calibration(Contract):
    chamber: Literal["house", "senate"]
    training_cycles: list[int]
    race_count: int
    cycle_count: int
    estimated_bias: float
    residual_sd: float
    shrinkage_weight: float
    warnings: list[str] = Field(default_factory=list)


class ScenarioForecast(Contract):
    race_id: str
    scenario: Literal["baseline", "historical", "reversal"]
    poll_margin: float
    adjustment: float
    mean_margin: float
    sd: float
    dem_win_probability: float
    p05: float
    p50: float
    p95: float
    poll_count: int
    pollster_count: int
    effective_pollsters: float
    warnings: list[str] = Field(default_factory=list)


class ForecastReport(Contract):
    recommended_scenario: Literal["baseline"] = "baseline"
    model_version: str = "0.1.0"
    input_sha256: str | None = None
    configuration: ModelConfig | None = None
    data_origin: Literal["bundled_public", "user_supplied_unverified", "synthetic"] = "user_supplied_unverified"
    as_of: date
    synthetic: bool
    status: Literal["ready", "missing_data", "partial"]
    forecasts: list[ScenarioForecast] = Field(default_factory=list, max_length=30)
    aggregates: list[PollAggregate] = Field(default_factory=list, max_length=10)
    calibrations: list[Calibration] = Field(default_factory=list, max_length=2)
    unmodeled_races: list[str] = Field(default_factory=list, max_length=10)
    requested_race_count: int = 0
    modeled_race_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class BacktestRequest(Contract):
    history: list[HistoricalRace] = Field(default_factory=list, max_length=3000)
    as_of: date = date(2026, 11, 1)
    min_training_cycles: int = Field(default=2, ge=1, le=6)
    synthetic: bool = False

    @model_validator(mode="after")
    def unique_records(self):
        keys = [row.race_id for row in self.history]
        if len(keys) != len(set(keys)):
            raise ValueError("Historical race records must have unique IDs.")
        return self


class EvaluationMetrics(Contract):
    race_count: int
    mae: float
    rmse: float
    brier: float
    interval90_coverage: float
    mean_interval90_width: float


class BacktestFold(Contract):
    cycle: int
    chamber: Literal["house", "senate"]
    training_cycles: list[int]
    rows: int
    baseline: EvaluationMetrics
    historical: EvaluationMetrics


class BacktestReport(Contract):
    model_version: str = "0.1.0"
    input_sha256: str | None = None
    data_origin: Literal["bundled_public", "user_supplied_unverified", "synthetic"] = "user_supplied_unverified"
    as_of: date
    synthetic: bool
    folds: list[BacktestFold] = Field(default_factory=list, max_length=100)
    warnings: list[str] = Field(default_factory=list)
    history_count: int = 0
    evaluated_race_count: int = 0


class SourceRecord(Contract):
    source_id: str
    title: str
    publisher: str
    url: str
    retrieved_at: datetime
    published_date: date | None = None
    sha256: str | None = None
    coverage: str
    limitations: list[str] = Field(default_factory=list)


class ResearchFinding(Contract):
    facet: str
    finding: str
    model_treatment: str
    source_ids: list[str]
    status: Literal["observed", "methodological", "missing", "hypothetical"]


class ContextObservation(Contract):
    source_id: str
    metric: str
    value: float | None = None
    unit: str | None = None
    dem_pct: float | None = None
    rep_pct: float | None = None
    population: Literal["lv", "rv", "adults", "unknown"] | None = None
    sample_size: int | None = None
    field_start: date | None = None
    field_end: date | None = None
    precision: str | None = None


class ResearchStatus(Contract):
    as_of: date
    status: Literal["research_only_missing_current_data", "partial_race_coverage"]
    historical_races: int
    historical_cycles: list[int]
    current_races: int
    current_polls: int
    sources: list[SourceRecord] = Field(max_length=40)
    findings: list[ResearchFinding] = Field(max_length=20)
    limitations: list[str]
    context_observations: list[ContextObservation] = Field(default_factory=list, max_length=30)
    aggregator_races: int = 0
    aggregator_polls: int = 0
    aggregator_race_ids: list[str] = Field(default_factory=list, max_length=200)
    aggregator_rejections: int = 0
