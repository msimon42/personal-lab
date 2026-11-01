"""Saved conversation disclosure follows current policy without deleting history."""
import asyncio
import concurrent.futures
import copy
import json
import threading
from types import SimpleNamespace

from mcp.client import Client as MCPClient
from agent_test_support import call_tool, result_data
import pytest

from backend.agent import context as context_module
from backend.agent.context import AssistantContext
from personal_lab.mcp_server import build_server_for_service
from backend.agent.session_projection import BINDINGS_KEY, SessionProjection
from backend.app.service import Service
from backend.app.store import Conflict
from test_agent_workflow import setup as setup, proposal


FIELDS = ("goal", "notes", "next_step")
TEXT = {field: "PRIVATE_SYNTHETIC_" + field.upper() for field in FIELDS}


def save_text(setup):
    service, _, workspace, _, _ = setup
    flow = AssistantContext(service)
    context = flow.get(selection="resume", workspace_id=workspace["id"])
    saved = flow.remember(context["id"], **TEXT)
    return flow, context, saved


def exclude(service, patterns=None):
    service.store.put("configuration", {"id": "inference-policy", "policy": {
        **service.inference_policy(), "excluded_patterns": ["*"] if patterns is None else patterns}})


@pytest.mark.parametrize("operation", ["context", "session", "remember", "cached_context"])
def test_saved_source_text_is_redacted_on_every_public_path_without_erasing_raw_history(setup, operation):
    service, workflow, workspace, _, _ = setup
    flow, context, saved = save_text(setup)
    cached = flow.get(selection="resume", workspace_id=workspace["id"])
    if operation == "cached_context":
        service.store.put("agent_context", {**service.store.get("agent_context", cached["id"]), "state": "needs_selection"})
    counts = {kind: len(service.store.list(kind)) for kind in ("job", "scan", "agent_index")}
    exclude(service)
    if operation == "context":
        public = flow.get(selection="resume", workspace_id=workspace["id"])["conversation"]
    elif operation == "session":
        public = service.agent_request("session", {"workspace_id": workspace["id"]})
    elif operation == "remember":
        public = flow.remember(context["id"], reviewed_issue_ids=[])
    else:
        public = flow.request_check(cached["id"])["context"]["conversation"]
    assert all(public[field] is None for field in FIELDS)
    assert set(public["text_disclosure"]["redacted_fields"]) == set(FIELDS)
    assert not any(text in json.dumps(public) for text in TEXT.values())
    retained = workflow.session(workspace["id"])
    assert retained["id"] == saved["id"]
    assert all(retained[field] == TEXT[field] for field in FIELDS)
    assert {kind: len(service.store.list(kind)) for kind in counts} == counts


def test_goal_only_updates_and_new_check_goal_do_not_relabel_old_notes(setup, monkeypatch):
    service, workflow, workspace, _, _ = setup
    flow, context, _ = save_text(setup)
    before = copy.deepcopy(workflow.session(workspace["id"])[BINDINGS_KEY])
    exclude(service)
    public = flow.remember(context["id"], goal="A new player objective")
    assert public["goal"] == "A new player objective" and public["notes"] is None and public["next_step"] is None
    after = workflow.session(workspace["id"])[BINDINGS_KEY]
    assert after["notes"] == before["notes"] and after["next_step"] == before["next_step"]
    assert after["goal"]["policy_hash"] != before["goal"]["policy_hash"]
    monkeypatch.setattr(service, "command", lambda request: {"job_id": "not-executed"})
    flow.request_check(context["id"], goal="Another fresh objective")
    final = workflow.public_session(workspace["id"])
    assert final["goal"] == "Another fresh objective" and final["notes"] is None and final["next_step"] is None
    assert workflow.session(workspace["id"])[BINDINGS_KEY]["notes"] == before["notes"]


def test_legacy_notes_are_readable_without_exclusions_and_hidden_with_exclusions(setup):
    service, workflow, workspace, _, _ = setup
    raw = workflow.session(workspace["id"])
    service.store.put("agent_session", {**raw, **TEXT})
    plain = workflow.public_session(workspace["id"])
    assert plain["notes"] == TEXT["notes"]
    assert plain["text_disclosure"]["fields"]["notes"]["provenance"] == "legacy_unbound"
    exclude(service)
    hidden = workflow.public_session(workspace["id"])
    assert all(hidden[field] is None for field in FIELDS)
    assert workflow.session(workspace["id"])["notes"] == TEXT["notes"]


