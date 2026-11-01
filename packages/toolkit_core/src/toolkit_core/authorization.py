"""Plan-bound authority for trusted adapters; issuance is never an MCP tool.

Coding access under the same OS identity can bypass these application controls.
This module governs the bounded runtime interface, not the operator's OS account.
"""
from __future__ import annotations

import time

from .contracts import LabError, ensure_instance
from .safety import ManagedLock
from .storage import canonical, digest, uid


class PlanManager:
    def __init__(self, store):
        self.store, self.instance_id = store, ensure_instance(store)

    def prepare(self, operation: str, target: str, candidate_digest: str, preconditions: dict,
                writes: list, details: dict | None = None, eligible=True, ttl=900) -> dict:
        if not operation or not target or not 1 <= ttl <= 86400:
            raise LabError("VALIDATION_FAILED", "A plan needs an operation, target, and bounded lifetime.")
        body = {"id": uid("plan"), "instance_id": self.instance_id, "operation": operation, "target": target,
            "candidate_digest": candidate_digest, "preconditions": preconditions, "writes": writes,
            "details": details or {}, "eligible": bool(eligible), "expires_at": time.time() + ttl}
        return self.store.put("lab_plan", {"id": body["id"], "plan": body, "plan_digest": digest(canonical(body))})

    def inspect(self, plan_id):
        record = self.store.get("lab_plan", plan_id)
        if record.get("relocation_historical") or record["plan"]["instance_id"] != self.instance_id:
            raise LabError("STALE_INPUT", "Plan is bound to another runtime instance.")
        if digest(canonical(record["plan"])) != record["plan_digest"]:
            raise LabError("STALE_INPUT", "Immutable plan digest changed.")
        return record

    def approve(self, plan_id: str, expected_digest: str, channel="terminal") -> dict:
        """Called only after an actual trusted terminal or host response."""
        if channel not in {"terminal", "native"}:
            raise LabError("APPROVAL_REQUIRED", "Unsupported approval channel.")
        with self.store.atomic():
            record = self.inspect(plan_id)
            plan = record["plan"]
            if expected_digest != record["plan_digest"]:
                raise LabError("STALE_INPUT", "The displayed plan changed before approval.")
            if time.time() >= plan["expires_at"]:
                raise LabError("PLAN_EXPIRED", "Prepare and review a fresh plan.")
            if not plan["eligible"]:
                raise LabError("VALIDATION_FAILED", "Required capability or validation gates still block this plan.")
            try:
                existing = self.store.get("lab_grant", "grant_" + plan_id)
            except KeyError:
                existing = None
            if existing is not None:
                if existing["state"] != "available":
                    raise LabError("STALE_INPUT", "This plan's grant was consumed; prepare a new plan for another action.")
                if existing["plan_digest"] == expected_digest:
                    return existing
            return self.store.put("lab_grant", {"id": "grant_" + plan_id, "plan_id": plan_id,
                "instance_id": self.instance_id, "plan_digest": expected_digest, "target": plan["target"],
                "operation": plan["operation"], "channel": channel, "state": "available"})

    def execute(self, plan_id, idempotency_key, observe, apply):
        """Trusted adapter callables only; ambiguous writes require reconciliation."""
        record = self.inspect(plan_id)
        plan = record["plan"]
        lock_name = digest(canonical(plan["target"]))
        with ManagedLock(self.store.root / "locks" / (lock_name + ".lock")):
            key = f"{self.instance_id}:plan:{idempotency_key}"
            request_hash = digest(canonical({"plan_id": plan_id, "plan_digest": record["plan_digest"]}))
            with self.store.atomic():
                cached = self.store.cached_request(key, request_hash)
                if cached is not None:
                    journal = self.store.get("lab_journal", cached["journal_id"])
                    if journal["state"] == "committed":
                        return journal["result"]
                    raise LabError("RECOVERY_REQUIRED", "The prior attempt has an unresolved operation journal.")
                record = self.inspect(plan_id)
                if time.time() >= plan["expires_at"]:
                    raise LabError("PLAN_EXPIRED", "Prepare and approve a fresh plan.")
                try:
                    grant = self.store.get("lab_grant", "grant_" + plan_id)
                except KeyError:
                    raise LabError("APPROVAL_REQUIRED", "The current plan needs trusted operator approval.") from None
                if (not plan["eligible"] or grant["state"] != "available"
                        or grant["instance_id"] != self.instance_id or grant["plan_digest"] != record["plan_digest"]
                        or grant["plan_id"] != plan_id or grant["target"] != plan["target"]
                        or grant["operation"] != plan["operation"]):
                    raise LabError("APPROVAL_REQUIRED", "No unused grant matches this exact eligible plan.")
                if observe(plan) != plan["preconditions"]:
                    raise LabError("STALE_INPUT", "Source, candidate, or destination changed after preparation.")
                journal = self.store.put("lab_journal", {"id": uid("journal"), "plan_id": plan_id,
                    "state": "intent", "plan_digest": record["plan_digest"], "result": None})
                grant.update(state="consumed", journal_id=journal["id"])
                self.store.put("lab_grant", grant)
                self.store.cache_request(key, request_hash, {"journal_id": journal["id"]})
            try:
                result = apply(plan)
            except BaseException:
                journal["state"] = "recovery_required"
                self.store.put("lab_journal", journal)
                raise
            with self.store.atomic():
                journal.update(state="committed", result=result)
                self.store.put("lab_journal", journal)
            return result
