"""Pure aggregate polling research calculations, with no I/O or transport imports.

All margins and adjustments are Democratic minus Republican percentage points.
The three scenarios change a poll-error assumption; they do not identify voters,
vote switching, turnout composition, or causes of a polling error. Configured
uncertainty and correlation components are sensitivity parameters, not estimates
of a validated 2026 forecasting distribution.
"""
from __future__ import annotations

import hashlib
import math
import random
import statistics
from collections import defaultdict
from datetime import date
from typing import Literal, TypeAlias

from .contracts import (
    BacktestFold,
    BacktestReport,
    BacktestRequest,
    Calibration,
    EvaluationMetrics,
    ForecastReport,
    ForecastRequest,
    HistoricalRace,
    ModelConfig,
    Poll,
    PollAggregate,
    Race,
    ScenarioForecast,
)

Chamber: TypeAlias = Literal["house", "senate"]
Scenario: TypeAlias = Literal["baseline", "historical", "reversal"]
DataOrigin: TypeAlias = Literal["bundled_public", "user_supplied_unverified", "synthetic"]

_RESEARCH_WARNING = (
    "Research scenarios only: probabilities are not a calibrated complete election forecast. "
    "No chamber-control probability is produced; the requested race list is incomplete."
)
_UNCERTAINTY_WARNING = (
    "Uncertainty floors and national/state correlation components are configurable sensitivity assumptions, "
    "not fitted 2026 parameters. More polls do not remove common polling error."
)
_ARCHIVE_WARNING = (
    "Bundled archive evaluation is a retrospective diagnostic, not an exact as-of replay: original publication "
    "availability is proxied and the historical archive selects eventual top-two candidates. "
    "Bundled historical coverage ends in 2022; it does not establish 2026 calibration."
)


def _effective_sample_size(poll: Poll) -> int:
    """Unknown respondent counts use a disclosed numerical placeholder only."""
    return poll.sample_size if poll.sample_size_verified else 1000


def _bounded_weights(raw: list[float], cap: float) -> list[float]:
    """Redistribute excess cluster weight; a cap below 1/n is infeasible."""
    if not raw:
        return []
    limit = max(cap, 1.0 / len(raw))
    remaining = set(range(len(raw)))
    weights = [0.0] * len(raw)
    remaining_mass = 1.0
    while remaining:
        total = sum(raw[index] for index in remaining)
        proposed = {index: remaining_mass * raw[index] / total if total > 0.0
                    else remaining_mass / len(remaining) for index in remaining}
        excess = [index for index, weight in proposed.items() if weight > limit + 1e-12]
        if not excess:
            for index, weight in proposed.items():
                weights[index] = weight
            break
        for index in excess:
            weights[index] = limit
            remaining_mass -= limit
            remaining.remove(index)
    return weights


