"""Real bundled evidence coverage and service boundaries, not election truth tests."""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import subprocess
import sys

import pytest

from election_model.contracts import BacktestRequest, ForecastRequest
from election_model.datasets import load_document, load_history
from election_model.service import ElectionService


ROOT = Path(__file__).resolve().parents[1]


def test_packaged_evidence_is_real_partial_and_dates_are_honest():
    service = ElectionService()
    status = service.status()
    assert status.historical_races == 771
    assert status.historical_cycles == [1998, 2002, 2006, 2010, 2014, 2018, 2022]
    assert status.current_races == 4 and status.current_polls == 4
    assert {finding.status for finding in status.findings} >= {"observed", "methodological", "missing"}
    for document in ("history.json", "history-v2.json", "current.json", "research.json"):
        assert load_document(document)
    history, metadata = load_history()
    assert metadata["schema_version"] == "2.0"
    assert all(row.cycle < 2024 for row in history)
    with pytest.raises(ValueError, match="Unknown trusted"):
        load_document("../../secret")


def test_service_requires_explicit_snapshot_and_preserves_missing_data():
    service = ElectionService()
    missing = service.forecast(ForecastRequest.model_validate({"config": {"as_of": "2026-11-01"}}))
    assert missing.status == "missing_data" and not missing.forecasts
    request = ForecastRequest.model_validate({"dataset_id": "us-2026-11-01", "config": {"as_of": "2026-11-01"}})
    result = service.forecast(request)
    assert result.status == "partial" and result.data_origin == "bundled_public"
    assert result.recommended_scenario == "baseline"
    assert len(result.input_sha256) == 64 and result.configuration.as_of == date(2026, 11, 1)
    assert result == service.forecast(request)
    assert len(result.forecasts) == 12 and not result.synthetic
    assert {row.latest_poll_age_days for row in result.aggregates} == {4, 13, 26, 33}
    assert all(row.adjustment == 0 for row in result.forecasts if row.scenario == "baseline")
    assert all(row.adjustment == 3 for row in result.forecasts if row.scenario == "reversal")
    assert any("All four" in warning for warning in result.warnings)


@pytest.mark.parametrize("overrides,match", [
    ({"as_of": "2026-10-31"}, "supports only"),
    ({"half_life_days": 12.0}, "requires half_life"),
    ({"pollster_cap": 0.5}, "requires pollster_cap"),
    ({"max_poll_age_days": 20}, "requires max_poll_age"),
])
def test_service_rejects_incompatible_bundled_calibration(overrides, match):
    request = ForecastRequest.model_validate({"dataset_id": "us-2026-11-01",
                                             "config": {"as_of": "2026-11-01", **overrides}})
    with pytest.raises(ValueError, match=match):
        ElectionService().forecast(request)


def test_real_snapshot_cannot_be_relabeled_synthetic_and_future_outcomes_are_excluded():
    request = ForecastRequest.model_validate({"dataset_id": "us-2026-11-01", "synthetic": True,
                                             "config": {"as_of": "2026-11-01"}})
    with pytest.raises(ValueError, match="Synthetic requests"):
        ElectionService().forecast(request)
    report = ElectionService().backtest(BacktestRequest(as_of=date(2000, 1, 1)))
    assert not report.folds and report.evaluated_race_count == 0


def test_research_report_repeats_identically_and_refuses_changed_evidence(tmp_path):
    command = [sys.executable, str(ROOT / "scripts/election_research_report.py"), "--output-dir", str(tmp_path)]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert subprocess.run(command, capture_output=True).returncode == 0
    assert before == {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    (tmp_path / "race-scenarios.json").write_text("different evidence")
    failed = subprocess.run(command, capture_output=True, text=True)
    assert failed.returncode != 0 and "Refusing to replace" in failed.stderr
    assert json.loads((tmp_path / "manifest.json").read_text())["model_version"] == "0.1.0"
