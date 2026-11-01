"""Probe lifecycle contracts use disposable child processes, never CK3."""
import concurrent.futures
from pathlib import Path
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from backend.agent import ck3_test as probe
from backend.app.store import Store
from test_agent_test_setup import prepare


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "sys", SimpleNamespace(platform="darwin", executable=sys.executable))
    store, discovery, normal = prepare(tmp_path)
    try:
        yield store, discovery, normal, probe.prepare_probe(store, discovery)
    finally:
        store.close()


def identity(pid=123456):
    return {"pid": pid, "pgid": pid, "started": [1234, 5678], "zombie": False}


def allow_synthetic_start(monkeypatch):
    monkeypatch.setattr(probe, "check_os_boundary", lambda *_: {"executed": True, "game_launched": False})
    monkeypatch.setattr(probe.subprocess, "run", lambda *_, **__: SimpleNamespace(stdout=b""))
    monkeypatch.setattr(probe, "process_identity", lambda pid: identity(pid))
    monkeypatch.setattr(probe, "_group_members", lambda group: [{"pid": group, "zombie": False}])


@pytest.mark.parametrize("changed", ["plan", "executable", "policy", "profile", "arguments"])
def test_changed_reviewed_launch_preconditions_refuse_before_spawn(prepared, monkeypatch, changed):
    store, _, _, record = prepared
    if changed == "plan":
        store.fs.write_bytes(record["prefix"] + "/plan.json", b"changed")
    elif changed == "executable":
        Path(record["executable"]).write_bytes(b"changed")
    elif changed == "policy":
        store.fs.write_bytes(record["prefix"] + "/isolation.sb", b"changed")
    elif changed == "profile":
        store.fs.write_bytes(record["prefix"] + "/profile/dlc_load.json", b'{"enabled_mods":["unreviewed"]}')
    else:
        store.put("agent_isolation", {**record, "argv": [record["executable"], "unreviewed"]})
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *_a, **_k: pytest.fail("A changed plan must not spawn"))
    with pytest.raises(ValueError):
        probe.start_probe(store, record["id"])
    assert store.get("agent_isolation", record["id"])["state"] == "prepared"


def test_missing_or_overlapping_original_root_rejected_before_profile_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "sys", SimpleNamespace(platform="darwin", executable=sys.executable))
    store, discovery, normal = prepare(tmp_path)
    try:
        (normal / "save").unlink()
        normal.rmdir()
        with pytest.raises(ValueError, match="missing"):
            probe.prepare_probe(store, discovery)
        normal.mkdir()
        discovery["installation"]["content_root"] = str(store.root)
        with pytest.raises(ValueError, match="separate"):
            probe.prepare_probe(store, discovery)
        assert not (store.root / "agent/tests").exists()
    finally:
        store.close()


def test_inventory_failure_never_returns_a_partial_preservation_inventory(prepared, monkeypatch):
    _, _, normal, _ = prepared
    def failed_walk(*_args, **kwargs):
        kwargs["onerror"](PermissionError("synthetic denied subtree"))
        return iter(())
    monkeypatch.setattr(probe.os, "walk", failed_walk)
    with pytest.raises(PermissionError):
        probe.normal_inventory(normal)


def test_launch_crash_leaves_durable_ambiguity_and_cannot_retry(prepared, monkeypatch):
    store, _, _, record = prepared
    allow_synthetic_start(monkeypatch)
    def crash(*_args, **_kwargs):
        assert store.get("agent_isolation", record["id"])["state"] == "starting"
        raise KeyboardInterrupt("synthetic crash at launch boundary")
    monkeypatch.setattr(probe.subprocess, "Popen", crash)
    with pytest.raises(KeyboardInterrupt):
        probe.start_probe(store, record["id"])
    assert store.get("agent_isolation", record["id"])["state"] == "starting"
    assert store.get("agent_isolation", record["id"])["game_launched"] is None
    assert probe.probe_status(store, record["id"])["state"] == "unknown"
    with pytest.raises(ValueError, match="already started"):
        probe.start_probe(store, record["id"])


def test_authoritative_spawn_failure_is_terminal_without_claiming_launch(prepared, monkeypatch):
    store, _, _, record = prepared
    allow_synthetic_start(monkeypatch)
    def failed(*_args, **_kwargs):
        raise OSError("synthetic child exec failed")
    monkeypatch.setattr(probe.subprocess, "Popen", failed)
    with pytest.raises(OSError):
        probe.start_probe(store, record["id"])
    failed_record = store.get("agent_isolation", record["id"])
    assert failed_record["state"] == "failed_to_start"
    assert failed_record["game_launched"] is False and failed_record["process_started"] is False
    with pytest.raises(ValueError, match="already started"):
        probe.start_probe(store, record["id"])


