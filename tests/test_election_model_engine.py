"""Synthetic fixtures test software contracts; they are not election evidence."""
from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from election_model.contracts import (
    BacktestRequest,
    ForecastRequest,
    HistoricalRace,
    ModelConfig,
    Poll,
    Race,
)
from election_model.model import aggregate_polls, backtest, forecast


AS_OF = date(2026, 11, 1)
ELECTIONS = {2010: date(2010, 11, 2), 2014: date(2014, 11, 4), 2018: date(2018, 11, 6),
             2022: date(2022, 11, 8), 2024: date(2024, 11, 5), 2026: date(2026, 11, 3)}


def race(race_id="synthetic-senate-AA", state="AA", chamber="senate"):
    return Race(race_id=race_id, chamber=chamber, state=state, election_date=ELECTIONS[2026],
                cycle=2026, map_vintage="synthetic-2026" if chamber == "house" else None)


def poll(poll_id="synthetic-poll", pollster="synthetic-firm", race_id="synthetic-senate-AA", **changes):
    values = dict(poll_id=poll_id, race_id=race_id, pollster=pollster, start_date=date(2026, 10, 25),
                  end_date=date(2026, 10, 28), available_date=date(2026, 10, 29), sample_size=1000,
                  sample_size_verified=True,
                  dem_pct=48.0, rep_pct=48.0, population="lv", mode="synthetic", source_id="synthetic")
    return Poll(**(values | changes))


def history(cycle, residual=-4.0, chamber="senate", suffix="AA", **changes):
    election_date = ELECTIONS[cycle]
    values = dict(race_id=f"synthetic-{cycle}-{chamber}-{suffix}", chamber=chamber, state="AA", cycle=cycle,
                  election_date=election_date, as_of=election_date - timedelta(days=2),
                  poll_margin=0.0, actual_margin=residual, poll_count=3, pollster_count=3,
                  source_id="synthetic", outcome_available_date=election_date + timedelta(days=30))
    return HistoricalRace(**(values | changes))


def request(*, races=None, polls=None, rows=None, exclude_partisan=False, exclude_internal=False, **config):
    return ForecastRequest(races=[race()] if races is None else races,
                           polls=[poll()] if polls is None else polls, history=rows or [],
                           exclude_partisan=exclude_partisan, exclude_internal=exclude_internal,
                           config=ModelConfig(as_of=AS_OF, draws=1000, **config), synthetic=True)


def test_cutoff_late_availability_and_stale_polls_are_not_observations():
    fresh = poll(dem_pct=50.0, rep_pct=46.0)
    future = poll("future", "another", available_date=date(2026, 11, 2), dem_pct=90.0, rep_pct=0.0)
    stale = poll("stale", "third", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2),
                 available_date=date(2026, 8, 3), dem_pct=0.0, rep_pct=90.0)
    result = aggregate_polls(race(), [fresh, future, stale], ModelConfig(as_of=AS_OF))
    assert result.poll_margin == 4.0
    assert result.poll_count == result.pollster_count == 1


def test_repeated_pollster_tracking_and_transitive_panels_add_no_precision():
    first = poll("a", "firm-a", overlap_group="panel-1")
    second = poll("b", "firm-a", overlap_group="panel-2")
    latest = poll("c", "firm-b", overlap_group="panel-1", end_date=date(2026, 10, 30),
                  available_date=date(2026, 10, 31), dem_pct=50.0, rep_pct=46.0)
    result = aggregate_polls(race(), [first, second, latest], ModelConfig(as_of=AS_OF))
    single = aggregate_polls(race(), [latest], ModelConfig(as_of=AS_OF))
    assert result.poll_count == 1
    assert result.effective_pollsters == 1.0
    assert result.poll_margin == single.poll_margin == 4.0
    assert result.sd_sampling == single.sd_sampling
    assert "overlapping" in " ".join(result.warnings)


def test_likely_voter_tie_breaker_is_explicit_and_input_order_independent():
    lv = poll("a", dem_pct=50.0, rep_pct=46.0)
    rv = poll("z", population="rv", sample_size=2000, dem_pct=40.0, rep_pct=55.0)
    config = ModelConfig(as_of=AS_OF)
    assert aggregate_polls(race(), [lv, rv], config) == aggregate_polls(race(), [rv, lv], config)
    assert aggregate_polls(race(), [lv, rv], config).poll_margin == 4.0