def test_historical_notes_keep_their_exact_original_scan_and_index(setup):
    service, workflow, workspace, index_id, _ = setup
    flow, _, saved = save_text(setup)
    first_scan = workspace["generation"]
    current = service.store.get("scan", first_scan)
    newer = service.store.put("scan", {**current, "id": "new-synthetic-generation"})
    service.store.put("workspace", {**workspace, "generation": newer["id"]})
    public = workflow.public_session(workspace["id"])
    assert public["notes"] == TEXT["notes"]
    assert public["text_disclosure"]["fields"]["notes"]["state"] == "historical"
    assert (public["notes_scan_id"], public["notes_index_id"]) == (first_scan, index_id)
    context = flow.get(selection="resume", workspace_id=workspace["id"])
    assert not context["conversation"]["applies_to_current_scan"]
    assert workflow.session(workspace["id"])["id"] == saved["id"]


def test_next_step_only_on_new_scan_does_not_relabel_older_notes_current(setup):
    service, workflow, workspace, index_id, _ = setup
    flow, _, _ = save_text(setup)
    original = workflow.session(workspace["id"])
    first_scan = service.store.get("scan", workspace["generation"])
    new_scan = service.store.put("scan", {**first_scan, "id": "new-synthetic-scan"})
    first_index = service.store.get("agent_index", index_id)
    service.store.put("agent_index", {**first_index, "id": "new-synthetic-index", "scan_id": new_scan["id"]})
    service.store.put("workspace", {**workspace, "generation": new_scan["id"]})
    current = flow.get(selection="resume", workspace_id=workspace["id"])
    changed = flow.remember(current["id"], next_step="A step about the new capture")
    assert changed["notes_scan_id"] == original["notes_scan_id"]
    resumed = flow.get(selection="resume", workspace_id=workspace["id"])["conversation"]
    assert resumed["notes"] == TEXT["notes"] and not resumed["applies_to_current_scan"]
    assert resumed["text_disclosure"]["fields"]["notes"]["state"] == "historical"
    assert resumed["text_disclosure"]["fields"]["next_step"]["state"] == "available"


@pytest.mark.parametrize("change", ["scan_owner", "index_owner", "scan_revision", "missing_scan", "target", "text"])
def test_missing_changed_or_foreign_saved_text_scope_is_not_disclosed(setup, change):
    service, workflow, workspace, index_id, _ = setup
    save_text(setup)
    if change == "scan_owner":
        record = service.store.get("scan", workspace["generation"])
        service.store.put("scan", {**record, "workspace_id": "another-workspace"})
    elif change == "index_owner":
        record = service.store.get("agent_index", index_id)
        service.store.put("agent_index", {**record, "workspace_id": "another-workspace"})
    elif change == "scan_revision":
        record = service.store.get("scan", workspace["generation"])
        service.store.put("scan", {**record, "manifest_hash": "changed"})
    elif change == "missing_scan":
        service.store.connection.execute("DELETE FROM records WHERE id=?", (workspace["generation"],))
        service.store.connection.commit()
    elif change == "target":
        service.store.put("workspace", {**workspace, "target_platform": "windows"})
    else:
        raw = workflow.session(workspace["id"])
        service.store.put("agent_session", {**raw, "notes": "UNBOUND_REPLACEMENT_TEXT"})
    public = workflow.public_session(workspace["id"])
    assert public["notes"] is None
    assert public["text_disclosure"]["fields"]["notes"]["state"] == "redacted"


def test_public_resume_uses_headers_only_and_creates_no_check_jobs(setup, monkeypatch):
    service, workflow, workspace, _, _ = setup
    flow, _, _ = save_text(setup)
    original_get = service.store.get
    def get(kind, identifier):
        assert kind not in {"scan", "agent_index"}, "Saved text projection decoded a full source record"
        return original_get(kind, identifier)
    monkeypatch.setattr(service.store, "get", get)
    monkeypatch.setattr(service.store.fs, "read_bytes", lambda *args, **kwargs:
                        pytest.fail("Saved text projection read a source blob"))
    assert workflow.public_session(workspace["id"])["notes"] == TEXT["notes"]
    assert flow.get(selection="resume", workspace_id=workspace["id"])["conversation"]["notes"] == TEXT["notes"]
    assert not service.store.list("job")


@pytest.mark.parametrize("operation", ["context", "session", "remember"])
@pytest.mark.parametrize("change", ["policy", "target", "source"])
def test_policy_or_scope_changes_during_projection_return_no_saved_text(setup, monkeypatch, operation, change):
    service, workflow, workspace, index_id, _ = setup
    flow, context, _ = save_text(setup)
    original_project = SessionProjection.project
    def change_after_projection(view, session):
        result = original_project(view, session)
        if change == "policy":
            exclude(service)
        elif change == "target":
            service.store.put("workspace", {**workspace, "target_platform": "windows"})
        else:
            source = service.store.get("agent_index", index_id)
            service.store.put("agent_index", {**source, "manifest_hash": "changed-during-projection"})
        return result
    monkeypatch.setattr(SessionProjection, "project", change_after_projection)
    with pytest.raises(Conflict):
        if operation == "context":
            flow.get(selection="resume", workspace_id=workspace["id"])
        elif operation == "session":
            workflow.public_session(workspace["id"])
        else:
            flow.remember(context["id"], goal="Must not escape this failed response")
    if operation == "remember":
        assert workflow.session(workspace["id"])["goal"] == TEXT["goal"]


