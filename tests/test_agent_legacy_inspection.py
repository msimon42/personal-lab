"""Legacy MCP inspection retains the current source policy and captured scope."""
import asyncio
import json

from mcp import types
from mcp.client import Client as MCPClient
from agent_test_support import call_tool, result_data
import pytest

from backend.agent import workflow as workflow_module
from backend.agent.approvals import sign, signing_key
from backend.agent.issues import CapturedSources
from personal_lab.mcp_server import build_server_for_service
from backend.app.store import Conflict
from test_agent_workflow import answer, proposal, setup as setup


def _issue(setup, *, localization=False):
    service, _, _, index_id, _ = setup
    return next(issue for issue in service.store.get("agent_index", index_id)["issues"] if
                issue["target"].endswith(":label" if localization else "/call_ally_interaction"))


def _exclude(service, pattern):
    service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": [pattern]}})


def _change_scope(setup, change):
    service, _, workspace, index_id, _ = setup
    if change == "generation":
        service.store.put("workspace", {**workspace, "generation": "a-new-generation"})
    elif change == "index_revision":
        index = service.store.get("agent_index", index_id)
        service.store.put("agent_index", {**index, "summary": {**index["summary"], "all_groups": 999}})
    else:
        scan = service.store.get("scan", workspace["generation"])
        service.store.put("scan", {**scan, "workspace_id": "a-different-workspace"})


@pytest.mark.parametrize("excerpt", [False, True])
def test_inspect_redacts_all_derived_content_if_any_contributor_is_excluded(setup, monkeypatch, excerpt):
    service, flow, workspace, index_id, _ = setup
    issue = _issue(setup, localization=True)
    _exclude(service, "localization/english/one_l_english.yml")
    visible = next(number for number, source in enumerate(issue["sources"])
                   if source["path"].endswith("two_l_english.yml"))
    monkeypatch.setattr(CapturedSources, "raw", lambda *args: pytest.fail("Excluded observations must not read source bytes"))
    result = flow.inspect(workspace["id"], index_id, issue["id"], visible if excerpt else None)
    assert result == {"issue_id": issue["id"], "excluded": True}
    assert "label" not in json.dumps(result) and "localization" not in json.dumps(result)