def test_single_large_pollster_does_not_dominate_independent_cluster_weights():
    polls = [poll("big", "big", sample_size=1000000, dem_pct=100.0, rep_pct=0.0),
             poll("small-a", "small-a", sample_size=100), poll("small-b", "small-b", sample_size=100)]
    result = aggregate_polls(race(), polls, ModelConfig(as_of=AS_OF, pollster_cap=0.35))
    assert result.poll_margin == pytest.approx(35.0)
    assert result.effective_pollsters > 2.9


def test_extremely_short_valid_half_life_does_not_underflow_the_weight_sum():
    polls = [poll("recent", "recent"), poll("older", "older", end_date=date(2026, 10, 27)),
             poll("oldest", "oldest", end_date=date(2026, 10, 26))]
    result = aggregate_polls(race(), polls, ModelConfig(as_of=AS_OF, half_life_days=1e-300))
    assert result.poll_margin == 0.0
    assert 1.0 <= result.effective_pollsters <= 3.0


def test_single_poll_retains_staleness_metadata_when_recency_weight_cancels():
    old = poll(start_date=date(2026, 9, 28), end_date=date(2026, 9, 29), available_date=date(2026, 9, 30))
    result = aggregate_polls(race(), [old], ModelConfig(as_of=AS_OF))
    assert result.latest_poll_age_days == 33
    assert result.newest_poll_end_date == result.oldest_poll_end_date == date(2026, 9, 29)
    assert "33 days old" in " ".join(result.warnings)
    assert "no fitted late-movement" in " ".join(result.warnings)


def test_multinomial_margin_sampling_error_and_duplicate_identity_boundary():
    sample = poll(dem_pct=50.0, rep_pct=50.0)
    result = aggregate_polls(race(), [sample, sample], ModelConfig(as_of=AS_OF))
    assert result.sd_sampling == pytest.approx(100 / 1000 ** 0.5)
    with pytest.raises(ValueError, match="Conflicting"):
        aggregate_polls(race(), [sample, poll(dem_pct=49.0, rep_pct=49.0)], ModelConfig(as_of=AS_OF))


def test_unverified_sample_counts_never_create_arbitrary_weight_or_precision():
    config = ModelConfig(as_of=AS_OF, pollster_cap=1.0)
    low = poll(sample_size=1, sample_size_verified=False)
    high = poll(sample_size=1000000, sample_size_verified=False)
    assert aggregate_polls(race(), [low], config) == aggregate_polls(race(), [high], config)
    polls = [poll("a", "a", sample_size=1, sample_size_verified=False, dem_pct=60.0, rep_pct=40.0),
             poll("b", "b", sample_size=1000000, sample_size_verified=False, dem_pct=40.0, rep_pct=60.0),
             poll("c", "c", sample_size=50, sample_size_verified=False)]
    unverified = aggregate_polls(race(), polls, config)
    verified = aggregate_polls(race(), [item.model_copy(update={"sample_size_verified": True})
                                      for item in polls], config)
    assert unverified.poll_margin == 0.0
    assert verified.poll_margin < -19.0
    assert "numerical placeholder" in " ".join(unverified.warnings)
    assert "observed precision" in " ".join(unverified.warnings)


def test_explicit_internal_partisan_filters_preserve_unknown_metadata():
    polls = [poll("neutral-unknown", "unknown", partisan=None, internal=None),
             poll("partisan", "party", partisan=True, dem_pct=60.0, rep_pct=40.0),
             poll("internal", "campaign", internal=True, dem_pct=40.0, rep_pct=60.0)]
    result = forecast(request(polls=polls, exclude_partisan=True, exclude_internal=True))
    assert result.aggregates[0].poll_count == 1
    assert result.aggregates[0].poll_margin == 0.0
    assert "excluded 2 input polls" in " ".join(result.warnings)
    assert "unknown flag values were retained" in " ".join(result.warnings)


def test_provider_creation_timestamp_is_disclosed_as_an_availability_proxy():
    result = aggregate_polls(race(), [poll(availability_basis="provider_created_at_proxy")],
                             ModelConfig(as_of=AS_OF))
    assert "availability proxy" in " ".join(result.warnings)


