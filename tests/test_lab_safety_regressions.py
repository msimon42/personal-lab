"""Adversarial boundaries for accepted work, source disclosure and authority."""
import threading

import pytest

from backend.app.service import Service
from personal_lab.bootstrap import LabApplication
from toolkit_core.artifacts import ArtifactManager
from toolkit_core.authorization import PlanManager
from toolkit_core.contracts import LabError
from toolkit_core.jobs import RunManager


def test_artifact_rechecks_disclosure_after_reading_immutable_bytes(tmp_path, monkeypatch):
    runtime = RunManager(tmp_path / "state")
    allowed = [True]
    artifacts = ArtifactManager(runtime.store, runtime.instance_id, authorize=lambda _: allowed[0])
    try:
        ref = artifacts.register_bytes(b"SYNTHETIC_PRIVATE_SOURCE", "text/plain", scope={"toolset": "ck3"})
        original = runtime.store.fs.read_bytes
        def revoke_after_read(*args, **kwargs):
            result = original(*args, **kwargs)
            allowed[0] = False
            return result
        monkeypatch.setattr(runtime.store.fs, "read_bytes", revoke_after_read)
        with pytest.raises(LabError, match="disclosure changed"):
            artifacts.read(ref.id)
    finally:
        runtime.close()


def test_old_game_less_job_is_not_disclosed_by_core_only_profile(tmp_path):
    state = tmp_path / "state"
    runtime = RunManager(state)
    runtime.store.put("workspace", {"id": "old-eu5-workspace"})
    runtime.store.put("job", {"id": "old-job", "workspace_id": "old-eu5-workspace", "state": "completed",
                              "result": {"source_text": "SYNTHETIC_PRIVATE_SOURCE"}})
    runtime.close()
    with LabApplication(state, profile="core-only") as application:
        with pytest.raises(LabError, match="disabled toolset"):
            application.project_run("old-job")


@pytest.mark.parametrize("domain", [False, True])
def test_scheduler_failure_retains_interrupted_id_and_never_replays(tmp_path, monkeypatch, domain):
    runtime = Service(tmp_path / "state") if domain else RunManager(tmp_path / "state")
    try:
        def no_submission(*args, **kwargs):
            raise RuntimeError("injected scheduler shutdown")
        monkeypatch.setattr(runtime.executor.pool, "submit", no_submission)
        if domain:
            workspace = runtime.store.list("workspace")[0]
            request = {"command": "scan", "workspace_id": workspace["id"], "payload": {"fixture_id": "F03"}, "idempotency_key": "interrupted"}
            def start():
                return runtime.command(request)
        else:
            def start():
                return runtime.start("synthetic", {}, "interrupted", lambda checkpoint: pytest.fail("Never scheduled"))
        with pytest.raises(LabError) as caught:
            start()
        assert caught.value.code == "RECOVERY_REQUIRED"
        identifier = caught.value.details["run_id"]
        assert runtime.run_get(identifier)["state"] == "interrupted"
        repeated = start()
        assert repeated.get("run_id", repeated.get("job_id")) == identifier
        assert runtime.run_get(identifier)["state"] == "interrupted"
    finally:
        runtime.close()


def test_atomic_acceptance_failure_rolls_back_record_and_idempotency(tmp_path, monkeypatch):
    runtime = RunManager(tmp_path / "state")
    try:
        original = runtime.store.append_event
        monkeypatch.setattr(runtime.store, "append_event", lambda *args: (_ for _ in ()).throw(OSError("injected event failure")))
        with pytest.raises(OSError, match="event failure"):
            runtime.start("work", {}, "same", lambda checkpoint: {"done": True})
        assert runtime.store.list("run") == []
        monkeypatch.setattr(runtime.store, "append_event", original)
        accepted = runtime.start("work", {}, "same", lambda checkpoint: {"done": True})
        runtime.executor.shutdown(wait=True)
        assert runtime.run_get(accepted["run_id"])["state"] == "succeeded"
    finally:
        runtime.close()


