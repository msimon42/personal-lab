"""Thin trusted host registration for the independent aggregate election domain.

Operations perform bounded local computation and read only packaged public data.
They accept no source paths, network destinations, credentials, or approval inputs.
"""
from toolkit_core.contracts import Empty, OperationSpec


def build_operations(application):
    from election_model.contracts import (
        BacktestReport,
        BacktestRequest,
        ForecastReport,
        ForecastRequest,
        ResearchStatus,
    )
    from election_model.service import ElectionService

    service = ElectionService()
    return [
        OperationSpec(
            "election_status",
            "Inspect aggregate election research, public source provenance, coverage and missing-data limitations.",
            Empty, ResearchStatus, lambda args: service.status(), read_only=True, effect="observation",
        ),
        OperationSpec(
            "election_forecast",
            "Compare bounded aggregate election margins and uncertainty under baseline, historical and reversal scenarios.",
            ForecastRequest, ForecastReport, service.forecast, read_only=True, effect="observation",
        ),
        OperationSpec(
            "election_backtest",
            "Evaluate aggregate polling-error models with earlier-cycle training and explicit temporal cutoffs.",
            BacktestRequest, BacktestReport, service.backtest, read_only=True, effect="observation",
        ),
    ]