def test_missing_current_polls_never_manufacture_forecast_or_chamber_probability():
    result = forecast(request(polls=[], rows=[history(2018), history(2022)]))
    assert result.status == "missing_data"
    assert result.modeled_race_count == 0
    assert result.forecasts == []
    assert result.aggregates[0].poll_margin is None
    assert result.unmodeled_races == [race().race_id]
    assert "No chamber-control" in " ".join(result.warnings)
    assert not any("seat" in key or "control" in key for key in result.model_dump())
    empty = forecast(request(races=[], polls=[]))
    assert empty.status == "missing_data" and empty.forecasts == []


def test_history_uses_equal_cycle_weight_shrinkage_and_dem_minus_rep_sign():
    rows = [history(2018, residual=-10.0)] + [history(2022, residual=0.0, suffix=str(i)) for i in range(20)]
    result = forecast(request(rows=rows))
    calibration = result.calibrations[0]
    assert calibration.training_cycles == [2018, 2022]
    assert calibration.race_count == 21
    assert calibration.estimated_bias == pytest.approx(-2.0)  # mean(-10,0) * 2/(2+3)
    models = {row.scenario: row for row in result.forecasts}
    assert models["baseline"].mean_margin == 0.0
    assert models["historical"].mean_margin == -2.0
    assert models["reversal"].mean_margin == 3.0
    assert models["historical"].dem_win_probability < models["baseline"].dem_win_probability
    assert models["reversal"].dem_win_probability > models["baseline"].dem_win_probability
    assert models["reversal"].p05 - models["baseline"].p05 == pytest.approx(3.0)
    assert "hypothetical" in " ".join(models["reversal"].warnings)


def test_presidential_cycles_future_outcomes_different_horizons_and_other_chambers_excluded():
    rows = [history(2018, residual=-5.0), history(2022, residual=-5.0),
            history(2024, residual=30.0), history(2010, residual=30.0, chamber="house"),
            history(2014, residual=30.0, outcome_available_date=date(2026, 11, 2)),
            history(2010, residual=30.0, as_of=ELECTIONS[2010] - timedelta(days=30))]
    result = forecast(request(rows=rows))
    assert result.calibrations[0].training_cycles == [2018, 2022]
    assert result.calibrations[0].estimated_bias == -2.0
    assert result.calibrations[0].race_count == 2


def test_common_error_floor_does_not_disappear_with_more_pollsters():
    polls = [poll(str(i), str(i)) for i in range(30)]
    result = forecast(request(polls=polls))
    assert result.aggregates[0].effective_pollsters == pytest.approx(30.0)
    assert result.forecasts[0].sd == 4.0
    assert result.aggregates[0].sd_sampling < 1.0
    assert "not fitted 2026" in " ".join(result.warnings)


def test_simulation_is_repeatable_and_race_order_invariant_with_shared_component_streams():
    second = race("synthetic-senate-BB", "BB")
    second_poll = poll("second", "second", race_id=second.race_id)
    first_result = forecast(request(races=[race(), second], polls=[poll(), second_poll]))
    reverse_result = forecast(request(races=[second, race()], polls=[second_poll, poll()]))
    assert first_result == forecast(request(races=[race(), second], polls=[poll(), second_poll]))
    first_rows = {(row.race_id, row.scenario): row for row in first_result.forecasts}
    reverse_rows = {(row.race_id, row.scenario): row for row in reverse_result.forecasts}
    assert first_rows == reverse_rows
    solo = forecast(request())
    assert solo.forecasts == [row for row in first_result.forecasts if row.race_id == race().race_id]


def test_shared_national_error_does_not_diversify_across_races():
    # Turn off all idiosyncratic/state noise. Identical marginals then use exactly
    # the same draw outcomes, demonstrating the common national component.
    second = race("synthetic-senate-BB", "BB")
    result = forecast(request(races=[race(), second],
                              polls=[poll(), poll("second", "second", race_id=second.race_id)],
                              national_sd=4.0, state_sd=0.0))
    left, right = result.forecasts[0], result.forecasts[3]
    assert left.dem_win_probability == right.dem_win_probability
    assert (left.p05, left.p50, left.p95) == (right.p05, right.p50, right.p95)


