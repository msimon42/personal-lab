"""Trusted election composition stays independent of games and other profiles."""
from __future__ import annotations

import json
import asyncio
import ast
import os
from pathlib import Path
import subprocess
import sys

from mcp import StdioServerParameters
from mcp.client import Client
import pytest

from personal_lab.bootstrap import create_application
from personal_lab.profiles import PROFILES
from personal_lab.registry import TOOLSETS


ROOT = Path(__file__).resolve().parents[1]


def _source_environment():
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / path) for path in (
        "apps/toolkit/src", "packages/toolkit_core/src", "packages/election_model/src"))
    return environment


def _synthetic_request(race_count=1):
    races, polls = [], []
    for index in range(race_count):
        # Long valid metadata exercises the transport result budget.
        race_id = f"2026-NC-SEN-{index}-" + "r" * 80
        races.append({"race_id": race_id, "chamber": "senate", "state": "NC",
                      "election_date": "2026-11-03", "cycle": 2026})
        for pollster in range(4):
            polls.append({"poll_id": f"fixture-{index}-{pollster}", "race_id": race_id,
                          "pollster": f"Synthetic firm {pollster}" + "p" * 150,
                          "start_date": "2026-10-27", "end_date": "2026-10-28",
                          "available_date": "2026-10-29", "sample_size": 1000,
                          "dem_pct": 50.0, "rep_pct": 45.0, "population": "lv",
                          "mode": "synthetic", "source_id": "synthetic-fixture"})
    return {"races": races, "polls": polls, "config": {"as_of": "2026-11-01", "draws": 1000},
            "synthetic": True}


def test_election_toolset_requires_explicit_reviewed_profile():
    assert PROFILES["election-research"].toolsets == ("core", "election")
    assert all("election" not in profile.toolsets for name, profile in PROFILES.items()
               if name != "election-research")
    assert TOOLSETS["election"].module == "personal_lab.toolsets.election"
    assert TOOLSETS["election"].requirements == ("election-model",)