def test_legacy_list_keeps_excluded_observations_without_content_or_search_matches(setup):
    service, flow, workspace, index_id, _ = setup
    index = service.store.get("agent_index", index_id)
    excluded_ids = {issue["id"] for issue in index["issues"]
                    if any(source["path"].startswith("localization/") for source in issue["sources"])}
    _exclude(service, "localization/*")
    result = flow.issues(workspace["id"], index_id, include_equal=True, include_internal=True, include_metadata=True)
    assert result["total"] == result["summary"]["all_groups"] == len(index["issues"])
    placeholders = [issue for issue in result["issues"] if issue.get("excluded")]
    assert {issue["id"] for issue in placeholders} == excluded_ids
    assert result["summary"]["excluded_groups"] == result["coverage"]["excluded_groups"] == len(excluded_ids)
    assert all(issue["comparison"] == "unknown" and not issue["mods"] and not issue["repair_available"]
               for issue in placeholders)
    assert "label" not in json.dumps(result) and "repeated" not in json.dumps(result)
    assert "one_l_english" not in json.dumps(result)
    searched = flow.issues(workspace["id"], index_id, query="label", include_internal=True)
    assert searched["total"] == 0 and searched["issues"] == []
    assert searched["coverage"]["excluded_groups"] == len(excluded_ids)
    seen, offset = [], 0
    while True:
        page = flow.issues(workspace["id"], index_id, include_equal=True, include_internal=True,
                           include_metadata=True, offset=offset, limit=1)
        seen.extend(issue["id"] for issue in page["issues"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert seen == [issue["id"] for issue in result["issues"]]


def test_valid_legacy_excerpt_has_exact_hash_byte_span_and_continuation(setup):
    service, flow, workspace, index_id, _ = setup
    issue = _issue(setup)
    source = issue["sources"][0]
    scan = service.store.get("scan", workspace["generation"])
    raw = CapturedSources(service.store, scan).raw(source["owner"], source["path"])
    result = flow.inspect(workspace["id"], index_id, issue["id"], source_number=0, offset=2, limit=7)
    start, end = source["span"]
    assert result["excerpt"]["sha256"] == source["sha256"]
    assert result["excerpt"]["span"] == [start + 2, min(start + 9, end)]
    assert result["excerpt"]["text"] == raw[start + 2:min(start + 9, end)].decode()
    assert result["excerpt"]["next_offset"] == 9
    assert "excerpt" not in flow.inspect(workspace["id"], index_id, issue["id"])


def test_legacy_excerpt_rejects_changed_saved_blob(setup):
    service, flow, workspace, index_id, _ = setup
    issue = _issue(setup)
    checksum = issue["sources"][0]["sha256"]
    service.store.fs.write_bytes(f"blobs/sha256/{checksum[:2]}/{checksum}", b"PRIVATE_CORRUPTED_CONTENT")
    with pytest.raises(ValueError, match="saved copy changed"):
        flow.inspect(workspace["id"], index_id, issue["id"], source_number=0)


def test_legacy_excerpt_refuses_policy_change_during_blob_read(setup, monkeypatch):
    service, flow, workspace, index_id, _ = setup
    issue = _issue(setup)
    original = CapturedSources.raw

    def change_after_read(reader, *args):
        raw = original(reader, *args)
        _exclude(service, "common/character_interactions/*")
        return raw

    monkeypatch.setattr(CapturedSources, "raw", change_after_read)
    with pytest.raises(Conflict, match="Source permissions changed"):
        flow.inspect(workspace["id"], index_id, issue["id"], source_number=0)


@pytest.mark.parametrize("change", ["generation", "index_revision", "scan_scope"])
def test_legacy_excerpt_refuses_scope_change_during_blob_read(setup, monkeypatch, change):
    _, flow, workspace, index_id, _ = setup
    issue = _issue(setup)
    original = CapturedSources.raw

    def change_after_read(reader, *args):
        raw = original(reader, *args)
        _change_scope(setup, change)
        return raw

    monkeypatch.setattr(CapturedSources, "raw", change_after_read)
    with pytest.raises(Conflict):
        flow.inspect(workspace["id"], index_id, issue["id"], source_number=0)


@pytest.mark.parametrize("change", ["policy", "generation", "index_revision", "scan_scope"])
def test_legacy_list_refuses_mid_response_policy_or_scope_changes(setup, monkeypatch, change):
    service, flow, workspace, index_id, _ = setup
    original = workflow_module.query_index

    def change_after_query(index, **query):
        result = original(index, **query)
        if change == "policy":
            _exclude(service, "*")
        else:
            _change_scope(setup, change)
        return result

    monkeypatch.setattr(workflow_module, "query_index", change_after_query)
    with pytest.raises(Conflict):
        flow.issues(workspace["id"], index_id)


def test_legacy_inspect_mcp_error_contains_no_excerpt_after_policy_change(setup, monkeypatch):
    service, _, workspace, index_id, _ = setup
    issue = _issue(setup)
    original = CapturedSources.raw

    def change_after_read(reader, *args):
        raw = original(reader, *args)
        _exclude(service, "common/character_interactions/*")
        return raw

    monkeypatch.setattr(CapturedSources, "raw", change_after_read)


    async def run():
        async with MCPClient(build_server_for_service(service), mode="legacy") as session:
            response = await call_tool(session, "inspect_change", {"workspace_id": workspace["id"],
                "index_id": index_id, "issue_id": issue["id"], "source_number": 0})
            assert response.is_error
            text = "\n".join(content.text for content in response.content if content.type == "text")
            assert "Source permissions changed" in text
            assert "call_ally_interaction" not in text and "first = yes" not in text
            assert "excerpt" not in text

    asyncio.run(run())


def test_excluded_preparation_reads_no_source_and_saves_no_proposal(setup, monkeypatch):
    service, _, _, _, _ = setup
    _exclude(service, "common/character_interactions/*")
    monkeypatch.setattr(CapturedSources, "raw", lambda *args: pytest.fail("Excluded preparation must not read sources"))
    with pytest.raises(ValueError, match="source policy excludes"):
        proposal(setup)
    assert not service.store.list("agent_proposal") and not service.store.list("agent_question")
    assert not (service.root / "agent/changes").exists()


@pytest.mark.parametrize("change", ["policy", "generation", "index_revision", "scan_scope"])
def test_preparation_rechecks_policy_and_scope_before_persisting(setup, monkeypatch, change):
    service, _, _, _, _ = setup
    original = CapturedSources.raw
    changed = False

    def change_after_read(reader, *args):
        nonlocal changed
        raw = original(reader, *args)
        if not changed:
            changed = True
            if change == "policy":
                _exclude(service, "common/character_interactions/*")
            else:
                _change_scope(setup, change)
        return raw

    monkeypatch.setattr(CapturedSources, "raw", change_after_read)
    with pytest.raises(ValueError, match="changed|stale|does not belong"):
        proposal(setup)
    assert not service.store.list("agent_proposal") and not service.store.list("agent_question")
    assert not (service.root / "agent/changes").exists()


def test_excluded_saved_choices_cannot_be_read_or_asked_about(setup):
    service, flow, workspace, _, _ = setup
    draft = proposal(setup)
    _exclude(service, "common/character_interactions/*")
    with pytest.raises(ValueError, match="source policy excludes"):
        flow.proposal(workspace["id"], draft["id"])
    for purpose in ("choose", "test", "enable"):
        with pytest.raises(ValueError, match="source policy excludes"):
            flow.challenge(workspace["id"], draft["id"], purpose)
    assert not service.store.list("agent_question")


@pytest.mark.parametrize("change", ["policy", "revision", "scope"])
def test_saved_choice_rendering_rechecks_policy_revision_and_scope(setup, monkeypatch, change):
    service, flow, workspace, _, _ = setup
    draft = proposal(setup)
    original = service.store.get
    reads = 0
    workspace_reads = 0

    def change_before_final_read(kind, identifier):
        nonlocal reads, workspace_reads
        if change == "scope" and kind == "workspace" and identifier == workspace["id"]:
            workspace_reads += 1
            if workspace_reads == 2:
                service.store.put("workspace", {**workspace, "target_platform": "windows"})
        if kind == "agent_proposal" and identifier == draft["id"]:
            reads += 1
            if reads == 2:
                if change == "policy":
                    _exclude(service, "*")
                elif change == "revision":
                    service.store.put("agent_proposal", {**original(kind, identifier), "state": "changed"})
        return original(kind, identifier)

    monkeypatch.setattr(service.store, "get", change_before_final_read)
    with pytest.raises(Conflict):
        flow.proposal(workspace["id"], draft["id"])


def test_policy_change_before_question_creation_creates_no_native_challenge(setup, monkeypatch):
    service, flow, workspace, _, _ = setup
    draft = proposal(setup)
    original = workflow_module.secrets.token_urlsafe

    def change_before_nonce(*args):
        _exclude(service, "*")
        return original(*args)

    monkeypatch.setattr(workflow_module.secrets, "token_urlsafe", change_before_nonce)
    with pytest.raises(Conflict, match="Source permissions changed"):
        flow.challenge(workspace["id"], draft["id"])
    assert not service.store.list("agent_question")


def test_source_exclusion_invalidates_pending_choice_without_recording_approval(setup):
    service, flow, workspace, _, _ = setup
    draft = proposal(setup)
    question = flow.challenge(workspace["id"], draft["id"])
    value = {"question_id": question["id"], "nonce": question["nonce"],
        "proposal_hash": question["proposal_hash"], "action": "accept", "choice": draft["options"][0]["id"]}
    signature = sign(signing_key(service.store), value)
    _exclude(service, "*")
    with pytest.raises(ValueError, match="source policy excludes"):
        flow.answer(value, signature)
    assert service.store.get("agent_question", question["id"])["state"] == "pending"
    assert service.store.get("agent_proposal", draft["id"])["state"] == "awaiting_choice"
    assert not (service.root / "agent/changes").exists()


@pytest.mark.parametrize("during_bundle", [False, True])
def test_excluded_approved_sources_cannot_write_inactive_files(setup, monkeypatch, during_bundle):
    service, flow, workspace, _, _ = setup
    draft = proposal(setup)
    answer(setup, draft["id"], selection=draft["options"][0]["id"])
    if during_bundle:
        original = workflow_module.ck3_bundle_files

        def change_after_bundle(*args, **kwargs):
            files = original(*args, **kwargs)
            _exclude(service, "*")
            return files

        monkeypatch.setattr(workflow_module, "ck3_bundle_files", change_after_bundle)
    else:
        _exclude(service, "*")
        monkeypatch.setattr(workflow_module, "ck3_bundle_files", lambda *args, **kwargs:
                            pytest.fail("Excluded approved sources must not materialize output"))
    with pytest.raises(ValueError, match="source policy excludes|Source permissions changed"):
        flow.apply(workspace["id"], draft["id"])
    assert service.store.get("agent_proposal", draft["id"])["state"] == "approved"
    assert not (service.root / "agent/changes").exists()


def test_fresh_native_undo_can_retire_excluded_inactive_artifact_without_source_metadata(setup, monkeypatch):
    service, flow, workspace, _, _ = setup
    draft = proposal(setup)
    answer(setup, draft["id"], selection=draft["options"][0]["id"])
    flow.apply(workspace["id"], draft["id"])
    prepared = service.store.get("agent_proposal", draft["id"])
    before = {file["path"]: service.store.fs.read_bytes(prepared["applied_path"] + "/" + file["path"])
              for file in prepared["files"]}
    _exclude(service, "*")
    asked = []
    operations = []
    original_request = service.agent_request
    def tracked_request(operation, payload):
        operations.append(operation)
        return original_request(operation, payload)
    monkeypatch.setattr(service, "agent_request", tracked_request)


    async def answer_from_user(context, params):
        asked.append(params)
        return types.ElicitResult(action="accept", content={"choice": "Yes, continue"})

    async def run():
        async with MCPClient(
                build_server_for_service(service), mode="legacy", elicitation_callback=answer_from_user) as session:
            response = await call_tool(session, "undo_selected_changes", {
                "workspace_id": workspace["id"], "proposal_id": draft["id"]})
            assert not response.is_error, response
            result = await result_data(session, response)
            while "job_id" in result:
                response = await call_tool(session, "get_progress", {"job_id": result["job_id"]})
                assert not response.is_error, response
                progress = await result_data(session, response)
                if progress["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    assert progress["state"] == "succeeded", progress
                    result = progress["result"]
                    break
                await asyncio.sleep(.01)
            assert result["state"] == "undone"

    asyncio.run(asyncio.wait_for(run(), timeout=15))
    assert len(asked) == 1 and "proposal" not in operations
    assert "Unofficial Patch" not in asked[0].message and "More Interactive Vassals" not in asked[0].message
    assert "call_ally" not in asked[0].message
    question = service.store.list("agent_question")[0]
    receipt = flow.answer(question["answer"], sign(signing_key(service.store), question["answer"]))
    assert receipt == {"proposal_id": draft["id"], "state": "undone", "enabled": False}
    assert {file["path"]: service.store.fs.read_bytes(prepared["applied_path"] + "/" + file["path"])
            for file in prepared["files"]} == before