def test_forward_backtest_does_not_fit_the_cycle_it_scores_and_can_reject_bias_model():
    rows = [history(2010, -4.0), history(2014, -4.0), history(2018, 4.0), history(2022, 4.0),
            history(2024, 80.0), history(2026, 80.0)]
    result = backtest(BacktestRequest(history=rows, as_of=AS_OF, synthetic=True))
    assert result.history_count == 4
    assert result.evaluated_race_count == 2
    assert [row.cycle for row in result.folds] == [2018, 2022]
    first = result.folds[0]
    assert first.training_cycles == [2010, 2014]
    assert first.baseline.mae == 4.0
    assert first.historical.mae == pytest.approx(5.6)
    assert first.baseline.brier < first.historical.brier
    assert 0.0 <= first.historical.interval90_coverage <= 1.0
    assert "retrospective diagnostic" in " ".join(result.warnings)
    assert "SYNTHETIC evaluation" in " ".join(result.warnings)


def test_historical_outcome_availability_is_checked_at_each_holdout_cutoff():
    rows = [history(2010, outcome_available_date=date(2020, 1, 1)), history(2014), history(2018), history(2022)]
    result = backtest(BacktestRequest(history=rows, as_of=AS_OF, synthetic=True))
    assert [row.cycle for row in result.folds] == [2022]
    assert result.folds[0].training_cycles == [2010, 2014, 2018]


def test_forward_backtest_never_borrows_house_errors_for_senate_or_other_horizons():
    rows = [history(2010, chamber="house"), history(2014, chamber="house"), history(2018),
            history(2022, as_of=ELECTIONS[2022] - timedelta(days=30))]
    result = backtest(BacktestRequest(history=rows, as_of=AS_OF, min_training_cycles=1, synthetic=True))
    assert [(fold.cycle, fold.chamber) for fold in result.folds] == [(2014, "house")]
    assert result.evaluated_race_count == 1
    empty = backtest(BacktestRequest(history=[history(2018)], as_of=AS_OF, synthetic=True))
    assert empty.folds == []
    assert "No eligible forward-cycle" in " ".join(empty.warnings)


def test_forecast_partial_coverage_is_explicit():
    second = race("synthetic-senate-BB", "BB")
    result = forecast(request(races=[race(), second]))
    assert result.status == "partial"
    assert result.requested_race_count == 2
    assert result.modeled_race_count == 1
    assert result.unmodeled_races == [second.race_id]
    assert "SYNTHETIC inputs" in " ".join(result.warnings)


@pytest.mark.parametrize("changes", [{"half_life_days": 0.0}, {"half_life_days": float("nan")},
                                     {"minimum_race_sd": 0.0}, {"national_sd": float("inf")}])
def test_nonfinite_or_zero_scale_inputs_fail_before_numerical_execution(changes):
    with pytest.raises(ValidationError):
        ModelConfig(as_of=AS_OF, **changes)


@pytest.mark.parametrize("changes", [{"available_date": date(2026, 10, 27)},
                                     {"dem_pct": float("nan")}, {"dem_pct": 70.0, "rep_pct": 60.0}])
def test_incoherent_poll_inputs_fail_validation(changes):
    with pytest.raises(ValidationError):
        poll(**changes)


def test_duplicate_race_poll_and_historical_ids_cannot_inflate_request_evidence():
    with pytest.raises(ValidationError, match="Duplicate race_id"):
        request(races=[race(), race()])
    with pytest.raises(ValidationError, match="Duplicate poll_id"):
        request(polls=[poll(), poll()])
    with pytest.raises(ValidationError, match="Duplicate race_id"):
        request(rows=[history(2018), history(2018)])
    with pytest.raises(ValidationError, match="unique IDs"):
        BacktestRequest(history=[history(2018), history(2018)], as_of=AS_OF)


def test_post_election_cutoffs_cannot_enter_direct_aggregation_or_forecast():
    with pytest.raises(ValueError, match="precede election day"):
        aggregate_polls(race(), [poll()], ModelConfig(as_of=ELECTIONS[2026]))
    with pytest.raises(ValidationError, match="cutoff must precede"):
        ForecastRequest(races=[race()], polls=[poll()], config=ModelConfig(as_of=ELECTIONS[2026]))
    with pytest.raises(ValidationError, match="must precede election day"):
        history(2018, as_of=ELECTIONS[2018])
