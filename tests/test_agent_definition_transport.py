"""Complete declaration retrieval through actual headless MCP and immutable snapshots."""
import asyncio

from mcp.client import Client as MCPClient
from agent_test_support import call_tool, result_data
import pytest

from personal_lab.mcp_server import build_server_for_service
from backend.agent.workflow import AgentWorkflow
from backend.app.service import Service


@pytest.fixture
def captured(tmp_path):
    originals = tmp_path / "originals"
    before = {}
    for number in range(12):
        path = originals / f"mod-{number:02}" / "common/traits/shared.txt"
        path.parent.mkdir(parents=True)
        raw = f"shared_trait = {{ diplomacy = {number} }}\n".encode()
        if number == 11:
            raw += b"shared_trait = { diplomacy = 99 }\n"
        path.write_bytes(raw)
        before[path] = raw
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"name": "Paged physical declarations", "game_id": "ck3",
            "target_platform": "macos", "mode": "game", "sources": [
                {"owner": f"mod-{number:02}", "path": str(originals / f"mod-{number:02}")} for number in range(12)]})
        scan = service._scan(workspace, {}, lambda *args: None)
        workspace = service.store.get("workspace", workspace["id"])
        index = AgentWorkflow(service).index(workspace, scan, lambda *args: None)
        yield service, {"workspace_id": workspace["id"], "index_id": index["index_id"]}
        assert {path: path.read_bytes() for path in before} == before
        assert not service.store.list("agent_proposal")
        assert not (service.store.root / "active-release.json").exists()
    finally:
        service.close()

def test_mcp_follows_symbol_preview_to_every_declaration_and_honors_owner(captured):
    service, scope = captured

    async def run():
        async with MCPClient(build_server_for_service(service), mode="legacy") as session:
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            declared = tools["ck3_get_definition_contributions"]
            assert declared.annotations.read_only_hint and not declared.annotations.destructive_hint
            assert declared.input_schema["properties"]["limit"]["maximum"] == 50

            async def tool(name, arguments):
                result = await call_tool(session, name, arguments)
                assert not result.is_error, result
                return await result_data(session, result)

            found = await tool("search_sources", {**scope, "query": "shared_trait", "kind": "symbol"})
            definition = found["results"][0]
            assert found["total"] == 1 and found["next_offset"] is None
            assert definition["occurrence_count"] == 13 and len(definition["occurrences"]) == 8
            occurrences = list(definition["occurrences"])
            next_request = definition["occurrences_continuation"]
            assert next_request is not None
            while next_request is not None:
                page = await tool("get_definition_contributions", next_request)
                assert page["occurrence_count"] == 13
                occurrences.extend(page["occurrences"])
                next_request = page["occurrences_continuation"]
            assert len(occurrences) == 13
            assert len({(item["source_id"], tuple(item["span"]), item["ordinal"]) for item in occurrences}) == 13
            assert {item["owner"] for item in occurrences} == {f"mod-{number:02}" for number in range(12)}

            filtered = await tool("search_sources", {**scope, "query": "shared_trait", "kind": "symbol",
                                                       "owner": "mod-11", "path_prefix": "common/traits/"})
            selected = filtered["results"][0]
            assert selected["occurrence_count"] == 2
            assert {item["owner"] for item in selected["occurrences"]} == {"mod-11"}
            assert selected["occurrences_continuation"] is None

    asyncio.run(run())


def test_contribution_tool_requires_profile_capability_and_strict_bounded_arguments(captured):
    service, scope = captured
    valid = {**scope, "target": "game/trait/shared_trait"}
    async def run():
        async with MCPClient(build_server_for_service(service), mode="legacy") as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "ck3_get_definition_contributions" in names
            assert "get_definition_contributions" not in names
            assert not any(name.endswith(("approve", "shell", "sql")) for name in names)
            for extra in ({"limit": 51}, {"offset": -1}, {"continuation": "x" * 4097}, {"write_path": "/original"}):
                response = await call_tool(client, "get_definition_contributions", {**valid, **extra})
                assert response.is_error, response
            response = await call_tool(client, "get_definition_contributions", valid)
            result = await result_data(client, response)
            assert result["occurrence_count"] == 13
            denied = await call_tool(client, "get_definition_contributions", {**valid, "workspace_id": "foreign"})
            assert denied.is_error
    asyncio.run(run())
