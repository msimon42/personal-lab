"""Generic operations; importing this module imports no mod-engine code."""
from __future__ import annotations

import platform
from typing import Any

from pydantic import Field

from toolkit_core.contracts import ArtifactRef, Empty, OperationSpec, StrictModel


class Capabilities(StrictModel):
    instance_id: str
    version: str
    profile: str
    python: str
    toolsets: list[dict[str, Any]]
    limits: dict[str, int]
    capabilities: dict[str, Any]
    limitations: list[str]


class Run(StrictModel):
    run_id: str = Field(min_length=1, max_length=200)


class RunEvents(Run):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=200)


class RunState(StrictModel):
    run_id: str
    state: str
    instance_id: str
    result: dict[str, Any] | None = None
    details: dict[str, Any]


class Events(StrictModel):
    events: list[dict[str, Any]]
    next_cursor: int | None
    disclosure: dict[str, Any] | None = None


class Cancelled(StrictModel):
    run_id: str
    cancellation_requested: bool


class ArtifactRead(StrictModel):
    artifact_id: str = Field(min_length=1, max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8192, ge=1, le=8192)


class ArtifactSlice(StrictModel):
    artifact: ArtifactRef
    offset: int
    bytes_base64: str
    next_offset: int | None


def build_operations(application):
    from personal_lab import __version__
    from personal_lab.registry import TOOLSETS

    def capabilities(args):
        available = {"game_launch": {"available": False, "reason": "No qualified execution adapter is enabled."},
                     "deployment": {"available": False, "reason": "Production game deployment remains qualification-gated."},
                     "model_calls": {"available": False, "reason": "Default operations make no model API calls."}}
        if "ck3" in application.profile.toolsets:
            from backend.agent.context import capabilities as ck3_capabilities
            available["ck3"] = ck3_capabilities()
        return Capabilities(instance_id=application.instance_id, version=__version__, profile=application.profile.name,
            python=platform.python_version(), toolsets=[{"name": TOOLSETS[name].name, "version": TOOLSETS[name].version,
                "description": TOOLSETS[name].description} for name in application.profile.toolsets],
            limits={"structured_result_bytes": 32768, "default_page_records": 50, "maximum_page_records": 200,
                    "artifact_read_bytes": 8192, "worker_count": 1, "queued_runs": 16}, capabilities=available,
            limitations=["Synthetic checks do not establish real-game conformance."] if application.service else [])

    def run(args):
        record = application.project_run(args.run_id)
        return RunState(run_id=record["run_id"], state=record["state"], instance_id=application.instance_id,
                        result=record.get("result"), details={k: v for k, v in record.items() if k != "result"})

    return [
        OperationSpec("lab_capabilities", "Discover this instance, enabled toolsets, limits and unavailable capabilities.",
                      Empty, Capabilities, capabilities, read_only=True, effect="observation"),
        OperationSpec("lab_run_get", "Inspect persistent run state and committed results; polling does not restart work.",
                      Run, RunState, run, read_only=True, effect="observation"),
        OperationSpec("lab_run_events", "Read an ordered bounded page of persistent run events.",
                      RunEvents, Events, lambda args: application.project_events(args.run_id, args.cursor, args.limit),
                      read_only=True, effect="observation"),
        OperationSpec("lab_run_cancel", "Request cancellation at a safe boundary; committed effects remain reported.",
                      Run, Cancelled, lambda args: application.cancel_run(args.run_id)),
        OperationSpec("lab_artifact_read", "Read a bounded slice of an immutable authorized artifact by ID.",
                      ArtifactRead, ArtifactSlice,
                      lambda args: application.artifacts.read(args.artifact_id, args.offset, args.limit),
                      read_only=True, effect="observation"),
    ]
