"""Actual MCP calls and trusted native responses against isolated direct services."""
import asyncio
import base64
from pathlib import Path
import shutil
import uuid

from mcp.client import Client
from mcp import types
import pytest
from pydantic import ValidationError

from personal_lab.mcp_server import build_server_for_service
from backend.agent import release
from backend.app.service import Service
from test_agent_workflow import setup as setup, proposal


def test_headless_boundary_rejects_model_approval(tmp_path):
    service = Service(tmp_path / "managed")
    try:
        with pytest.raises(ValidationError):
            service.agent_request("apply", {"workspace_id": "x", "proposal_id": "x", "approved": True})
    finally:
        service.close()


async def call(session, name, **arguments):
    name = name if name.startswith(("ck3_", "lab_")) else "ck3_" + name
    tool = next(item for item in (await session.list_tools()).tools if item.name == name)
    if "idempotency_key" in tool.input_schema.get("required", []):
        arguments.setdefault("idempotency_key", uuid.uuid4().hex)
    return await session.call_tool(name, arguments)


def test_native_choice_reaches_engine_but_model_cannot_supply_approval(setup):
    service, flow, workspace, *_ = setup
    draft = proposal(setup, b'call_ally_interaction = { effect = { first = yes second = yes } }')
    asked = []
    async def answer_from_user(context, params):
        asked.append(params)
        options = params.requested_schema["properties"]["choice"]["enum"]
        assert "Prepare the proposed combined version" in options
        assert all("keep_" not in option for option in options)
        return types.ElicitResult(action="accept", content={"choice": options[0]})
    async def run():
        async with Client(build_server_for_service(service), mode="legacy", elicitation_callback=answer_from_user) as session:
            tools = (await session.list_tools()).tools
            approval = next(tool for tool in tools if tool.name == "ck3_ask_about_changes")
            assert set(approval.input_schema["properties"]) == {"workspace_id", "proposal_id"}
            result = await call(session, "ask_about_changes", workspace_id=workspace["id"], proposal_id=draft["id"])
            assert not result.is_error, result
            assert result.structured_content["data"]["state"] == "approved"
            assert not any(tool.name in {"ck3_enable_selected_changes", "ck3_test_selected_changes"} for tool in tools)
    asyncio.run(run())
    assert len(asked) == 1
    assert flow.proposal(workspace["id"], draft["id"])["selected_option"] == "test_combined"


def test_missing_user_prompt_support_never_approves(setup):
    service, flow, workspace, *_ = setup
    draft = proposal(setup)
    async def run():
        async with Client(build_server_for_service(service)) as session:
            result = await call(session, "ask_about_changes", workspace_id=workspace["id"], proposal_id=draft["id"])
            assert result.structured_content["status"] == "needs_input"
            assert "lab decide" in result.structured_content["data"]["next_step"]
    asyncio.run(run())
    assert flow.proposal(workspace["id"], draft["id"])["state"] == "awaiting_choice"


def test_unrelated_combined_bytes_rejected_by_application_schema(tmp_path):
    service = Service(tmp_path / "managed")
    try:
        with pytest.raises(ValidationError):
            service.command({"command": "agent_prepare", "workspace_id": "x", "idempotency_key": "bad-options",
                "payload": {"index_id": "x", "issue_id": "x", "combined_base64": base64.b64encode(b"other={}").decode(),
                    "write_path": "/original"}})
        assert not (service.root / "agent/changes").exists()
    finally:
        service.close()


def test_mismatched_loaded_engine_blocks_preparation_and_user_prompt(setup):
    service, _, workspace, *_ = setup
    draft = proposal(setup)
    service.agent_release = {**service.agent_release, "fingerprint": "old-loaded-engine"}
    asked = []
    async def answer_from_user(context, params):
        asked.append(params)
        return types.ElicitResult(action="decline")
    async def run():
        async with Client(build_server_for_service(service), mode="legacy", elicitation_callback=answer_from_user) as session:
            for name, arguments in [
                ("prepare_call_allies_choices", {"workspace_id": workspace["id"], "index_id": draft["index_id"], "issue_id": draft["issue_id"]}),
                ("ask_about_changes", {"workspace_id": workspace["id"], "proposal_id": draft["id"]}),
                ("make_selected_files", {"workspace_id": workspace["id"], "proposal_id": draft["id"]}),
            ]:
                result = await call(session, name, **arguments)
                assert result.is_error and result.structured_content["error"]["code"] == "STALE_INPUT"
    asyncio.run(run())
    assert not asked and not service.store.list("agent_question")