def test_election_domain_has_no_host_game_or_transport_imports():
    def imports(path):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                yield from (alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                yield node.module.split(".")[0]

    for path in (ROOT / "packages/election_model/src/election_model").rglob("*.py"):
        assert not {"personal_lab", "backend", "mcp", "fastapi", "uvicorn"} & set(imports(path)), path
    for path in (ROOT / "packages/toolkit_core/src/toolkit_core").rglob("*.py"):
        assert "election_model" not in set(imports(path)), path


def test_core_profile_does_not_import_optional_election_package(tmp_path):
    code = """
import builtins, json, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'election_model' or name.startswith('election_model.'):
        raise AssertionError('core-only imported election domain')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from personal_lab.bootstrap import create_application
with create_application(Path(sys.argv[1]), profile='core-only') as app:
    print(json.dumps(sorted(app.operations)))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "state")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert all(name.startswith("lab_") for name in json.loads(result.stdout))


def test_cli_calls_same_typed_boundary_and_preserves_profile_authority(tmp_path):
    def call(operation, arguments):
        return subprocess.run(
            [sys.executable, "-m", "personal_lab", "call", operation, "--profile", "core-only",
             "--state-dir", str(tmp_path / "state"), "--arguments", json.dumps(arguments)],
            capture_output=True, text=True, timeout=30,
        )

    allowed = call("lab_capabilities", {})
    assert allowed.returncode == 0, allowed.stderr
    assert json.loads(allowed.stdout)["data"]["profile"] == "core-only"
    forged = call("lab_capabilities", {"profile": "election-research"})
    assert forged.returncode == 2
    assert json.loads(forged.stdout)["error"]["code"] == "VALIDATION_FAILED"
    disabled = call("election_status", {})
    assert disabled.returncode == 2
    assert "not enabled" in disabled.stderr
    invalid = call("lab_capabilities", [])
    assert invalid.returncode == 2
    assert "JSON object" in invalid.stderr


def test_election_profile_never_imports_game_domain(tmp_path):
    code = """
import asyncio, builtins, json, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'backend' or name.startswith('backend.'):
        raise AssertionError('election profile imported a game domain')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from personal_lab.bootstrap import create_application
with create_application(Path(sys.argv[1]), profile='election-research') as app:
    result = asyncio.run(app.execute('election_status', {}))
    assert result.status == 'ok', result
    print(json.dumps(sorted(app.operations)))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "state")],
                            env=_source_environment(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    names = json.loads(result.stdout)
    assert len([name for name in names if name.startswith("election_")]) == 3
    assert not any(name.startswith(("eu5_", "ck3_")) for name in names)


@pytest.mark.parametrize("input_mode", ["inline", "file"])
def test_cli_public_snapshot_uses_typed_dates_and_records_provenance(tmp_path, input_mode):
    arguments = json.dumps({"dataset_id": "us-2026-11-01",
                            "config": {"as_of": "2026-11-01", "draws": 1000}})
    if input_mode == "file":
        source = tmp_path / "forecast.json"
        source.write_text(arguments, encoding="utf-8")
        argument_source = ["--arguments-file", str(source)]
    else:
        argument_source = ["--arguments", arguments]
    result = subprocess.run(
        [sys.executable, "-m", "personal_lab", "call", "election_forecast",
         "--profile", "election-research", "--state-dir", str(tmp_path / "state"),
         *argument_source],
        env=_source_environment(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["status"] == "ok" and not envelope["artifacts"]
    data = envelope["data"]
    assert data["status"] == "partial" and data["modeled_race_count"] == 4
    assert data["as_of"] == "2026-11-01" and data["data_origin"] == "bundled_public"
    assert len(data["input_sha256"]) == 64 and data["synthetic"] is False


def test_cli_argument_file_is_bounded_explicit_and_mutually_exclusive(tmp_path):
    source = tmp_path / "arguments.json"
    source.write_text("{}", encoding="utf-8")
    command = [sys.executable, "-m", "personal_lab", "call", "lab_capabilities", "--profile", "core-only",
               "--state-dir", str(tmp_path / "state")]

    def run(options):
        return subprocess.run([*command, *options], capture_output=True, text=True, timeout=30)

    assert run([]).returncode == 0
    assert run(["--arguments-file", str(source)]).returncode == 0
    conflict = run(["--arguments-file", str(source), "--arguments", "{}"])
    assert conflict.returncode == 2 and "not allowed with argument" in conflict.stderr
    source.write_text("[]", encoding="utf-8")
    invalid = run(["--arguments-file", str(source)])
    assert invalid.returncode == 2 and "JSON object" in invalid.stderr
    source.write_bytes(b"{}" + b" " * 262144)
    oversized = run(["--arguments-file", str(source)])
    assert oversized.returncode == 2 and "262144-byte" in oversized.stderr


def test_election_maximum_race_output_is_inline_and_bounded(tmp_path):
    async def run():
        with create_application(tmp_path / "state", profile="election-research") as app:
            result = await app.execute("election_forecast", _synthetic_request(10))
            assert result.status == "ok", result
            assert result.data is not None and not result.artifacts
            assert result.data.synthetic is True
            assert result.data.modeled_race_count == 10
            assert len(result.data.forecasts) == 30
            assert len(result.model_dump_json().encode()) <= 32768
            oversized = _synthetic_request(11)
            denied = await app.execute("election_forecast", oversized)
            assert denied.error.code == "VALIDATION_FAILED"
            forged = await app.execute("election_status", {"source_path": "/outside/root"})
            assert forged.error.code == "VALIDATION_FAILED"
            assert all(operation.read_only and operation.effect == "observation"
                       for name, operation in app.operations.items() if name.startswith("election_"))
    asyncio.run(run())


def test_election_workflow_over_real_mcp_subprocess(tmp_path):
    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "personal_lab", "mcp", "--profile", "election-research",
                  "--state-dir", str(tmp_path / "state")],
            cwd=str(tmp_path), env=_source_environment(),
        )
        async with Client(parameters) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert {"election_status", "election_forecast", "election_backtest"} <= tools.keys()
            assert not any(name.startswith(("eu5_", "ck3_")) for name in tools)
            for name in ("election_status", "election_forecast", "election_backtest"):
                assert tools[name].annotations.read_only_hint is True
                assert tools[name].input_schema["additionalProperties"] is False

            async def call(name, arguments):
                response = await client.call_tool(name, arguments)
                assert not response.is_error, response
                envelope = response.structured_content
                assert envelope["data"] is not None and not envelope["artifacts"]
                return envelope["data"]

            status = await call("election_status", {})
            assert status["sources"] and status["findings"]
            assert status["current_polls"] == 4 and status["current_races"] == 4
            assert status["status"] == "partial_race_coverage"
            missing = await call("election_forecast", {"config": {"as_of": "2026-11-01"}})
            assert missing["status"] == "missing_data"
            assert missing["modeled_race_count"] == 0 and not missing["synthetic"]
            public = await call("election_forecast", {
                "dataset_id": "us-2026-11-01", "config": {"as_of": "2026-11-01", "draws": 1000}})
            assert public["status"] == "partial" and public["modeled_race_count"] == 4
            assert public["synthetic"] is False
            request = _synthetic_request()
            first = await call("election_forecast", request)
            assert first == await call("election_forecast", request)
            assert first["synthetic"] and first["modeled_race_count"] == 1
            assert {row["scenario"] for row in first["forecasts"]} == {"baseline", "historical", "reversal"}
            evaluated = await call("election_backtest", {})
            assert evaluated["history_count"] == status["historical_races"]
            assert evaluated["synthetic"] is False
            denied = await client.call_tool("election_forecast", {**request, "profile": "personal"})
            assert denied.is_error
    asyncio.run(run())