def test_synchronous_metadata_mutation_and_retry_record_commit_together(tmp_path, monkeypatch):
    service = Service(tmp_path / "state")
    try:
        before = service.store.list("workspace")
        original = service.store.cache_request
        def fail_receipt(*args):
            raise OSError("injected retry receipt failure")
        monkeypatch.setattr(service.store, "cache_request", fail_receipt)
        request = {"command": "create_workspace", "payload": {"name": "Atomic creation"}, "idempotency_key": "same"}
        with pytest.raises(OSError, match="receipt failure"):
            service.command(request)
        assert service.store.list("workspace") == before
        monkeypatch.setattr(service.store, "cache_request", original)
        accepted = service.command(request)
        assert service.command(request) == accepted
        assert len(service.store.list("workspace")) == len(before) + 1
    finally:
        service.close()


def test_service_queue_full_creates_no_extra_accepted_job(tmp_path, monkeypatch):
    service = Service(tmp_path / "state")
    entered, release = threading.Event(), threading.Event()
    def work(workspace, payload, checkpoint):
        entered.set()
        assert release.wait(10)
        return {"synthetic_control": True}
    monkeypatch.setattr(service, "_scan", work)
    workspace = service.store.list("workspace")[0]
    def request(key):
        return service.command({"command": "scan", "workspace_id": workspace["id"], "payload": {}, "idempotency_key": key})
    try:
        request("active")
        assert entered.wait(5)
        for number in range(16):
            request(f"queued-{number}")
        with pytest.raises(LabError) as caught:
            request("overflow")
        assert caught.value.code == "BUSY"
        assert len(service.store.list("job")) == 17
    finally:
        release.set()
        service.close()


def test_cancellation_racing_with_committed_success_retains_truthful_request(tmp_path, monkeypatch):
    service = Service(tmp_path / "state")
    entered, release = threading.Event(), threading.Event()
    def work(workspace, payload, checkpoint):
        entered.set()
        assert release.wait(5)
        return {"committed": True}
    monkeypatch.setattr(service, "_scan", work)
    try:
        workspace = service.store.list("workspace")[0]
        accepted = service.command({"command": "scan", "workspace_id": workspace["id"], "payload": {}, "idempotency_key": "race"})
        assert entered.wait(5)
        assert service.cancel_run(accepted["job_id"])["cancellation_requested"]
        release.set()
        service.executor.shutdown(wait=True)
        result = service.run_get(accepted["job_id"])
        assert result["state"] == "succeeded" and result["result"] == {"committed": True}
        assert result["cancel_requested"] is True
    finally:
        release.set()
        service.close()


def test_untrusted_command_cannot_stamp_operator_decision(tmp_path):
    service = Service(tmp_path / "state")
    try:
        workspace = service.store.list("workspace")[0]
        scan = service._scan(workspace, {"fixture_id": "F04"}, lambda *args: None)
        with pytest.raises(LabError) as caught:
            service.command({"command": "decision", "workspace_id": workspace["id"], "payload": {
                "scan_id": scan["scan_id"], "choice": "B", "reason": "Model claims approval"}, "idempotency_key": "fake-human"})
        assert caught.value.code == "APPROVAL_REQUIRED"
        assert service.store.list("decision") == []
    finally:
        service.close()


@pytest.mark.parametrize("field", ["plan_id", "target", "operation"])
def test_grant_must_match_every_bound_plan_identity(tmp_path, field):
    runtime = RunManager(tmp_path / "state")
    manager = PlanManager(runtime.store)
    try:
        plan = manager.prepare("publish", "disposable", "candidate", {}, [])
        grant = manager.approve(plan["id"], plan["plan_digest"])
        grant[field] = "another-identity"
        runtime.store.put("lab_grant", grant)
        with pytest.raises(LabError, match="grant"):
            manager.execute(plan["id"], "apply", lambda _: {}, lambda _: pytest.fail("Wrong grant cannot write"))
        assert runtime.store.list("lab_journal") == []
    finally:
        runtime.close()
