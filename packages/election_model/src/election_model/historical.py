"""Read the public FiveThirtyEight archive without importing retrospective ratings.

This archive is an exploratory historical dataset, not a vintage forecast tape:
its dates are median field dates and its candidates were selected retrospectively.
Those limitations are carried into every normalized snapshot.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import io
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .contracts import HistoricalRace


RAW_URL = "https://raw.githubusercontent.com/fivethirtyeight/data/master/pollster-ratings/raw_polls.csv"
ARCHIVE_LIMITATIONS = [
    "Archive available now is not a vintage dataset; historical publication dates are unknown.",
    "Observation dates are median field dates, not publication dates or field end dates.",
    "Candidates are the retrospectively selected top two election finishers; selection bias remains.",
    "The provider has already imputed some sample sizes without a row-level imputation flag.",
    "Historical actual margins are the archive's values; independent official-return reconciliation is not supplied.",
    "This snapshot has no 2024 or 2026 election observations.",
]


class ArchivePoll(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    poll_id: str
    question_id: str
    race_id: str
    source_race_id: str
    cycle: int
    chamber: Literal["house", "senate"]
    state: str
    geography: str
    election_date: date
    median_field_date: date
    publication_date: None = None
    pollster: str
    mode: str
    partisan: str | None
    dem_pct: float = Field(ge=0, le=100)
    rep_pct: float = Field(ge=0, le=100)
    sample_size: float | None = Field(default=None, gt=0)
    actual_margin: float = Field(ge=-100, le=100)


class ArchiveData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: str = "1.0"
    kind: Literal["retrospective_archive"] = "retrospective_archive"
    source_url: str = RAW_URL
    source_sha256: str
    source_bytes: int
    retrieved_at: datetime
    license: str = "CC-BY-4.0"
    attribution: str = "FiveThirtyEight data/pollster-ratings"
    raw_rows: int
    raw_cycles: list[int]
    selected_rows: int
    selected_races: int
    selected_cycles: list[int]
    excluded_counts: dict[str, int]
    limitations: list[str]
    polls: list[ArchivePoll]


def _finite(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Archive numerical value is not finite.")
    return parsed


def november_general_date(year: int) -> date:
    """The US federal general election date: Tuesday after the first Monday."""
    november_first = date(year, 11, 1)
    return november_first + timedelta(days=(7 - november_first.weekday()) % 7 + 1)


def read_538_archive(raw: bytes, *, retrieved_at: datetime, midterms_only: bool = True) -> ArchiveData:
    """Normalize House/Senate D/R rows, retaining source identifiers and date gaps.

    Bad/inconsistent eligible rows fail closed instead of being silently dropped.
    The 8 MB cap allows this archived file and keeps untrusted parsing bounded.
    No mode, pollster grade, current rating or retrospectively derived weight is used.
    """
    if not raw or len(raw) > 8 * 1024 * 1024:
        raise ValueError("Historical source must contain 1 to 8 MiB of CSV bytes.")
    if retrieved_at.tzinfo is None:
        raise ValueError("retrieved_at must include a timezone.")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    required = {"poll_id", "question_id", "race_id", "cycle", "location", "type_simple", "polldate",
                "electiondate", "pollster", "methodology", "partisan", "samplesize", "cand1_party",
                "cand2_party", "cand1_pct", "cand2_pct", "margin_poll", "margin_actual"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("Historical source is missing required columns.")
    polls: list[ArchivePoll] = []
    raw_rows = 0
    raw_cycles: set[int] = set()
    excluded: dict[str, int] = {}
    seen: set[tuple] = set()
    identities: set[tuple] = set()
    actuals: dict[str, tuple] = {}

    def exclude(reason: str) -> None:
        excluded[reason] = excluded.get(reason, 0) + 1

    for line_number, row in enumerate(reader, start=2):
        raw_rows += 1
        if raw_rows > 50_000:
            raise ValueError("Historical source exceeds 50,000 rows.")
        try:
            cycle = int(row["cycle"])
            raw_cycles.add(cycle)
            if row["type_simple"] not in {"House-G", "Sen-G"}:
                exclude("other_race_type")
                continue
            if {row["cand1_party"], row["cand2_party"]} != {"DEM", "REP"}:
                exclude("not_democrat_republican_top_two")
                continue
            if midterms_only and cycle % 4 != 2:
                exclude("not_midterm_cycle")
                continue
            median = date.fromisoformat(row["polldate"])
            election = date.fromisoformat(row["electiondate"])
            if midterms_only and election != november_general_date(cycle):
                exclude("not_november_general_date")
                continue
            if median > election:
                exclude("field_date_after_election")
                continue
            if row["margin_actual"] in {"", "NA"}:
                exclude("missing_actual_margin")
                continue
            # Race IDs distinguish regular/special races at the same place/date.
            source_race_id = row["race_id"]
            if not source_race_id or not row["poll_id"] or not row["question_id"]:
                raise ValueError("Missing poll/question/race identity.")
            race_id = f"538-{cycle}-{source_race_id}"
            dem_first = row["cand1_party"] == "DEM"
            dem = _finite(row["cand1_pct"] if dem_first else row["cand2_pct"])
            rep = _finite(row["cand2_pct"] if dem_first else row["cand1_pct"])
            sign = 1 if dem_first else -1
            actual = sign * _finite(row["margin_actual"])
            reported = sign * _finite(row["margin_poll"])
            if abs(dem - rep - reported) > 1e-6:
                raise ValueError("Reported poll margin disagrees with candidate shares.")
            if dem + rep > 100.000001:
                exclude("candidate_shares_exceed_100")
                continue
            sample = None if row["samplesize"] in {"", "NA"} else _finite(row["samplesize"])
            key = (race_id, row["poll_id"], row["question_id"])
            record = ArchivePoll(
                poll_id=row["poll_id"], question_id=row["question_id"], race_id=race_id,
                source_race_id=source_race_id, cycle=cycle,
                chamber="house" if row["type_simple"] == "House-G" else "senate",
                state=row["location"].split("-")[0], geography=row["location"], election_date=election,
                median_field_date=median, pollster=row["pollster"], mode=row["methodology"],
                partisan=None if row["partisan"] in {"", "NA"} else row["partisan"],
                dem_pct=dem, rep_pct=rep, sample_size=sample, actual_margin=actual)
            exact = tuple(record.model_dump(mode="json").items())
            if exact in seen:
                exclude("exact_duplicate")
                continue
            seen.add(exact)
            # Multiple questions within one survey remain explicit for clustering.
            if key in identities:
                raise ValueError("Duplicate poll/question identity with different content.")
            identities.add(key)
            identity = (record.election_date, record.cycle, record.chamber, record.geography, record.actual_margin)
            if race_id in actuals and actuals[race_id] != identity:
                raise ValueError("Race identity has inconsistent metadata or actual margin.")
            actuals[race_id] = identity
            polls.append(record)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Invalid historical CSV row {line_number}: {exc}") from exc

    return ArchiveData(
        source_sha256=hashlib.sha256(raw).hexdigest(), source_bytes=len(raw),
        retrieved_at=retrieved_at.astimezone(timezone.utc), raw_rows=raw_rows, raw_cycles=sorted(raw_cycles),
        selected_rows=len(polls), selected_races=len(actuals), selected_cycles=sorted({p.cycle for p in polls}),
        excluded_counts=excluded, limitations=ARCHIVE_LIMITATIONS.copy(), polls=polls)


def aggregate_history(archive: ArchiveData, *, publication_lag_days: int = 7,
                      horizon_days: int = 2) -> tuple[list[HistoricalRace], dict]:
    """Build one bounded-model record per race using explicit archival proxies.

    The constant sample size deliberately avoids retrospective provider imputation.
    Publication and outcome availability lags are assumptions, not source facts.
    Only forward-cycle evaluation is justified; exact historical availability is
    not recovered by these assumptions.
    """
    from .contracts import ModelConfig, Poll, Race
    from .model import aggregate_polls

    if not 0 <= publication_lag_days <= 30 or not 1 <= horizon_days <= 90:
        raise ValueError("Publication lag must be 0..30 days and horizon 1..90 days.")
    grouped: dict[str, list[ArchivePoll]] = {}
    for poll in archive.polls:
        grouped.setdefault(poll.race_id, []).append(poll)
    history: list[HistoricalRace] = []
    unmodeled = 0
    normalized_poll_count = 0
    for race_id in sorted(grouped):
        rows = grouped[race_id]
        reference = rows[0]
        cutoff = reference.election_date - timedelta(days=horizon_days)
        race = Race(race_id=race_id, chamber=reference.chamber, state=reference.state,
                    election_date=reference.election_date, cycle=reference.cycle,
                    map_vintage=f"archive-{reference.cycle}-unknown-map" if reference.chamber == "house" else None)
        polls = [Poll(
            poll_id=f"538-{p.poll_id}-{p.question_id}", race_id=race_id, pollster=p.pollster,
            start_date=p.median_field_date, end_date=p.median_field_date,
            available_date=p.median_field_date + timedelta(days=publication_lag_days),
            sample_size=1000, dem_pct=p.dem_pct, rep_pct=p.rep_pct,
            mode=p.mode, source_id="538-pollster-ratings-2023", overlap_group=f"538-{p.poll_id}")
            for p in rows]
        aggregate = aggregate_polls(race, polls, ModelConfig(as_of=cutoff))
        if aggregate.poll_margin is None:
            unmodeled += 1
            continue
        normalized_poll_count += aggregate.poll_count
        history.append(HistoricalRace(
            race_id=race_id, chamber=reference.chamber, state=reference.state, cycle=reference.cycle,
            election_date=reference.election_date, as_of=cutoff, poll_margin=round(aggregate.poll_margin, 12),
            actual_margin=reference.actual_margin, poll_count=aggregate.poll_count,
            pollster_count=aggregate.pollster_count, source_id="538-pollster-ratings-2023",
            outcome_available_date=reference.election_date + timedelta(days=30)))
    metadata = {
        "schema_version": "2.0", "kind": "retrospective_archive_evaluation",
        "source_sha256": archive.source_sha256, "source_url": archive.source_url,
        "retrieved_at": archive.retrieved_at.isoformat(), "license": archive.license,
        "attribution": archive.attribution, "raw_rows": archive.raw_rows,
        "raw_cycles": archive.raw_cycles, "selected_rows": archive.selected_rows,
        "selected_races": archive.selected_races, "selected_cycles": archive.selected_cycles,
        "excluded_counts": archive.excluded_counts, "modeled_races": len(history),
        "races_without_eligible_poll": unmodeled, "eligible_poll_count": normalized_poll_count,
        "normalization": {
            "elections": "D/R top-two House/Senate, November federal election date in midterm cycles",
            "poll_dates": "median field date used as both start/end proxy",
            "publication_lag_days": publication_lag_days, "forecast_horizon_days": horizon_days,
            "outcome_lag_days": 30, "sample_size": 1000,
            "poll_margin_decimal_places": 12,
            "numerical_method": "recency weights relative to newest eligible poll; margins rounded to 12 decimals",
            "sample_size_reason": "constant avoids retrospective source imputation; no design effect is known",
            "model_defaults": ModelConfig(as_of=date(2026, 11, 1)).model_dump(mode="json"),
        },
        "limitations": archive.limitations + [
            "Publication lag and outcome availability lag are declared proxies, not observed release dates.",
            "Archive map vintage is unknown; no district baseline or cross-map transfer is modeled.",
            "November specials sharing the federal general-election date may remain as distinct race IDs.",
            "Historical selection favors polled competitive D/R races and is not a representative chamber slate.",
        ],
    }
    return history, metadata