def test_core_only_update_blocks_changes_but_saved_report_readable(setup, tmp_path, monkeypatch):
    service, _, workspace, *_ = setup
    draft = proposal(setup)
    package = tmp_path / "updated-backend"
    import backend
    shutil.copytree(Path(backend.__file__).parent, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    resolver = release.resources.files
    monkeypatch.setattr(release.resources, "files", lambda name: package if name == "backend" else resolver(name))
    before = release.release_info()
    assert before["fingerprint"] == service.agent_release["fingerprint"]
    parser = package / "engine/parser.py"
    parser.write_bytes(parser.read_bytes() + b"\n# Simulated installed update.\n")
    updated = release.release_info()
    assert updated["fingerprint"] != before["fingerprint"]
    assert updated["workflow_files"] == before["workflow_files"]
    asked = []
    async def answer_from_user(context, params):
        asked.append(params)
        return types.ElicitResult(action="decline")
    async def run():
        async with Client(build_server_for_service(service), mode="legacy", elicitation_callback=answer_from_user) as session:
            report = await call(session, "get_investigation_report", workspace_id=workspace["id"], index_id=draft["index_id"])
            assert not report.is_error and report.structured_content["data"]["summary"]["total_concerns"] > 0
            for name, arguments in [
                ("prepare_call_allies_choices", {"workspace_id": workspace["id"], "index_id": draft["index_id"], "issue_id": draft["issue_id"]}),
                ("ask_about_changes", {"workspace_id": workspace["id"], "proposal_id": draft["id"]}),
                ("make_selected_files", {"workspace_id": workspace["id"], "proposal_id": draft["id"]}),
            ]:
                result = await call(session, name, **arguments)
                assert result.is_error and result.structured_content["error"]["code"] == "STALE_INPUT"
    asyncio.run(run())
    assert not asked and not service.store.list("agent_question")
    assert not (service.store.root / "agent/changes").exists()


def test_tool_annotations_and_strict_schemas(tmp_path):
    service = Service(tmp_path / "managed")
    async def run():
        async with Client(build_server_for_service(service)) as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert len(tools) == 37
            assert tools["ck3_get_context"].input_schema["properties"]["selection"]["enum"] == ["current", "resume"]
            assert tools["ck3_get_concern_evidence"].input_schema["properties"]["limit"]["maximum"] == 4
            assert "needs_game_test" in tools["ck3_record_concern_review"].input_schema["properties"]["state"]["enum"]
            for name in ("read_source", "search_knowledge", "investigate_change", "get_progress", "read_sources", "list_concerns",
                         "get_concern_evidence", "get_investigation_report", "get_definition_contributions"):
                assert tools["ck3_" + name].annotations.read_only_hint
            for name in ("get_context", "check_playset", "remember_progress", "make_selected_files", "record_concern_review",
                         "find_missing_mod_candidates", "inspect_mod_candidate", "set_version_preference"):
                assert not tools["ck3_" + name].annotations.read_only_hint
            assert all(tool.annotations.open_world_hint is False for tool in tools.values())
            assert all(tool.output_schema for tool in tools.values())
    try:
        asyncio.run(run())
    finally:
        service.close()


def test_invalidated_question_cannot_be_signed_again_by_current_connector(setup):
    from backend.agent.approvals import sign, signing_key
    from toolkit_core.storage import Conflict
    service, flow, workspace, *_ = setup
    draft = proposal(setup)
    question = flow.challenge(workspace["id"], draft["id"])
    service.store.put("agent_question", {**question, "state": "invalidated"})
    answer = {"question_id": question["id"], "nonce": question["nonce"],
        "proposal_hash": question["proposal_hash"], "action": "accept", "choice": draft["options"][0]["id"]}
    with pytest.raises(Conflict, match="no longer pending"):
        flow.answer(answer, sign(signing_key(service.store), answer))
    assert flow.proposal(workspace["id"], draft["id"])["state"] == "awaiting_choice"
