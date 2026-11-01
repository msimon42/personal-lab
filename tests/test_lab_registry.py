"""Fresh composition and trusted extension/confirmation boundaries."""
import asyncio
import json
import subprocess
import sys

from mcp.client import Client
from mcp.types import ElicitResult
from pydantic import Field
import pytest

from personal_lab.bootstrap import create_application
from personal_lab.mcp_server import build_server
from toolkit_core.contracts import Empty, OperationSpec, StrictModel
from test_agent_workflow import setup as setup


def test_core_only_fresh_process_never_imports_domain(tmp_path):
    code = """
import builtins, json, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'backend' or name.startswith('backend.'):
        raise AssertionError('core-only imported game domain')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from personal_lab.bootstrap import create_application
with create_application(Path(sys.argv[1]), profile='core-only') as app:
    print(json.dumps(sorted(app.operations)))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path / "state")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["lab_artifact_read", "lab_capabilities", "lab_run_cancel", "lab_run_events", "lab_run_get"]


def test_unrelated_toolset_is_independent_and_default_disabled(tmp_path):
    async def run():
        with create_application(tmp_path / "test", profile="test-only") as app:
            async with Client(build_server(app)) as client:
                result = await client.call_tool("test_text_statistics", {"text": "one two\nthree"})
                assert not result.is_error
                assert result.structured_content["data"] == {"characters": 13, "words": 3, "lines": 2}
        with create_application(tmp_path / "core", profile="core-only") as app:
            assert "test_text_statistics" not in app.operations
    asyncio.run(run())


class Decision(StrictModel):
    state: str


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
def test_native_confirmation_uses_sdk_response_channel_across_protocols(tmp_path, mode, action):
    asked, committed = [], []

    async def run():
        with create_application(tmp_path / "state", profile="core-only") as app:
            async def operation(args, native):
                selected = await native.choose("question-immutable-1", "Choose this exact action", ["Do it", "Keep it"])
                if native.action == "accept" and selected == "Do it":
                    committed.append(selected)
                return Decision(state=native.action)
            app.operations["test_decision"] = OperationSpec("test_decision", "Test trusted choice", Empty,
                Decision, operation, requires_context=True)
            async def user_answer(context, params):
                asked.append(params)
                assert params.requested_schema["properties"]["choice"]["enum"] == ["Do it", "Keep it"]
                return ElicitResult(action=action, content={"choice": "Do it"} if action == "accept" else None)
            async with Client(build_server(app), mode=mode, elicitation_callback=user_answer) as client:
                tools = await client.list_tools()
                tool = next(row for row in tools.tools if row.name == "test_decision")
                assert "native_choice" not in tool.input_schema["properties"]
                forged = await client.call_tool("test_decision", {"native_choice": {"choice": "Do it"}})
                assert forged.is_error
                assert not asked and not committed
                result = await client.call_tool("test_decision", {})
                assert not result.is_error
                assert result.structured_content["data"]["state"] == action
    asyncio.run(run())
    assert len(asked) == 1
    assert committed == (["Do it"] if action == "accept" else [])


def test_missing_native_support_returns_terminal_route_without_receipt(tmp_path):
    async def run():
        with create_application(tmp_path / "state", profile="core-only") as app:
            async def operation(args, native):
                selected = await native.choose("question-1", "Choose", ["Yes"])
                assert selected is None
                return Decision(state="awaiting_operator")
            app.operations["test_decision"] = OperationSpec("test_decision", "Test trusted choice", Empty,
                Decision, operation, requires_context=True)
            async with Client(build_server(app)) as client:
                result = await client.call_tool("test_decision", {})
                assert result.structured_content["status"] == "needs_input"
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["legacy", "auto"])
@pytest.mark.parametrize("change", ["question", "message", "choices"])
def test_native_response_cannot_authorize_replaced_question_between_resolver_and_body(tmp_path, mode, change):
    from personal_lab.mcp_server import ChoiceContext
    committed = []

    async def run():
        with create_application(tmp_path / "state", profile="core-only") as app:
            async def operation(args, native):
                # Models an expiry/replacement or changed rendering at the
                # final application boundary, after the SDK resolved input.
                changed = isinstance(native, ChoiceContext) and native.action == "accept"
                question = "question-replacement" if changed and change == "question" else "question-original"
                message = "Changed candidate" if changed and change == "message" else "Original candidate"
                choices = ["Do it", "New alternative"] if changed and change == "choices" else ["Do it", "Keep it"]
                selected = await native.choose(question, message, choices)
                committed.append(selected)
                return Decision(state="committed")
            app.operations["test_decision"] = OperationSpec("test_decision", "Test exact question binding", Empty,
                Decision, operation, requires_context=True)
            async def user_answer(context, params):
                return ElicitResult(action="accept", content={"choice": "Do it"})
            async with Client(build_server(app), mode=mode, elicitation_callback=user_answer) as client:
                result = await client.call_tool("test_decision", {})
                assert result.is_error
                assert result.structured_content["error"]["code"] == "STALE_INPUT"
    asyncio.run(run())
    assert not committed


def test_disabled_domain_run_and_artifact_cannot_be_read_or_cancelled(tmp_path):
    async def run():
        with create_application(tmp_path / "state", profile="core-only") as app:
            app.store.put("job", {"id": "job-other", "game_id": "ck3", "state": "completed", "result": {"secret": "source text"}})
            app.store.append_event("job-other", {"result": {"secret": "source text"}})
            ref = app.artifacts.register_bytes(b"source text", "text/plain", scope={"toolset": "ck3"})
            for name, arguments in [
                ("lab_run_get", {"run_id": "job-other"}), ("lab_run_events", {"run_id": "job-other"}),
                ("lab_run_cancel", {"run_id": "job-other"}), ("lab_artifact_read", {"artifact_id": ref.id})]:
                result = await app.execute(name, arguments)
                assert result.error.code == "PATH_DENIED"
                assert "source text" not in result.model_dump_json()
    asyncio.run(run())


class LargeInput(StrictModel):
    workspace_id: str


class LargeData(StrictModel):
    text: str = Field(max_length=100000)


def test_oversized_result_and_generic_run_recheck_current_source_policy(tmp_path):
    async def run():
        with create_application(tmp_path / "state", profile="ck3-local") as app:
            workspace = app.service.create_workspace({"name": "Synthetic CK3 sources", "mode": "game", "game_id": "ck3",
                "target_platform": "macos"})
            secret = "private declaration " * 2400
            app.operations["ck3_large_test"] = OperationSpec("ck3_large_test", "Large saved evidence", LargeInput,
                LargeData, lambda args: LargeData(text=secret), read_only=True)
            result = await app.execute("ck3_large_test", {"workspace_id": workspace["id"]})
            assert result.data is None and len(result.artifacts) == 1
            assert len(result.model_dump_json().encode()) <= 32768
            artifact_id = result.artifacts[0].id
            allowed = await app.execute("lab_artifact_read", {"artifact_id": artifact_id})
            assert allowed.status == "ok"
            app.store.put("job", {"id": "job-old", "game_id": "ck3", "state": "completed", "workspace_id": workspace["id"],
                                  "result": {"secret": secret}})
            app.store.append_event("job-old", {"state": "completed", "result": {"secret": secret}})
            policy = app.service.inference_policy()
            policy["excluded_patterns"] = ["*"]
            policy.pop("key_configured", None)
            app.store.put("configuration", {"id": "inference-policy", "policy": policy})
            app.store.put("job", {"id": "job-matching", "game_id": "ck3", "state": "completed", "workspace_id": workspace["id"],
                "source_policy_hash": app._policy_digest(), "result": {"secret": secret}})
            app.store.append_event("job-matching", {"state": "completed", "result": {"secret": secret}})
            denied = await app.execute("lab_artifact_read", {"artifact_id": artifact_id})
            assert denied.error.code == "PATH_DENIED"
            for run_id in ("job-old", "job-matching"):
                for name in ("lab_run_get", "lab_run_events"):
                    result = await app.execute(name, {"run_id": run_id})
                    assert result.status == "ok"
                    assert "private declaration" not in result.model_dump_json()
    asyncio.run(run())


def test_whole_input_budget_and_error_payload_are_bounded(tmp_path):
    from toolkit_core.contracts import LabError
    from pydantic import JsonValue

    class Arguments(StrictModel):
        values: dict[str, JsonValue]

    async def run():
        with create_application(tmp_path / "state", profile="core-only") as app:
            called = []
            def handler(args):
                called.append(args)
                raise LabError("VALIDATION_FAILED", "\x01" * 100000, details={"large": "x" * 100000})
            app.operations["test_budget"] = OperationSpec("test_budget", "Test whole-input budget", Arguments, Decision, handler)
            oversized = await app.execute("test_budget", {"values": {"large": "x" * 262144}})
            assert oversized.error.code == "VALIDATION_FAILED" and not called
            error = await app.execute("test_budget", {"values": {}})
            assert called and error.error.details["details_omitted"]
            assert len(error.model_dump_json().encode()) <= 32768
    asyncio.run(run())


@pytest.mark.parametrize("phase", ["scope", "register", "small_handler"])
def test_response_never_relabels_source_bytes_to_a_new_policy(tmp_path, monkeypatch, phase):
    async def run():
        with create_application(tmp_path / "state", profile="ck3-local") as app:
            workspace = app.service.create_workspace({"name": "Managed test", "mode": "game", "game_id": "ck3",
                                                      "target_platform": "macos"})
            captured = app._policy_digest()
            secret = "PRIVATE SOURCE" if phase == "small_handler" else "PRIVATE SOURCE" * 4000

            def revoke():
                policy = app.service.inference_policy()
                policy.pop("key_configured", None)
                policy["excluded_patterns"] = ["*"]
                app.store.put("configuration", {"id": "inference-policy", "policy": policy})

            def handler(args):
                if phase == "small_handler":
                    revoke()
                return LargeData(text=secret)

            app.operations["ck3_large_test"] = OperationSpec("ck3_large_test", "Policy-bound evidence", LargeInput,
                LargeData, handler, read_only=True)
            if phase == "scope":
                original = app._scope
                def scope(toolset, args):
                    revoke()
                    return original(toolset, args)
                monkeypatch.setattr(app, "_scope", scope)
            elif phase == "register":
                original_register = app.artifacts.register_bytes
                def register(*args, **kwargs):
                    revoke()
                    return original_register(*args, **kwargs)
                monkeypatch.setattr(app.artifacts, "register_bytes", register)

            result = await app.execute("ck3_large_test", {"workspace_id": workspace["id"]})
            assert result.error.code == "STALE_INPUT"
            assert result.data is None and not result.artifacts
            assert "PRIVATE SOURCE" not in result.model_dump_json()
            artifacts = app.store.list("lab_artifact")
            if phase == "register":
                assert len(artifacts) == 1
                assert artifacts[0]["scope"]["policy_digest"] == captured
                assert app._policy_digest() != captured
                denied = await app.execute("lab_artifact_read", {"artifact_id": artifacts[0]["id"]})
                assert denied.error.code == "PATH_DENIED"
                assert "PRIVATE SOURCE" not in denied.model_dump_json()
            else:
                assert not artifacts
    asyncio.run(run())


@pytest.mark.parametrize("operation", ["ck3_get_context", "ck3_remember_progress", "ck3_resume_conversation"])
@pytest.mark.parametrize("changed", ["workspace", "index", "session"])
def test_same_key_cached_discussion_rechecks_source_scope_without_repeating_effect(setup, monkeypatch, operation, changed):
    from backend.agent.context import AssistantContext
    from personal_lab.bootstrap import LabApplication
    service, workflow, workspace, index_id, _ = setup
    context = AssistantContext(service).get(selection="resume", workspace_id=workspace["id"])
    secret = "PRIVATE_CACHE_SCOPE_NOTES"
    AssistantContext(service).remember(context["id"], notes=secret)
    app = LabApplication(service.root, profile="ck3-local", service=service)
    arguments = {"idempotency_key": "same-discussion-key"}
    if operation == "ck3_remember_progress":
        arguments.update(context_id=context["id"], notes=secret)
    else:
        arguments["workspace_id"] = workspace["id"]
        if operation == "ck3_get_context":
            arguments["selection"] = "resume"

    async def run():
        async with Client(build_server(app)) as client:
            first = await client.call_tool(operation, arguments)
            assert not first.is_error
            assert secret in json.dumps(first.structured_content)
            original_get = service.store.get
            def headers_only(kind, identifier):
                if kind in {"scan", "agent_index"}:
                    raise AssertionError("A cached reply must not decode full source records")
                return original_get(kind, identifier)
            monkeypatch.setattr(service.store, "get", headers_only)
            same = await client.call_tool(operation, arguments)
            assert not same.is_error
            assert secret in json.dumps(same.structured_content)
            if changed == "workspace":
                current = original_get("workspace", workspace["id"])
                service.store.put("workspace", {**current, "generation": "scan_new_generation"})
            elif changed == "index":
                current = original_get("agent_index", index_id)
                service.store.put("agent_index", current, expected_revision=current["revision"])
            else:
                current = workflow.session(workspace["id"])
                service.store.put("agent_session", {**current, "notes": "REPLACED_WITHOUT_REBINDING"},
                                  expected_revision=current["revision"])
            session = workflow.session(workspace["id"])
            revisions = session["revision"]
            counts = {kind: len(service.store.list(kind)) for kind in ("job", "agent_context", "agent_session")}
            stale = await client.call_tool(operation, arguments)
            assert stale.is_error and stale.structured_content["error"]["code"] == "STALE_INPUT"
            assert secret not in json.dumps(stale.structured_content)
            assert workflow.session(workspace["id"])["revision"] == revisions
            assert {kind: len(service.store.list(kind)) for kind in counts} == counts
    asyncio.run(run())


def test_legacy_unscoped_cached_reply_is_withheld_without_repeating_original_write(setup, monkeypatch):
    from backend.agent.context import AssistantContext
    from personal_lab.bootstrap import LabApplication
    from toolkit_core.storage import canonical, digest
    service, workflow, workspace, _, _ = setup
    context = AssistantContext(service).get(selection="resume", workspace_id=workspace["id"])
    payload = {"context_id": context["id"], "notes": "PRIVATE_LEGACY_CACHE"}
    request_hash = digest(canonical({"payload": payload, "source_policy": service.inference_policy()}))
    key = f"{service.instance_id}:ck3:remember_progress:legacy-repeat"
    service.store.cache_request(key, request_hash, {"state": "reviewing", "notes": "PRIVATE_LEGACY_CACHE"})
    app = LabApplication(service.root, profile="ck3-local", service=service)
    before = workflow.session(workspace["id"])["revision"]
    def never_repeat(*args, **kwargs):
        raise AssertionError("An unscoped receipt must not re-execute its original effect")
    monkeypatch.setattr(service, "agent_request", never_repeat)

    async def run():
        async with Client(build_server(app)) as client:
            result = await client.call_tool("ck3_remember_progress", {**payload, "idempotency_key": "legacy-repeat"})
            assert result.is_error and result.structured_content["error"]["code"] == "STALE_INPUT"
            assert "PRIVATE_LEGACY_CACHE" not in json.dumps(result.structured_content)
    asyncio.run(run())
    assert workflow.session(workspace["id"])["revision"] == before


def test_artifact_scope_checks_use_headers_without_loading_saved_payloads(tmp_path, monkeypatch):
    with create_application(tmp_path / "state", profile="ck3-local") as app:
        workspace = app.service.create_workspace({"name": "Captured source scope", "mode": "game", "game_id": "ck3", "target_platform": "macos"})
        original = app.store.get
        def bounded_get(kind, identifier):
            if kind in {"workspace", "scan", "agent_index", "agent_context"}:
                raise AssertionError("Artifact authorization must use record headers")
            return original(kind, identifier)
        monkeypatch.setattr(app.store, "get", bounded_get)
        scope = app._scope("ck3", {"workspace_id": workspace["id"]})
        artifact = app.artifacts.register_bytes(b"captured source", "text/plain", scope=scope)
        assert app.artifacts.read(artifact.id, limit=4)["next_offset"] == 4