@pytest.mark.parametrize("explicit_workspace", [False, True])
def test_mcp_resume_redacts_saved_notes_after_exclusion_without_scanning(setup, monkeypatch, explicit_workspace):
    service, _, workspace, _, _ = setup
    save_text(setup)
    exclude(service)
    monkeypatch.setattr(context_module, "sys", SimpleNamespace(platform="darwin"))
    async def run():
        async with MCPClient(build_server_for_service(service), mode="legacy") as session:
            response = await call_tool(session, "resume_conversation", {"workspace_id": workspace["id"]} if explicit_workspace else {})
            assert not response.is_error, response
            result = await result_data(session, response)
            assert not any(text in json.dumps(result) for text in TEXT.values())
            public = result if explicit_workspace else result["conversation"]
            assert public["notes"] is None
    asyncio.run(run())
    assert not service.store.list("job")


def test_headless_session_and_remember_responses_honor_saved_text_policy(tmp_path):
    source = tmp_path / "originals"
    source.mkdir()
    (source / "descriptor.mod").write_bytes(b'name="Synthetic"')
    before = (source / "descriptor.mod").read_bytes()
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "mode": "game", "target_platform": "macos",
            "name": "Saved headless text", "sources": [{"owner": "one", "path": str(source)}]})
        context = AssistantContext(service).get(workspace_id=workspace["id"])
        async def run():
            async with MCPClient(build_server_for_service(service), mode="legacy") as client:
                await result_data(client, await call_tool(client, "remember_progress", {"context_id": context["id"], **TEXT}))
                exclude(service)
                public = await result_data(client, await call_tool(client, "resume_conversation", {"workspace_id": workspace["id"]}))
                assert public["notes"] is None
                remembered = await result_data(client, await call_tool(client, "remember_progress", {
                    "context_id": context["id"], "goal": "Current user objective"}))
                assert remembered["notes"] is None and remembered["goal"] == "Current user objective"
        asyncio.run(run())
        assert (source / "descriptor.mod").read_bytes() == before
    finally:
        service.close()


def test_internal_progress_preserves_notes_and_their_original_bindings(setup):
    _, workflow, workspace, _, _ = setup
    save_text(setup)
    original = workflow.session(workspace["id"])
    workflow.progress(workspace["id"], "reviewing", proposal_id="an-opaque-proposal")
    updated = workflow.session(workspace["id"])
    assert all(updated[field] == TEXT[field] for field in FIELDS)
    assert updated[BINDINGS_KEY] == original[BINDINGS_KEY]


@pytest.mark.parametrize("operation", ["progress", "check"])
def test_concurrent_note_goal_and_workflow_updates_preserve_unrelated_fields(setup, monkeypatch, operation):
    service, workflow, workspace, _, _ = setup
    flow, context, _ = save_text(setup)
    barrier = threading.Barrier(3)
    monkeypatch.setattr(service, "command", lambda request: {"job_id": "not-executed"})
    def goal():
        barrier.wait(timeout=5)
        if operation == "check":
            return flow.request_check(context["id"], goal="Updated objective")
        return flow.remember(context["id"], goal="Updated objective")
    def notes():
        barrier.wait(timeout=5)
        return flow.remember(context["id"], notes="Updated explanation")
    def progress():
        barrier.wait(timeout=5)
        return workflow.progress(workspace["id"], "reviewing", proposal_id="opaque-progress")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(function) for function in (goal, notes, progress)]
        for future in futures:
            future.result(timeout=10)
    saved = workflow.public_session(workspace["id"])
    assert saved["goal"] == "Updated objective" and saved["notes"] == "Updated explanation"
    assert saved["next_step"] == TEXT["next_step"] and saved["proposal_id"] == "opaque-progress"
    assert not saved["text_disclosure"]["redacted_fields"]


def test_explicit_mcp_resume_handles_excluded_pending_choices_without_losing_saved_progress(setup):
    service, workflow, workspace, _, _ = setup
    save_text(setup)
    draft = proposal(setup)
    exclude(service)
    async def run():
        async with MCPClient(build_server_for_service(service), mode="legacy") as session:
            response = await call_tool(session, "resume_conversation", {"workspace_id": workspace["id"]})
            assert not response.is_error, response
            result = await result_data(session, response)
            assert result["proposal"] == {"state": "requires_fresh_review"}
            assert result["proposal_id"] == draft["id"]
            assert result["notes"] is None and result["goal"] is None
            assert "Unofficial Patch" not in json.dumps(result) and "More Interactive Vassals" not in json.dumps(result)
    asyncio.run(run())
    assert workflow.session(workspace["id"])["notes"] == TEXT["notes"]
    assert not service.store.list("job") and not service.store.list("agent_question")
