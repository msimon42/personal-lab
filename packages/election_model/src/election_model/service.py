"""Application operations for aggregate election research, without host imports."""
from __future__ import annotations

from datetime import date
import hashlib
import json

from .contracts import (
    BacktestReport, BacktestRequest, ContextObservation, ForecastReport,
    ForecastRequest, ResearchFinding, ResearchStatus, SourceRecord,
)
from .datasets import has_document, load_document, load_history
from .model import backtest, forecast


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


class ElectionService:
    """Bounded local observations/computation; no network, paths, state or effects."""

    def status(self) -> ResearchStatus:
        research = load_document("research.json")
        current = load_document("current.json")
        history, _ = load_history()
        aggregator = load_document("votehub.json") if has_document("votehub.json") else None
        return ResearchStatus(
            as_of=date.fromisoformat(research["as_of"]), status="partial_race_coverage",
            historical_races=len(history), historical_cycles=sorted({row.cycle for row in history}),
            current_races=len(current["races"]), current_polls=len(current["polls"]),
            sources=[SourceRecord.model_validate(row) for row in research["sources"]],
            findings=[ResearchFinding.model_validate(row) for row in research["findings"]],
            context_observations=[ContextObservation.model_validate(row)
                                  for row in research["context_observations"]],
            limitations=research["limitations"],
            aggregator_races=len(aggregator["races"]) if aggregator else 0,
            aggregator_polls=len(aggregator["polls"]) if aggregator else 0,
            aggregator_race_ids=[row["race_id"] for row in aggregator["races"]] if aggregator else [],
            aggregator_rejections=len(aggregator["rejections"]) if aggregator else 0,
        )

    def forecast(self, request: ForecastRequest) -> ForecastReport:
        warnings: list[str] = []
        data = request.model_dump(mode="json")
        bundled = request.dataset_id != "inline"
        current = None
        if bundled:
            current = load_document("votehub.json" if request.dataset_id == "votehub-2026-11-01" else "current.json")
            snapshot_date = date.fromisoformat(current["metadata"]["as_of"])
            if request.config.as_of != snapshot_date:
                raise ValueError("The us-2026-11-01 snapshot supports only its November1 cutoff; "
                                 "supply separately sourced inline data for another date.")
            if request.synthetic:
                raise ValueError("Synthetic requests cannot select the real public-data snapshot.")
            if request.dataset_id == "us-2026-11-01":
                sources = {row["source_id"]: row for row in load_document("research.json")["sources"]}
                polls = [{**row, "sample_size_verified": True, "availability_basis": "publisher_release",
                          "source_url": sources[row["source_id"]]["url"]} for row in current["polls"]]
            else:
                polls = current["polls"]
            known = {row["race_id"] for row in current["races"]}
            selected = set(request.race_ids) if request.race_ids else known
            if not selected.issubset(known):
                raise ValueError("Selected race IDs are absent from the chosen snapshot.")
            if len(selected) > 10:
                raise ValueError("Select at most ten race IDs from election_status.aggregator_race_ids.")
            data.update(dataset_id="inline", race_ids=[],
                        races=[row for row in current["races"] if row["race_id"] in selected],
                        polls=[row for row in polls if row["race_id"] in selected])
            warnings.extend(current["metadata"]["limitations"])
        history_metadata = None
        if not request.history and not request.synthetic:
            history, history_metadata = load_history()
            defaults = history_metadata["normalization"]["model_defaults"]
            for key in ("half_life_days", "max_poll_age_days", "pollster_cap"):
                if getattr(request.config, key) != defaults[key]:
                    raise ValueError(f"Bundled historical calibration requires {key}={defaults[key]}; "
                                     "provide comparably normalized history for different aggregation settings.")
            data["history"] = [row.model_dump(mode="json") for row in history]
            warnings.extend(history_metadata["limitations"])
        resolved = ForecastRequest.model_validate(data)
        report = forecast(resolved)
        if not request.synthetic and not bundled:
            warnings.append("Inline races/polls are user supplied and unverified; scenario output does not verify their provenance.")
        if request.history and not request.synthetic:
            warnings.append("Historical rows were supplied by the caller; their vintage, selection and normalization are unverified.")
        if bundled:
            warnings.append("Baseline is the default: retrospective held-out evidence does not justify promoting a directional correction.")
        if request.dataset_id == "us-2026-11-01":
            warnings.append("All four current seed polls come from Emerson; there is no independent cross-firm confirmation. "
                            "The seed is not an exhaustive inventory of races or polls.")
        provenance = {"resolved_request": resolved.model_dump(mode="json"),
                      "historical_metadata": history_metadata,
                      "current_metadata": current["metadata"] if current else None}
        return report.model_copy(update={
            "status": "partial" if bundled and report.modeled_race_count else report.status,
            "input_sha256": _hash(provenance),
            "data_origin": "synthetic" if request.synthetic else
                           "bundled_public" if bundled and not request.history else "user_supplied_unverified",
            "warnings": list(dict.fromkeys(report.warnings + warnings)),
        })

    def backtest(self, request: BacktestRequest) -> BacktestReport:
        metadata = None
        bundled = not request.history and not request.synthetic
        data = request.model_dump(mode="json")
        if bundled:
            history, metadata = load_history()
            data["history"] = [row.model_dump(mode="json") for row in history]
        resolved = BacktestRequest.model_validate(data)
        report = backtest(resolved)
        warnings = report.warnings + (metadata["limitations"] if metadata else [])
        if not request.synthetic and not bundled:
            warnings.append("Caller-supplied history has not had its provenance independently verified.")
        return report.model_copy(update={
            "input_sha256": _hash({"resolved_request": data, "historical_metadata": metadata}),
            "data_origin": "synthetic" if request.synthetic else
                           "bundled_public" if bundled else "user_supplied_unverified",
            "warnings": list(dict.fromkeys(warnings)),
        })
