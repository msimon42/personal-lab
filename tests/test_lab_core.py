"""Core facilities are independent of games and enforce real retry boundaries."""
import threading

import pytest

from toolkit_core.artifacts import ArtifactManager
from toolkit_core.authorization import PlanManager
from toolkit_core.contracts import LabError
from toolkit_core.jobs import RunManager
from toolkit_core.paging import decode_cursor, encode_cursor


def test_artifacts_and_cursors_enforce_scope_and_bytes(tmp_path):
    runtime = RunManager(tmp_path / "state")
    try:
        artifacts = ArtifactManager(runtime.store, runtime.instance_id)
        ref = artifacts.register_bytes(b"abcdef", "text/plain")
        assert artifacts.read(ref.id, 0, 2)["next_offset"] == 2
        restricted = artifacts.register_bytes(b"secret", "text/plain", scope={"toolset": "ck3"})
        with pytest.raises(LabError, match="disclosure"):
            artifacts.read(restricted.id)
        cursor = encode_cursor({"snapshot": "one"}, 12, b"key")
        assert decode_cursor(cursor, {"snapshot": "one"}, b"key") == 12
        with pytest.raises(LabError):
            decode_cursor(cursor, {"snapshot": "two"}, b"key")
    finally:
        runtime.close()


def test_run_queue_retry_cancel_and_history(tmp_path):
    runtime = RunManager(tmp_path / "state")
    entered, release = threading.Event(), threading.Event()
    def work(checkpoint):
        entered.set()
        assert release.wait(5)
        checkpoint("finishing")
        return {"done": True}
    try:
        first = runtime.start("work", {"a": 1}, "key", work)
        assert entered.wait(5)
        assert runtime.start("work", {"a": 1}, "key", work) == first
        with pytest.raises(ValueError, match="Idempotency"):
            runtime.start("work", {"a": 2}, "key", work)
        runtime.cancel_run(first["run_id"])
        release.set()
        runtime.executor.shutdown(wait=True)
        assert runtime.run_get(first["run_id"])["state"] == "cancelled"
        assert runtime.run_events(first["run_id"])["events"]
    finally:
        release.set()
        runtime.close()
    reopened = RunManager(tmp_path / "state")
    try:
        assert reopened.run_get(first["run_id"])["state"] == "cancelled"
    finally:
        reopened.close()


def test_plan_grants_recheck_and_reconcile_disposable_effects(tmp_path):
    runtime = RunManager(tmp_path / "state")
    manager = PlanManager(runtime.store)
    target = tmp_path / "managed-output"
    effects = []
    try:
        record = manager.prepare("publish", "test-target", "candidate-hash", {"before": None}, [{"path": "result"}])
        def apply(plan):
            target.write_text("approved")
            effects.append(plan["id"])
            return {"published": True}
        with pytest.raises(LabError, match="approval"):
            manager.execute(record["id"], "once", lambda _: {"before": None}, apply)
        manager.approve(record["id"], record["plan_digest"])
        with pytest.raises(LabError, match="changed"):
            manager.execute(record["id"], "once", lambda _: {"before": "external"}, apply)
        result = manager.execute(record["id"], "once", lambda _: {"before": None}, apply)
        assert manager.execute(record["id"], "once", lambda _: {}, apply) == result
        assert len(effects) == 1 and target.read_text() == "approved"
        crash = manager.prepare("publish", "other-target", "next", {}, [])
        manager.approve(crash["id"], crash["plan_digest"])
        def fail(_):
            raise OSError("injected after intent")
        with pytest.raises(OSError):
            manager.execute(crash["id"], "crash", lambda _: {}, fail)
        with pytest.raises(LabError, match="journal"):
            manager.execute(crash["id"], "crash", lambda _: {}, apply)
    finally:
        runtime.close()