def aggregate_polls(race: Race, polls: list[Poll], config: ModelConfig) -> PollAggregate:
    """Aggregate eligible independent poll clusters with transparent conservative rules.

    A shared pollster or declared overlap group links observations. Only the latest
    observation in each connected component is retained, preferring likely-voter
    samples on a field-date tie. A missing overlap declaration cannot establish
    independence. No unobserved house, population, or mode effect is invented.
    """
    if config.as_of >= race.election_date:
        raise ValueError("Poll aggregation cutoff must precede election day.")
    warnings: list[str] = []
    seen: dict[str, Poll] = {}
    for poll in polls:
        if poll.poll_id in seen and poll != seen[poll.poll_id]:
            raise ValueError("Conflicting observations share a poll ID.")
        seen[poll.poll_id] = poll
    eligible = [poll for poll in seen.values()
                if poll.race_id == race.race_id
                and poll.end_date <= config.as_of
                and poll.available_date <= config.as_of
                and 0 <= (config.as_of - poll.end_date).days <= config.max_poll_age_days]
    if not eligible:
        return PollAggregate(race_id=race.race_id, poll_margin=None, sd_sampling=None,
                             poll_count=0, pollster_count=0, effective_pollsters=0.0,
                             warnings=["No eligible polls available by the cutoff within the recency window."])

    # Union-find handles transitive overlap, including two firms using a shared panel.
    parents = list(range(len(eligible)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    aliases: dict[tuple[str, str], int] = {}
    for index, poll in enumerate(eligible):
        keys = [("pollster", poll.pollster.casefold().strip())]
        if poll.overlap_group:
            keys.append(("overlap", poll.overlap_group))
        for key in keys:
            if key in aliases:
                parents[find(index)] = find(aliases[key])
            else:
                aliases[key] = index
    clusters: dict[int, list[Poll]] = defaultdict(list)
    for index, poll in enumerate(eligible):
        clusters[find(index)].append(poll)
    population_priority = {"lv": 3, "rv": 2, "adults": 1, "unknown": 0}
    retained = sorted((max(cluster, key=lambda poll: (
        poll.end_date, population_priority[poll.population], poll.available_date,
        _effective_sample_size(poll), poll.poll_id,
    )) for cluster in clusters.values()), key=lambda poll: poll.poll_id)
    if len(retained) < len(eligible):
        warnings.append(f"Retained {len(retained)} latest independent cluster observations from "
                        f"{len(eligible)} eligible observations; repeated/overlapping samples do not add precision.")
    if len(retained) * config.pollster_cap < 1.0 - 1e-12:
        warnings.append("Configured pollster cap is infeasible with this few clusters; equal-weight minimum applies.")
    if any(not poll.sample_size_verified for poll in retained):
        warnings.append("Unverified sample sizes use n=1000 as a numerical placeholder for weighting and "
                        "the illustrative sampling component; this is not an observed precision estimate.")
    if any(poll.availability_basis == "provider_created_at_proxy" for poll in retained):
        warnings.append("A retained poll uses provider creation time as an availability proxy; original "
                        "publisher release timing may differ.")
    # Subtract the youngest age, a cancelling common weight factor. At least one
    # cluster then stays nonzero even for an extremely short valid half-life.
    youngest_age = min((config.as_of - poll.end_date).days for poll in retained)
    if youngest_age >= 14:
        warnings.append(f"Latest retained poll is {youngest_age} days old at cutoff; no fitted late-movement "
                        "or age-based uncertainty model is available.")
    raw = [math.sqrt(_effective_sample_size(poll)) * 2 ** (-((config.as_of - poll.end_date).days - youngest_age) /
                                                   config.half_life_days) for poll in retained]
    weights = _bounded_weights(raw, config.pollster_cap)
    margin = sum(weight * (poll.dem_pct - poll.rep_pct) for poll, weight in zip(retained, weights))
    # Var(D-R) for a multinomial simple random sample. This is a lower-bound
    # sampling calculation when n is verified, not the total poll error
    # (weights/designs are unknown). Otherwise it is a disclosed illustration.
    variances = [10000.0 * max(0.0, (poll.dem_pct + poll.rep_pct) / 100.0 -
                               ((poll.dem_pct - poll.rep_pct) / 100.0) ** 2) / _effective_sample_size(poll)
                 for poll in retained]
    sampling = math.sqrt(sum(weight ** 2 * variance for weight, variance in zip(weights, variances)))
    return PollAggregate(
        race_id=race.race_id, poll_margin=margin, sd_sampling=sampling,
        poll_count=len(retained), pollster_count=len({poll.pollster.casefold().strip() for poll in retained}),
        effective_pollsters=1.0 / sum(weight ** 2 for weight in weights), warnings=warnings,
        latest_poll_age_days=youngest_age, newest_poll_end_date=max(poll.end_date for poll in retained),
        oldest_poll_end_date=min(poll.end_date for poll in retained),
    )


def _fit(history: list[HistoricalRace], chamber: Chamber, config: ModelConfig) -> Calibration:
    """Fit signed errors with equal cycle weight, rather than equal race weight."""
    by_cycle: dict[int, list[float]] = defaultdict(list)
    for row in history:
        if row.chamber == chamber:
            by_cycle[row.cycle].append(row.actual_margin - row.poll_margin)
    cycles = sorted(by_cycle)
    warnings: list[str] = []
    if not cycles:
        warnings.append("No eligible prior-cycle history; historical adjustment falls back to zero.")
        return Calibration(chamber=chamber, training_cycles=[], race_count=0, cycle_count=0,
                           estimated_bias=0.0, residual_sd=config.minimum_race_sd,
                           shrinkage_weight=0.0, warnings=warnings)
    means = [statistics.mean(by_cycle[cycle]) for cycle in cycles]
    raw_mean = statistics.mean(means)
    shrinkage = len(cycles) / (len(cycles) + config.bias_shrinkage_cycles)
    within_variance = statistics.mean(statistics.pvariance(by_cycle[cycle]) for cycle in cycles)
    between_variance = statistics.variance(means) if len(means) > 1 else 0.0
    residual_sd = math.sqrt(within_variance + between_variance)
    if len(cycles) < 3:
        warnings.append("Fewer than three independent midterm cycles: signed-error estimate is exploratory.")
    return Calibration(
        chamber=chamber, training_cycles=cycles, race_count=sum(map(len, by_cycle.values())),
        cycle_count=len(cycles), estimated_bias=raw_mean * shrinkage,
        residual_sd=residual_sd, shrinkage_weight=shrinkage, warnings=warnings,
    )


def _eligible_history(history: list[HistoricalRace], *, cutoff: date, chamber: Chamber,
                      before_cycle: int, horizon: int) -> list[HistoricalRace]:
    return [row for row in history if row.chamber == chamber and row.cycle % 4 == 2
            and row.cycle < before_cycle and row.outcome_available_date <= cutoff
            and row.election_date < cutoff and row.as_of <= cutoff
            and (row.election_date - row.as_of).days == horizon]


def _rng(seed: int, key: str) -> random.Random:
    # Stable component streams make a race invariant to request ordering or to
    # adding another race. The same shocks are used in all three scenarios.
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _quantile(sorted_values: list[float], probability: float) -> float:
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    return sorted_values[lower] * (upper - position) + sorted_values[upper] * (position - lower) \
        if upper != lower else sorted_values[lower]


def forecast(request: ForecastRequest) -> ForecastReport:
    """Compare a zero-error baseline, prior-cycle estimate and hypothetical shift."""
    config = request.config
    input_sha256 = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    data_origin: DataOrigin = "synthetic" if request.synthetic else "user_supplied_unverified"
    if len({(race.election_date, race.cycle) for race in request.races}) > 1:
        raise ValueError("A forecast request must use one election date and cycle.")
    included_polls = [poll for poll in request.polls
                      if not (request.exclude_partisan and poll.partisan is True)
                      and not (request.exclude_internal and poll.internal is True)]
    aggregates = [aggregate_polls(race, included_polls, config) for race in request.races]
    warnings = [_RESEARCH_WARNING, _UNCERTAINTY_WARNING, _ARCHIVE_WARNING,
                "Aggregate polling errors cannot identify which people switched or why; turnout, "
                "preference change and measurement error remain distinct explanations.",
                "Empirical adjustments use only prior midterm cycles from the same chamber with an exact "
                "days-to-election horizon match and outcomes available by the cutoff."]
    if request.exclude_partisan or request.exclude_internal:
        warnings.append(f"Explicit partisan/internal filters excluded {len(request.polls) - len(included_polls)} "
                        "input polls with matching true flags; unknown flag values were retained and do not "
                        "establish independence or neutrality.")
    if request.synthetic:
        warnings.insert(0, "SYNTHETIC inputs: output is a fixture/scenario demonstration, never a real forecast.")
    if not request.races:
        return ForecastReport(as_of=config.as_of, synthetic=request.synthetic, status="missing_data",
                              warnings=warnings + ["No target race or current poll data supplied."],
                              input_sha256=input_sha256, configuration=config, data_origin=data_origin)
    election_date, cycle = request.races[0].election_date, request.races[0].cycle
    horizon = (election_date - config.as_of).days
    if any((row.election_date - row.as_of).days != horizon for row in request.history):
        warnings.append(f"Historical snapshot horizons differing from the requested {horizon} days to election "
                        "were excluded from empirical fitting.")
    chambers = sorted({race.chamber for race in request.races})
    calibrations = [_fit(_eligible_history(request.history, cutoff=config.as_of, chamber=chamber,
                                         before_cycle=cycle, horizon=horizon), chamber, config)
                    for chamber in chambers]
    by_chamber = {calibration.chamber: calibration for calibration in calibrations}
    national_rng = _rng(config.seed, "national")
    national = [national_rng.gauss(0.0, config.national_sd) for _ in range(config.draws)]
    state_shocks = {}
    for state in sorted({race.state for race in request.races}):
        state_rng = _rng(config.seed, f"state:{state}")
        state_shocks[state] = [state_rng.gauss(0.0, config.state_sd) for _ in range(config.draws)]
    forecasts: list[ScenarioForecast] = []
    unmodeled: list[str] = []
    for race, aggregate in zip(request.races, aggregates):
        if aggregate.poll_margin is None:
            unmodeled.append(race.race_id)
            continue
        calibration = by_chamber[race.chamber]
        sd = max(config.minimum_race_sd, aggregate.sd_sampling or 0.0, calibration.residual_sd,
                 math.hypot(config.national_sd, config.state_sd))
        local_sd = math.sqrt(max(0.0, sd ** 2 - config.national_sd ** 2 - config.state_sd ** 2))
        race_rng = _rng(config.seed, f"race:{race.race_id}")
        residuals = [national[index] + state_shocks[race.state][index] + race_rng.gauss(0.0, local_sd)
                     for index in range(config.draws)]
        ordered_residuals = sorted(residuals)
        scenarios: tuple[tuple[Scenario, float], ...] = (
            ("baseline", 0.0), ("historical", calibration.estimated_bias), ("reversal", config.reversal_shift),
        )
        for scenario, adjustment in scenarios:
            mean = max(-100.0, min(100.0, aggregate.poll_margin + adjustment))
            # Normal latent margin is an approximation. Clip quantiles to the
            # feasible electoral margin range without claiming calibrated tails.
            quantiles = [max(-100.0, min(100.0, mean + _quantile(ordered_residuals, probability)))
                         for probability in (0.05, 0.5, 0.95)]
            probability = (sum(mean + error > 0.0 for error in residuals) + 0.5) / (config.draws + 1)
            forecasts.append(ScenarioForecast(
                race_id=race.race_id, scenario=scenario, poll_margin=aggregate.poll_margin,
                adjustment=adjustment, mean_margin=mean, sd=sd,
                dem_win_probability=probability, p05=quantiles[0], p50=quantiles[1], p95=quantiles[2],
                poll_count=aggregate.poll_count, pollster_count=aggregate.pollster_count,
                effective_pollsters=aggregate.effective_pollsters,
                warnings=["Explicit hypothetical D-minus-R shift; not a learned reversal."]
                if scenario == "reversal" else [],
            ))
    modeled = len(request.races) - len(unmodeled)
    if any(row.cycle % 4 != 2 or row.outcome_available_date > config.as_of for row in request.history):
        warnings.append("Presidential-cycle or outcome-unavailable records were excluded from empirical fitting.")
    warnings.append(f"Monte Carlo seed={config.seed}, draws={config.draws}; maximum probability "
                    f"simulation standard error is approximately {0.5 / math.sqrt(config.draws):.3f}.")
    return ForecastReport(
        as_of=config.as_of, synthetic=request.synthetic,
        status="missing_data" if not modeled else "partial" if unmodeled else "ready",
        forecasts=forecasts, aggregates=aggregates, calibrations=calibrations,
        unmodeled_races=unmodeled, requested_race_count=len(request.races), modeled_race_count=modeled,
        warnings=warnings,
        input_sha256=input_sha256, configuration=config, data_origin=data_origin,
    )


def _metrics(records: list[tuple[float, float, float]]) -> EvaluationMetrics:
    errors = [mean - actual for actual, mean, _ in records]
    distribution = statistics.NormalDist()
    quantile = distribution.inv_cdf(0.95)
    probabilities = [distribution.cdf(mean / sd) for _, mean, sd in records]
    return EvaluationMetrics(
        race_count=len(records), mae=statistics.mean(map(abs, errors)),
        rmse=math.sqrt(statistics.mean(error ** 2 for error in errors)),
        brier=statistics.mean((probability - float(actual > 0.0)) ** 2
                              for (actual, _, _), probability in zip(records, probabilities)),
        interval90_coverage=statistics.mean(float(abs(actual - mean) <= quantile * sd)
                                            for actual, mean, sd in records),
        mean_interval90_width=statistics.mean(min(100.0, mean + quantile * sd) -
                                               max(-100.0, mean - quantile * sd) for _, mean, sd in records),
    )


def backtest(request: BacktestRequest) -> BacktestReport:
    """Strictly forward-cycle diagnostics; never use the tested cycle to fit bias.

    Outcome timestamps must be available at each held-out snapshot. Exact matching
    forecast horizons keep a two-day historical snapshot out of a 60-day test.
    Historical source availability remains a disclosed proxy, not proof of replay.
    """
    eligible = [row for row in request.history if row.cycle % 4 == 2
                and row.outcome_available_date <= request.as_of and row.election_date < request.as_of]
    groups: dict[tuple[int, Chamber], list[HistoricalRace]] = defaultdict(list)
    for row in eligible:
        groups[row.cycle, row.chamber].append(row)
    folds: list[BacktestFold] = []
    evaluated = 0
    skipped = 0
    for (cycle, chamber), rows in sorted(groups.items()):
        baseline_records: list[tuple[float, float, float]] = []
        history_records: list[tuple[float, float, float]] = []
        training_cycles: set[int] = set()
        for row in rows:
            config = ModelConfig(as_of=row.as_of)
            training = _eligible_history(eligible, cutoff=row.as_of, chamber=chamber, before_cycle=cycle,
                                         horizon=(row.election_date - row.as_of).days)
            calibration = _fit(training, chamber, config)
            if calibration.cycle_count < request.min_training_cycles:
                skipped += 1
                continue
            training_cycles.update(calibration.training_cycles)
            sd = max(config.minimum_race_sd, calibration.residual_sd,
                     math.hypot(config.national_sd, config.state_sd))
            baseline_records.append((row.actual_margin, row.poll_margin, sd))
            history_records.append((row.actual_margin,
                                    max(-100.0, min(100.0, row.poll_margin + calibration.estimated_bias)), sd))
        if baseline_records:
            evaluated += len(baseline_records)
            folds.append(BacktestFold(cycle=cycle, chamber=chamber, training_cycles=sorted(training_cycles),
                                      rows=len(baseline_records), baseline=_metrics(baseline_records),
                                      historical=_metrics(history_records)))
    warnings = [_RESEARCH_WARNING, _UNCERTAINTY_WARNING, _ARCHIVE_WARNING,
                "Forward-cycle folds use only earlier midterm cycles with outcomes available before the held-out "
                "snapshot; historical and baseline models share the same uncertainty assumptions. "
                "Races within a cycle are correlated, so row count is not independent cycle count.",
                "No hypothetical reversal scenario is scored as a trained model; this diagnostic tests only "
                "the zero-bias baseline and prior-cycle signed-error estimate."]
    if request.synthetic:
        warnings.insert(0, "SYNTHETIC evaluation: metrics establish software contracts, not election accuracy.")
    if skipped:
        warnings.append(f"Skipped {skipped} eligible rows without the required prior-cycle training history.")
    if not folds:
        warnings.append("No eligible forward-cycle evaluation folds; no accuracy or calibration claim is available.")
    return BacktestReport(as_of=request.as_of, synthetic=request.synthetic, folds=folds, warnings=warnings,
                          history_count=len(eligible), evaluated_race_count=evaluated,
                          input_sha256=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
                          data_origin="synthetic" if request.synthetic else "user_supplied_unverified")
