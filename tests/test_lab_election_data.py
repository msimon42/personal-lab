"""Trusted host composition for the read-only election data toolset."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from mcp import StdioServerParameters
from mcp.client import Client

from personal_lab.bootstrap import create_application
from personal_lab.profiles import PROFILES
from personal_lab.registry import TOOLSETS

from election_data.core import connect, writer_lock
from election_data.release import build_release, publish_release
from election_data_support import load_universe, write_sources

ROOT = Path(__file__).resolve().parents[1]
TOOLS = {"election_data_status", "election_data_contests", "election_data_results", "election_data_provenance",
         "election_data_map", "election_data_issues"}


def _environment():
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(str(ROOT / path) for path in (
        "apps/toolkit/src", "packages/toolkit_core/src", "packages/election_data/src"))
    return environment


@pytest.fixture(scope="module")
def configured(tmp_path_factory):
    base = tmp_path_factory.mktemp("lab-election-data")
    root, _ = load_universe(base / "data", write_sources(base / "sources"))
    with closing(connect(root.database)) as connection, writer_lock(root):
        publish_release(root, connection, build_release(root, connection)["release_id"])
    config = base / "lab.toml"
    config.write_text(f"election_data_root = {json.dumps(str(root.path))}\n", encoding="utf-8")
    return base, root, config


def test_toolset_requires_its_explicit_profile():
    assert PROFILES["election-data"].toolsets == ("core", "election_data")
    assert all("election_data" not in profile.toolsets for name, profile in PROFILES.items() if name != "election-data")
    assert TOOLSETS["election_data"].requirements == ("election-data",)


def test_domain_package_has_no_host_transport_or_other_domain_imports():
    forbidden = {"personal_lab", "backend", "mcp", "fastapi", "uvicorn", "election_model", "toolkit_core"}
    for path in (ROOT / "packages/election_data/src/election_data").rglob("*.py"):
        names = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names.add(node.module.split(".")[0])
        assert not forbidden & names, path


def test_core_profile_never_imports_election_data(tmp_path):
    code = """
import builtins, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'election_data' or name.startswith('election_data.'):
        raise AssertionError('core-only imported election_data')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from personal_lab.bootstrap import create_application
with create_application(Path(sys.argv[1]), profile='core-only') as app:
    assert not any(name.startswith('election_data') for name in app.operations)
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "state")], env=_environment(),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_unconfigured_profile_reports_status_and_refuses_reads(tmp_path):
    async def run():
        with create_application(tmp_path / "state", profile="election-data") as app:
            status = await app.execute("election_data_status", {})
            assert status.status == "ok" and status.data.configured is False
            refused = await app.execute("election_data_contests", {})
            assert refused.status == "blocked" and refused.error.code == "CAPABILITY_UNAVAILABLE"
            assert set(app.operations) >= TOOLS
            assert all(app.operations[name].read_only and app.operations[name].effect == "observation"
                       for name in TOOLS)
    asyncio.run(run())


def test_every_contest_page_fits_the_response_budget(configured):
    base, _, config = configured

    async def run():
        from personal_lab.profiles import load_configuration
        from personal_lab.bootstrap import LabApplication
        with LabApplication(base / "state-budget", "election-data", load_configuration(config)) as app:
            contests, cursor = [], 0
            while cursor is not None:
                page = await app.execute("election_data_contests", {"cursor": cursor, "limit": 15})
                assert page.status == "ok" and not page.artifacts
                contests += page.data.contests
                cursor = page.data.next_cursor
            assert len(contests) == 56
            for contest in contests:
                for level in ("state", "county"):
                    result = await app.execute("election_data_results",
                                               {"contest_id": contest.contest_id, "level": level, "limit": 15})
                    assert result.status == "ok" and not result.artifacts
                    assert len(result.model_dump_json().encode()) <= 32768
            for forged in ({"contest_id": contests[0].contest_id, "data_root": "/tmp"},
                           {"contest_id": "../../etc/passwd"}, {"contest_id": contests[0].contest_id, "limit": 500}):
                denied = await app.execute("election_data_results", forged)
                assert denied.error.code == "VALIDATION_FAILED"
    asyncio.run(run())


def test_read_workflow_over_real_mcp_subprocess(configured):
    base, _, config = configured

    async def run():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "personal_lab", "mcp", "--profile", "election-data", "--state-dir", str(base / "state-mcp"),
                  "--config", str(config)], cwd=str(base), env=_environment())
        async with Client(parameters) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert TOOLS <= tools.keys()
            assert not any(name.startswith(("eu5_", "ck3_", "election_forecast")) for name in tools)
            for name in TOOLS:
                assert tools[name].annotations.read_only_hint is True
                assert tools[name].input_schema["additionalProperties"] is False

            async def call(name, arguments):
                response = await client.call_tool(name, arguments)
                assert not response.is_error, response
                return response.structured_content["data"]

            status = await call("election_data_status", {})
            assert status["configured"] and status["active_release_id"]
            assert {source["source_id"] for source in status["sources"]} == {
                "mit-county-president", "fec-federal-elections-2024-president", "nara-electoral-college-2024",
                "census-cb-2020-20m"}
            nc = (await call("election_data_contests", {"state": "NC"}))["contests"][0]
            county = await call("election_data_results", {"contest_id": nc["contest_id"], "level": "county"})
            alamance = next(unit for unit in county["units"] if unit["label"] == "Alamance")
            trace = await call("election_data_provenance", {"derivation_id": alamance["provenance_ids"][0]})
            assert trace["total"] == 14 and trace["inputs"][0]["artifact_sha256"]
            view = await call("election_data_map", {"contest_id": nc["contest_id"]})
            assert view["asset"]["path"].endswith("counties.geojson") and view["features"]
            issues = await call("election_data_issues", {"severity": "warning"})
            assert issues["total"] >= 1 and all(issue["decision_id"] for issue in issues["issues"])
    asyncio.run(run())
