"""Bounded QA for licensed public VoteHub polls, without guessing candidate parties.

The API does not identify parties per answer or document ``created_at`` as the
original release date. This adapter therefore separates candidate-verified rows
from source-reviewed model inputs and retains every rejection reason.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
import hashlib
import json
import re
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Poll


class VoteHubAnswer(Contract):
    choice: str = Field(min_length=1, max_length=180)
    pct: float = Field(ge=0, le=100)


class VoteHubRecord(Contract):
    id: str = Field(min_length=1, max_length=120)
    poll_type: str = Field(max_length=60)
    sample_size: int | str | None
    population: str = Field(max_length=30)
    url: str = Field(max_length=2048)
    created_at: date
    start_date: date
    end_date: date
    pollster: str = Field(min_length=1, max_length=180)
    answers: list[VoteHubAnswer] = Field(max_length=30)
    seat_name: str | None = Field(max_length=180)
    sponsors: list[str] = Field(max_length=20)
    internal: bool
    partisan: str | None = Field(max_length=30)
    subject: str = Field(max_length=180)

    @model_validator(mode="after")
    def coherent(self):
        if len({answer.choice for answer in self.answers}) != len(self.answers):
            raise ValueError("Answer choices must be unique within a question.")
        return self


class CandidateEvidence(Contract):
    """A reviewed exact API answer label scoped to one election contest."""
    subject: str = Field(min_length=1, max_length=180)
    choice: str = Field(min_length=1, max_length=180)
    race_id: str = Field(min_length=1, max_length=100)
    chamber: Literal["house", "senate"]
    state: str = Field(pattern=r"^[A-Z]{2}$")
    party: str = Field(min_length=1, max_length=30)
    candidate_id: str = Field(min_length=1, max_length=120)
    source_name: str = Field(min_length=1, max_length=250)
    evidence_date: date
    source_url: str = Field(min_length=1, max_length=2048)
    match_basis: Literal["exact_full_name", "reviewed_primary_alias"] = "exact_full_name"


class PrimaryPollReview(Contract):
    """Source facts checked independently, with corrected sample/population values."""
    poll_id: str
    available_date: date
    sample_size: int = Field(ge=1, le=1000000)
    population: Literal["lv", "rv", "adults", "unknown"]
    mode: str = Field(default="unknown", max_length=120)
    dem_pct: float = Field(ge=0, le=100)
    rep_pct: float = Field(ge=0, le=100)
    source_url: str = Field(min_length=1, max_length=2048)
    notes: list[str] = Field(default_factory=list, max_length=20)


class ExistingSurvey(Contract):
    """A previously retained primary survey to prevent duplicate API variants."""
    race_id: str
    pollster: str
    start_date: date
    end_date: date
    poll_id: str


class VoteHubAuditRow(Contract):
    poll_id: str
    subject: str
    pollster: str
    source_url: str
    status: Literal["reviewed", "candidate_verified", "rejected"]
    race_id: str | None = None
    reasons: list[str] = Field(default_factory=list, max_length=20)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    availability_basis: Literal["primary_release_verified", "provider_created_at_proxy", "unavailable"]
    internal: bool
    partisan: str | None = None
    sponsors: list[str] = Field(default_factory=list, max_length=30)
    dem_pct: float | None = None
    rep_pct: float | None = None
    sample_size_reported: int | None = None
    population_reported: str


class VoteHubAudit(Contract):
    schema_version: str = "1.0"
    as_of: date
    source_sha256: str
    source_records: int
    target_records: int
    reviewed_polls: list[Poll] = Field(default_factory=list, max_length=5000)
    candidate_verified_polls: list[Poll] = Field(default_factory=list, max_length=5000)
    rows: list[VoteHubAuditRow] = Field(max_length=5000)
    rejection_counts: dict[str, int]
    limitations: list[str]


def _pollster_key(name: str, aliases: dict[str, str]) -> str:
    """Only caller-reviewed aliases merge otherwise distinct firm names."""
    return re.sub(r"[^a-z0-9]", "", aliases.get(name, name).casefold())


def _reported_sample(value: int | str | None) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = int(value)
        return result if 1 <= result <= 1000000 and str(result) == str(value).strip() else None
    except (ValueError, TypeError):
        return None


def audit_votehub(raw: bytes, *, as_of: date, candidate_evidence: list[CandidateEvidence],
                  primary_reviews: list[PrimaryPollReview] | None = None,
                  existing_surveys: list[ExistingSurvey] | None = None,
                  pollster_aliases: dict[str, str] | None = None) -> VoteHubAudit:
    """Validate records and classify their suitability with exhaustive QA reasons.

    Candidate-only records may support an explicitly labeled aggregator sensitivity
    study; they are never silently upgraded to source-reviewed model inputs. All
    same-firm/contest/field-window duplicates are suppressed unless a primary review
    selects exactly one question. Existing primary surveys take precedence.
    """
    if not raw or len(raw) > 4 * 1024 * 1024:
        raise ValueError("VoteHub snapshot must contain 1 byte to 4 MiB.")
    data = json.loads(raw)
    if not isinstance(data, list) or len(data) > 5000:
        raise ValueError("VoteHub snapshot must be an array with at most 5,000 records.")
    records = [VoteHubRecord.model_validate_json(json.dumps(row)) for row in data]
    if len({record.id for record in records}) != len(records):
        raise ValueError("VoteHub poll IDs must be unique.")
    evidence: dict[tuple[str, str], CandidateEvidence] = {}
    for candidate in candidate_evidence:
        key = candidate.subject, candidate.choice
        if key in evidence:
            raise ValueError("Candidate evidence must have unique subject/answer labels.")
        evidence[key] = candidate
    primary_reviews = primary_reviews or []
    if len({item.poll_id for item in primary_reviews}) != len(primary_reviews):
        raise ValueError("Primary source reviews must have unique poll IDs.")
    reviews = {item.poll_id: item for item in primary_reviews}
    aliases = pollster_aliases or {}
    existing = {(item.race_id, _pollster_key(item.pollster, aliases), item.start_date, item.end_date)
                for item in existing_surveys or []}
    variants: dict[tuple, list[str]] = defaultdict(list)
    target_records = [record for record in records if record.poll_type in {"us-senator", "us-representative"}]
    for record in target_records:
        variants[(record.subject, _pollster_key(record.pollster, aliases), record.start_date,
                  record.end_date)].append(record.id)
    rows: list[VoteHubAuditRow] = []
    reviewed_polls: list[Poll] = []
    candidate_polls: list[Poll] = []
    reject_counts: Counter[str] = Counter()
    ignored_answers = {"Undecided", "Unsure", "Other", "Someone else", "Don't know", "Refused"}
    for record in records:
        reasons: list[str] = []
        warnings: list[str] = []
        candidates: list[tuple[VoteHubAnswer, CandidateEvidence]] = []
        race_id: str | None = None
        review = reviews.get(record.id)
        status: Literal["reviewed", "candidate_verified", "rejected"]
        basis: Literal["primary_release_verified", "provider_created_at_proxy", "unavailable"]
        sample = _reported_sample(record.sample_size)
        if record.poll_type not in {"us-senator", "us-representative"}:
            reasons.append("unsupported_poll_type")
        if record.start_date > record.end_date or record.created_at < record.end_date:
            reasons.append("invalid_field_or_provider_dates")
        if record.end_date > as_of or record.created_at > as_of:
            reasons.append("not_available_by_cutoff")
        if record.internal:
            reasons.append("internal_poll_excluded")
        if record.partisan is not None:
            reasons.append("partisan_poll_excluded")
        if sample is None:
            warnings.append("No valid reported sample; any research sensitivity uses placeholder n=1000.")
        if record.population not in {"lv", "rv", "a"}:
            reasons.append("unsupported_population")
        if record.poll_type in {"us-senator", "us-representative"}:
            if record.subject in {"2026 Alaska", "2026 AK-01"}:
                reasons.append("ranked_choice_final_outcome_unsupported")
            for answer in record.answers:
                if answer.choice in ignored_answers:
                    continue
                item = evidence.get((record.subject, answer.choice))
                if item is None:
                    reasons.append("candidate_party_not_verified")
                    continue
                if item.evidence_date > as_of:
                    reasons.append("candidate_evidence_after_cutoff")
                expected = "senate" if record.poll_type == "us-senator" else "house"
                if item.chamber != expected:
                    reasons.append("candidate_evidence_contest_mismatch")
                candidates.append((answer, item))
            race_ids = {item.race_id for _, item in candidates}
            if len(race_ids) == 1:
                race_id = next(iter(race_ids))
            elif len(race_ids) > 1:
                reasons.append("candidate_evidence_contest_mismatch")
        democrats = [(answer, item) for answer, item in candidates if item.party == "DEM"]
        republicans = [(answer, item) for answer, item in candidates if item.party == "REP"]
        if record.poll_type in {"us-senator", "us-representative"} and (len(democrats) != 1 or len(republicans) != 1):
            reasons.append("requires_one_verified_democrat_and_republican")
        survey = (record.subject, _pollster_key(record.pollster, aliases), record.start_date, record.end_date)
        if len(variants.get(survey, [])) > 1:
            selected = [poll_id for poll_id in variants[survey] if poll_id in reviews]
            if selected != [record.id]:
                reasons.append("ambiguous_same_survey_variant")
        if (race_id, _pollster_key(record.pollster, aliases), record.start_date, record.end_date) in existing:
            reasons.append("overlaps_existing_primary_survey")
        dem = democrats[0][0].pct if len(democrats) == 1 else None
        rep = republicans[0][0].pct if len(republicans) == 1 else None
        if dem is not None and rep is not None and dem + rep > 100.000001:
            reasons.append("candidate_shares_exceed_100")
        if review is not None and (review.available_date > as_of or review.available_date < record.end_date):
            reasons.append("invalid_primary_release_availability")
        if review is not None and (dem != review.dem_pct or rep != review.rep_pct):
            reasons.append("primary_source_values_disagree")
        if reasons:
            reject_counts.update(set(reasons))
            status = "rejected"
            basis = "unavailable"
        elif review is not None:
            status = "reviewed"
            basis = "primary_release_verified"
        else:
            status = "candidate_verified"
            basis = "provider_created_at_proxy"
            warnings.extend([
                "Provider created_at is used as an availability proxy, not a verified release date.",
                "Reported population/sample counts and numerical poll values have not been reconciled with the primary source.",
                "Mode is unavailable; no firm/mode directional adjustment is estimated.",
            ])
        if status in {"reviewed", "candidate_verified"}:
            assert race_id is not None and dem is not None and rep is not None
            population = {"a": "adults", "rv": "rv", "lv": "lv"}[record.population]
            poll_values = dict(
                poll_id=f"votehub-{record.id}", race_id=race_id,
                pollster=aliases.get(record.pollster, record.pollster), start_date=record.start_date,
                end_date=record.end_date, available_date=review.available_date if review else record.created_at,
                sample_size=review.sample_size if review else (sample or 1000),
                dem_pct=dem, rep_pct=rep, population=review.population if review else population,
                mode=review.mode if review else "unknown", source_id="votehub-2026-11-01",
                availability_basis="publisher_release" if review else "provider_created_at_proxy",
                source_url=review.source_url if review else record.url,
                internal=record.internal, partisan=record.partisan is not None, sponsors=record.sponsors,
                overlap_group=f"votehub-{hashlib.sha256(repr(survey).encode()).hexdigest()[:24]}")
            if "sample_size_verified" in Poll.model_fields:
                poll_values["sample_size_verified"] = review is not None
            poll = Poll.model_validate(poll_values)
            (reviewed_polls if review else candidate_polls).append(poll)
        rows.append(VoteHubAuditRow(
            poll_id=record.id, subject=record.subject, pollster=record.pollster, source_url=record.url,
            status=status, race_id=race_id, reasons=sorted(set(reasons)), warnings=warnings,
            availability_basis=basis, internal=record.internal, partisan=record.partisan,
            sponsors=record.sponsors, dem_pct=dem, rep_pct=rep,
            sample_size_reported=sample, population_reported=record.population))
    return VoteHubAudit(
        as_of=as_of, source_sha256=hashlib.sha256(raw).hexdigest(), source_records=len(records),
        target_records=len(target_records), reviewed_polls=reviewed_polls,
        candidate_verified_polls=candidate_polls, rows=rows,
        rejection_counts=dict(sorted(reject_counts.items())), limitations=[
            "The API has no answer-party metadata; supplied primary candidate evidence is required for exact labels.",
            "FEC candidate affiliation establishes a reported party, not ballot nomination certification.",
            "Candidate-only rows are a research sensitivity input, not primary-reviewed current forecasting data.",
            "Provider-created date is not a verified publication date; exact availability remains unobserved without a primary review.",
            "Partisan/internal polls and ambiguous duplicate survey questions are excluded under this conservative QA policy.",
            "The feed is an incomplete convenience sample of races and cannot justify chamber-control probabilities.",
        ])