def test_distinct_prepared_probes_serialize_across_store_connections(prepared, monkeypatch):
    store, discovery, _, first = prepared
    second = probe.prepare_probe(store, discovery)
    another = Store(store.root)
    allow_synthetic_start(monkeypatch)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def spawn(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(pid=123456, poll=lambda: None)
    monkeypatch.setattr(probe.subprocess, "Popen", spawn)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            started = executor.submit(probe.start_probe, store, first["id"])
            assert entered.wait(5)
            refused = executor.submit(probe.start_probe, another, second["id"])
            try:
                with pytest.raises(ValueError, match="Another probe"):
                    refused.result(timeout=5)
            finally:
                release.set()
            assert started.result(timeout=5)["state"] == "running"
        assert len(calls) == 1
        assert store.get("agent_isolation", second["id"])["state"] == "prepared"
    finally:
        release.set()
        another.close()


def running_record(store, record, pid=123456):
    return store.put("agent_isolation", {**record, "state": "running", "pid": pid,
        "process_identity": identity(pid), "process_started": True})


@pytest.mark.parametrize("case", ["reused_pid", "denied_observation", "orphan_group", "contradictory_group"])
def test_uncertain_process_ownership_is_not_terminal_and_never_signalled(prepared, monkeypatch, case):
    store, _, _, record = prepared
    running_record(store, record)
    observed = identity()
    if case == "reused_pid":
        observed["started"] = [1234, 5679]
    if case == "orphan_group":
        observed = None
    def read_identity(_):
        if case == "denied_observation":
            raise PermissionError("synthetic denied process query")
        return observed
    monkeypatch.setattr(probe, "process_identity", read_identity)
    monkeypatch.setattr(probe, "_group_members", lambda _: [] if case == "contradictory_group" else [
        {"pid": 123457, "zombie": False}])
    monkeypatch.setattr(probe.os, "killpg", lambda *_: pytest.fail("Uncertain ownership must not signal"))
    assert probe.probe_status(store, record["id"])["state"] == "unknown"
    assert probe.stop_probe(store, record["id"])["state"] == "unknown"


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Mac process identity; no Windows qualification")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_real_disposable_process_exit_is_observed_and_stopping_is_honest(prepared, ignore_term):
    store, _, _, record = prepared
    program = ("import signal,time; " +
        ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if ignore_term else "") +
        "print('ready',flush=True); time.sleep(30)")
    child = subprocess.Popen([sys.executable, "-u", "-c", program], start_new_session=True,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        captured = probe.process_identity(child.pid)
        assert captured and captured["started"][1] >= 0
        store.put("agent_isolation", {**record, "state": "running", "pid": child.pid,
            "process_identity": captured, "process_started": True})
        assert probe.probe_status(store, record["id"])["state"] == "running"
        stopped = probe.stop_probe(store, record["id"], timeout=.15 if ignore_term else 1)
        assert stopped["state"] == ("stopping" if ignore_term else "stopped")
        if ignore_term:
            assert child.poll() is None
            child.kill()
        child.wait(timeout=5)
        status = probe.probe_status(store, record["id"])
        assert status["state"] == "stopped"
        assert status["game_launched"] is False and status["game_initialization"] == "not_observed"
        assert status["normal_data_metadata_unchanged"] is True
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Mac process-group observation")
def test_leader_exit_does_not_hide_live_descendant(prepared, tmp_path):
    store, _, _, record = prepared
    released = tmp_path / "release"
    # The leader remains alive until its identity has been captured, then exits
    # while its child remains in the original group. Neither process is a game.
    program = ("import os,time; from pathlib import Path; "
        "child=os.fork(); "
        "\nif child == 0: time.sleep(30); os._exit(0)"
        "\nprint(child,flush=True)"
        f"\nwhile not Path({str(released)!r}).exists(): time.sleep(.01)")
    leader = subprocess.Popen([sys.executable, "-u", "-c", program], start_new_session=True,
                              stdout=subprocess.PIPE, text=True)
    descendant = int(leader.stdout.readline().strip())
    try:
        captured = probe.process_identity(leader.pid)
        store.put("agent_isolation", {**record, "state": "running", "pid": leader.pid,
            "process_identity": captured, "process_started": True})
        released.write_text("exit")
        leader.wait(timeout=5)
        status = probe.probe_status(store, record["id"])
        assert status["state"] == "unknown"
        assert descendant in status["process_observation"]["live_group_members"]
        assert probe.stop_probe(store, record["id"])["state"] == "unknown"
    finally:
        if leader.poll() is None:
            leader.kill()
        leader.wait(timeout=5)
        try:
            probe.os.kill(descendant, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.stdout.close()
