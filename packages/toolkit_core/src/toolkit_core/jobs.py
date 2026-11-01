"""Bounded local execution with durable history and no automatic effect replay."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

from .contracts import LabError, ensure_instance
from .safety import ManagedLock, reject_link_components
from .storage import Store, canonical, digest, uid


class Cancelled(Exception):
    pass


class BoundedExecutor:
    def __init__(self, pending: int = 16):
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lab-worker")
        self.slots = threading.BoundedSemaphore(pending + 1)
        self.closed = False

    def reserve(self):
        if self.closed or not self.slots.acquire(blocking=False):
            raise LabError("BUSY", "The bounded local execution queue is full or shutting down.", True)

    def submit_reserved(self, fn, *args):
        try:
            result = self.pool.submit(fn, *args)
        except BaseException:
            self.slots.release()
            raise
        result.add_done_callback(lambda _: self.slots.release())
        return result

    def submit(self, fn, *args):
        self.reserve()
        return self.submit_reserved(fn, *args)

    def shutdown(self, wait=True, cancel_futures=False):
        self.closed = True
        self.pool.shutdown(wait=wait, cancel_futures=cancel_futures)


def run_projection(store, identifier, instance_id):
    try:
        record = store.get("run", identifier)
    except KeyError:
        record = store.get("job", identifier)
        if not record.get("game_id"):
            try:
                workspace = store.get("workspace", record["workspace_id"])
            except KeyError:
                raise LabError("PATH_DENIED", "Historical job ownership is unavailable.") from None
            # The original schema used EU5-only records before game identity
            # was introduced. Read the header without importing domain code.
            record["game_id"] = workspace.get("game_id", "eu5")
    if record.get("instance_id", instance_id) != instance_id:
        raise LabError("PATH_DENIED", "Run belongs to another instance.")
    state = record["state"]
    if state == "completed":
        state = "succeeded"
    elif state == "failed" and record.get("phase") == "interrupted":
        state = "interrupted"
    return {**record, "state": state, "instance_id": instance_id, "run_id": record["id"]}


def record_submission_failure(store, kind, identifier):
    """Retain accepted history without retrying an ambiguous scheduling failure."""
    with store.atomic():
        record = store.get(kind, identifier)
        if record["state"] in {"queued", "running", "cancelling"}:
            record.update(state="failed" if kind == "job" else "interrupted", phase="interrupted",
                error="Execution scheduling failed after acceptance; inspect the retained run before resubmitting.")
            saved = store.put(kind, record, expected_revision=record["revision"])
            store.append_event(identifier, saved)


class RunManager:
    def __init__(self, root: Path, profile="core-only"):
        self.root = reject_link_components(Path(root))
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner = ManagedLock(self.root / "coordinator.lock")
        self.owner.__enter__()
        try:
            self.store = Store(self.root)
            self.instance_id, self.profile = ensure_instance(self.store), profile
            self.executor = BoundedExecutor()
            self.command_lock = threading.RLock()
            with self.store.atomic():
                for record in self.store.iter_records("run"):
                    if record["state"] in {"queued", "running", "cancelling"}:
                        record.update(state="interrupted", error="Execution owner exited; resubmit explicitly.")
                        self.store.put("run", record)
        except BaseException:
            self.owner.__exit__(None, None, None)
            raise

    def start(self, operation, payload, idempotency_key, handler):
        request_hash = digest(canonical({"operation": operation, "payload": payload}))
        key = f"{self.instance_id}:{operation}:{idempotency_key}"
        with self.command_lock:
            cached = self.store.cached_request(key, request_hash)
            if cached is not None:
                return cached
            if getattr(self, "closing", False):
                raise LabError("BUSY", "The execution owner is shutting down.", True)
            self.executor.reserve()
            try:
                with self.store.atomic():
                    record = self.store.put("run", {"id": uid("run"), "state": "queued", "operation": operation,
                        "profile": self.profile, "instance_id": self.instance_id, "contract_version": "1.0",
                        "input_digest": request_hash, "cancel_requested": False, "result": None})
                    response = {"run_id": record["id"]}
                    self.store.cache_request(key, request_hash, response)
                    self.store.append_event(record["id"], {"state": "queued"})
            except BaseException:
                self.executor.slots.release()
                raise
            try:
                self.executor.submit_reserved(self._run, record["id"], handler)
            except BaseException as exc:
                record_submission_failure(self.store, "run", record["id"])
                raise LabError("RECOVERY_REQUIRED", "The accepted run could not be scheduled; no automatic retry is permitted.",
                               details={"run_id": record["id"]}) from exc
            return response

    def _run(self, identifier, handler):
        def checkpoint(phase="running"):
            with self.store.atomic():
                record = self.store.get("run", identifier)
                if record["cancel_requested"]:
                    raise Cancelled()
                record.update(state="running", phase=phase)
                self.store.put("run", record)
                self.store.append_event(identifier, {"state": "running", "phase": phase})
        try:
            checkpoint()
            result = handler(checkpoint)
            outcome = {"state": "succeeded", "result": result}
        except Cancelled:
            outcome = {"state": "cancelled"}
        except Exception as exc:
            outcome = {"state": "failed", "error": str(exc)}
        with self.store.atomic():
            record = self.store.get("run", identifier)
            record.update(outcome)
            self.store.put("run", record)
            self.store.append_event(identifier, outcome)

    def run_get(self, run_id):
        return run_projection(self.store, run_id, self.instance_id)

    def run_events(self, run_id, cursor=0, limit=50):
        self.run_get(run_id)
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise LabError("VALIDATION_FAILED", "Invalid event range.")
        events = self.store.events(run_id, cursor)[:limit]
        return {"events": events, "next_cursor": events[-1]["sequence"] if len(events) == limit else None}

    def cancel_run(self, run_id):
        with self.store.atomic():
            record = self.store.get("run", run_id)
            self.run_get(run_id)
            terminal = record["state"] in {"succeeded", "failed", "cancelled", "interrupted"}
            if not terminal:
                record.update(cancel_requested=True, state="cancelling")
                self.store.put("run", record)
            return {"run_id": run_id, "cancellation_requested": not terminal}

    def close(self):
        with self.command_lock:
            self.closing = True
            with self.store.atomic():
                for record in self.store.iter_records("run"):
                    if record["state"] in {"queued", "running", "cancelling"}:
                        record.update(cancel_requested=True, state="cancelling")
                        self.store.put("run", record)
        self.executor.shutdown(wait=True)
        self.store.close()
        self.owner.__exit__(None, None, None)
