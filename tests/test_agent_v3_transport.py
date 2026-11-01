"""Version-three conversations through the actual MCP request boundary."""
import asyncio

from mcp.client import Client as MCPClient
from agent_test_support import call_tool, result_data

from personal_lab.mcp_server import build_server_for_service
from test_agent_workflow import setup as setup


def test_complete_review_and_resume_through_mcp(setup):
    service, _, workspace, index_id, _ = setup


    async def run():
        async with MCPClient(build_server_for_service(service), mode="legacy") as session:
            args = {"workspace_id": workspace["id"], "index_id": index_id}

            async def tool(name, **extra):
                result = await call_tool(session, name, {**args, **extra})
                assert not result.is_error, result
                return await result_data(session, result)

            page = await tool("list_concerns")
            assert page["summary"]["raw_findings"] >= len(page["concerns"])
            for concern in page["concerns"]:
                if concern["review"]["state"] == "explained_from_files":
                    continue
                references = []
                continuation = None
                while True:
                    evidence = await tool("get_concern_evidence", concern_id=concern["id"], continuation=continuation)
                    references.extend(evidence["evidence_refs"])
                    for contribution in evidence["contributions"]:
                        assert contribution["continuation"] is None
                    continuation = evidence["continuation"]
                    if not continuation:
                        break
                recorded = await tool("record_concern_review", concern_id=concern["id"],
                    state="explained_from_files", explanation="These saved contributions differ in the displayed reward.",
                    player_impact="Which reward the game uses has not been established.", evidence_refs=references,
                    uncertainties=["No game behavior has been observed."],
                    expected_revision=concern["review"]["revision"])
                assert recorded["review"]["approval"] is False
                stale = await call_tool(session, "record_concern_review", {**args, "concern_id": concern["id"],
                    "state": "in_progress", "expected_revision": 0})
                assert stale.is_error
            report = await tool("get_investigation_report")
            assert report["summary"]["accounted_for"]
            assert report["summary"]["gameplay_compatibility"] == "not_established"
            assert report["summary"]["next_unfinished"] is None
            # A second client/conversation gets the saved conclusions without jobs.
        jobs = len(service.store.list("job"))
        async with MCPClient(build_server_for_service(service), mode="legacy") as resumed:
            result = await call_tool(resumed, "get_investigation_report", args)
            assert not result.is_error
            assert (await result_data(resumed, result))["summary"]["accounted_for"]
        assert len(service.store.list("job")) == jobs
        assert not service.store.list("agent_proposal")
        assert not service.store.list("agent_question")

    asyncio.run(run())


def test_v3_transport_rejects_overbudget_batch_and_fake_approval(setup):
    service, _, workspace, index_id, _ = setup


    async def run():
        async with MCPClient(build_server_for_service(service), mode="legacy") as session:
            args = {"workspace_id": workspace["id"], "index_id": index_id}
            response = await call_tool(session, "read_sources", {**args, "requests": [
                {"source_id": "not-a-capability", "limit": 8192} for _ in range(9)]})
            assert response.is_error and "65536" in response.content[0].text
            response = await call_tool(session, "record_concern_review", {**args, "concern_id": "invalid",
                "state": "approved", "explanation": "Everything works"})
            assert response.is_error
    asyncio.run(run())
