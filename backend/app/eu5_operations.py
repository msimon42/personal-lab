"""Typed, bounded headless operations over the existing EU5 implementation.

The public functions never accept host paths or issue operator authority. Fixture
adapters use the same physical capture, parser, planner and gates as other calls.
"""
from __future__ import annotations

import base64
import copy
import fnmatch
from functools import wraps
import json
from pathlib import Path
from typing import Any, Literal, TypeVar, Generic

from pydantic import BaseModel, ConfigDict, Field

from backend.app.store import Conflict, canonical, digest, uid


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(Model):
    pass


class PageInput(Model):
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=50, ge=1, le=200)


class InstallationInput(PageInput):
    installation_id: str = Field(min_length=1, max_length=200)


class ScanInput(Model):
    installation_id: str = Field(min_length=1, max_length=200)
    ordered_mod_ids: list[str] | None = Field(default=None, max_length=1000)
    playset_id: str | None = Field(default=None, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=128)


class SnapshotInput(PageInput):
    snapshot_id: str = Field(min_length=1, max_length=200)


class FindingsInput(SnapshotInput):
    category: str | None = Field(default=None, max_length=200)
    kind: str | None = Field(default=None, max_length=200)


class FindingInput(Model):
    finding_id: str = Field(min_length=1, max_length=500)
    cursor: str | None = Field(default=None, max_length=4096)


class SourceInput(Model):
    snapshot_id: str = Field(min_length=1, max_length=200)
    source_id: str = Field(min_length=1, max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4096, ge=1, le=8192)


class DefinitionsInput(SnapshotInput):
    query: str = Field(default="", max_length=500)
    stage: str | None = Field(default=None, max_length=200)
    domain: str | None = Field(default=None, max_length=200)


class DefinitionInput(SnapshotInput):
    definition_id: str = Field(min_length=1, max_length=4096)


class IntentInput(Model):
    snapshot_id: str = Field(min_length=1, max_length=200)
    finding_ids: list[str] = Field(default_factory=list, max_length=100)
    selected_outcome: Literal["preserve_all", "choose_owner"] = "preserve_all"
    choice: str | None = Field(default=None, max_length=200)
    reason: str = Field(default="Preserve every contribution", min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=128)


class WorkspaceCreateInput(Model):
    snapshot_id: str = Field(min_length=1, max_length=200)
    intent_ids: list[str] = Field(default_factory=list, max_length=100)
    idempotency_key: str = Field(min_length=1, max_length=128)


class WorkspaceInput(Model):
    workspace_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)


class WorkspaceReadInput(WorkspaceInput, PageInput):
    pass


class MutationInput(WorkspaceInput):
    idempotency_key: str = Field(min_length=1, max_length=128)


class ReplaceScalar(Model):
    kind: Literal["replace_scalar"]
    path: str = Field(min_length=1, max_length=32760)
    document_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    ordinal: int = Field(ge=0)
    old_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    replacement_base64: str = Field(max_length=5500)


class PatchInput(MutationInput):
    operations: list[ReplaceScalar] = Field(min_length=1, max_length=16)


class PrepareInput(MutationInput):
    target_id: Literal["experimental"] = "experimental"


class Installation(Model):
    id: str
    name: str
    mode: Literal["fixture", "configured"]
    available: bool
    limitations: list[str] = Field(default_factory=list)


class Mod(Model):
    id: str
    owner: str
    files: int | None
    fingerprint: str | None
    limitations: list[str] = Field(default_factory=list)


class Playset(Model):
    id: str
    ordered_mod_ids: list[str]
    version: str
    provenance: Literal["fixture", "operator_configured"]
    active_in_game_verified: bool = False


class Finding(Model):
    id: str
    kind: str
    category: str
    target: str
    message: str
    certainty: str
    coverage: str
    repairability: str


class Definition(Model):
    id: str
    state: str
    contribution_count: int


class Reference(Model):
    graph: str
    source: str | None = None
    target: str | None = None
    details: dict[str, Any]


class Diff(Model):
    path: str
    sha256: str
    size: int
    text: str


T = TypeVar("T")


class Page(Model, Generic[T]):
    items: list[T]
    total: int
    next_cursor: str | None = None
    limitations: list[str] = Field(default_factory=list)


class RunAccepted(Model):
    run_id: str
    workspace_id: str
    state: Literal["accepted"] = "accepted"


class RecordChunk(Model):
    record_id: str
    snapshot_id: str
    media_type: Literal["application/json"] = "application/json"
    json_chunk: str
    sha256: str
    total_bytes: int
    next_cursor: str | None = None


class SourceData(Model):
    snapshot_id: str
    source_id: str
    owner: str
    path: str
    sha256: str
    offset: int
    bytes_base64: str
    text: str
    total_bytes: int
    next_offset: int | None = None


class IntentData(Model):
    intent_id: str
    provenance: Literal["agent_proposed"] = "agent_proposed"
    state: Literal["proposed", "awaiting_operator", "operator_selected"]
    question_id: str | None = None
    choices: list[str] = Field(default_factory=list)
    decision_id: str | None = None
    limitations: list[str] = Field(default_factory=list)


class WorkspaceData(Model):
    workspace_id: str
    snapshot_id: str
    workspace_revision: int
    candidate_id: str
    state: str
    files: int


class PlanData(Model):
    plan_id: str
    plan_digest: str
    workspace_id: str
    workspace_revision: int
    state: Literal["prepared"] = "prepared"
    external_apply_available: bool = False
    experimental_export_eligible: bool
    limitations: list[str]


class ExportData(Model):
    artifact_id: str
    sha256: str
    media_type: Literal["application/zip"] = "application/zip"
    size: int
    kind: Literal["experimental_bundle"] = "experimental_bundle"
    activated: bool = False


def _cursor(scope: dict, offset: int) -> str:
    return base64.urlsafe_b64encode(canonical({"scope": digest(canonical(scope)), "offset": offset})).decode()


def _offset(scope: dict, cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if set(value) != {"scope", "offset"} or value["scope"] != digest(canonical(scope)):
            raise ValueError
        if type(value["offset"]) is not int or value["offset"] < 0:
            raise ValueError
        return value["offset"]
    except (ValueError, TypeError, KeyError) as exc:
        raise Conflict("Continuation does not match this immutable scope") from exc


def snapshot_read(function):
    """Return-time scope/policy guards apply equally to direct, CLI and MCP reads."""
    @wraps(function)
    def read(self, args):
        workspace_header = None
        if hasattr(args, "snapshot_id"):
            snapshot_id = args.snapshot_id
        elif hasattr(args, "finding_id"):
            snapshot_id = args.finding_id.partition("/")[0]
        else:
            staging = self.store.get("lab_eu5_workspace", args.workspace_id)
            workspace_header = self.store.record_header("lab_eu5_workspace", args.workspace_id)
            snapshot_id = staging["snapshot_id"]
        header = self.store.record_header("scan", snapshot_id)
        policy = self._policy()
        result = function(self, args)
        if (policy != self._policy() or header != self.store.record_header("scan", snapshot_id)
                or workspace_header is not None and workspace_header != self.store.record_header("lab_eu5_workspace", args.workspace_id)):
            raise Conflict("Source disclosure or snapshot scope changed during this read")
        return result
    return read


class EU5Operations:
    def __init__(self, service, *, profile="dev", installations=None):
        self.service, self.store, self.profile = service, service.store, profile
        if installations is None:
            installations = []
        self.installations = {item["id"]: dict(item) for item in installations if item.get("game_id", "eu5") == "eu5"}
        if profile == "dev":
            from backend.engine.fixtures import catalog
            self.installations.update({"fixture:" + entry["id"]: {
                "id": "fixture:" + entry["id"], "name": entry["title"], "fixture_id": entry["id"]}
                for entry in catalog()})

    def _policy(self):
        return self.service.inference_policy().get("excluded_patterns", [])

    def _snapshot(self, identifier):
        record = self.store.get("scan", identifier)
        if record.get("game_id", "eu5") != "eu5":
            raise ValueError("Snapshot does not belong to EU5")
        workspace = self.store.get("workspace", record["workspace_id"])
        if workspace.get("game_id", "eu5") != "eu5":
            raise ValueError("Workspace does not belong to EU5")
        return record

    def _disclosure(self, scan):
        patterns = self._policy()
        if any(fnmatch.fnmatch(item["path"], pattern) for item in scan["inputs"] for pattern in patterns):
            raise ValueError("Snapshot details are withheld by the current source disclosure policy")

    def _page(self, rows, args, *, scope, model):
        scope = {**scope, "policy": self._policy()}
        if scope.get("snapshot"):
            scope["snapshot_revision"] = self.store.get("scan", scope["snapshot"])["revision"]
        offset = _offset(scope, args.cursor)
        selected, size = [], 0
        for row in rows[offset:offset + args.limit]:
            item = model.model_validate(row)
            item_size = len(item.model_dump_json().encode())
            if item_size > 24 * 1024:
                raise ValueError("Use a bounded detail read for this oversized record")
            if size + item_size > 24 * 1024:
                break
            selected.append(item)
            size += item_size
        end = offset + len(selected)
        return Page[model](items=selected, total=len(rows), next_cursor=_cursor(scope, end) if end < len(rows) else None)

    def _idempotent(self, operation, args, output_model, fn):
        request = args.model_dump(exclude={"idempotency_key"})
        policy = self._policy()
        checksum = digest(canonical({"operation": operation, "input": request, "policy": policy}))
        key = f"lab:{getattr(self.service, 'instance_id', 'legacy')}:{operation}:{args.idempotency_key}"
        with self.service.command_lock:
            cached = self.store.cached_request(key, checksum)
            if cached is not None:
                if policy != self._policy():
                    raise Conflict("Source disclosure changed during the retry")
                return output_model.model_validate(cached)
            result = output_model.model_validate(fn())
            if policy != self._policy():
                raise Conflict("Source disclosure changed during this operation; no output is authorized for disclosure")
            self.store.cache_request(key, checksum, result.model_dump())
            return result

    def installations_list(self, args):
        rows = [Installation(id=item["id"], name=item.get("name", item["id"]),
            mode="fixture" if item.get("fixture_id") else "configured", available=True,
            limitations=["Synthetic contract; no game observation"] if item.get("fixture_id") else ["Real-game loading remains unverified"])
            .model_dump() for item in sorted(self.installations.values(), key=lambda row: row["id"])]
        return self._page(rows, args, scope={"operation": "installations", "digest": digest(canonical(rows))}, model=Installation)

    def _installation(self, identifier):
        try:
            return self.installations[identifier]
        except KeyError as exc:
            raise ValueError("Installation must be registered in the selected startup profile") from exc

    def _workspace(self, installation):
        if installation.get("workspace_id"):
            workspace = self.store.get("workspace", installation["workspace_id"])
            if workspace.get("game_id", "eu5") != "eu5":
                raise ValueError("Configured installation is not EU5")
            return workspace
        old = self.store.iter_records("lab_eu5_installation")
        binding = next((row for row in old if row["installation_id"] == installation["id"]), None)
        if binding:
            if binding.get("configuration_hash") != digest(canonical(installation)):
                raise Conflict("Installation configuration changed; register a new identity before capturing its sources")
            return self.store.get("workspace", binding["domain_workspace_id"])
        payload = {"name": installation.get("name", installation["id"]), "mode": "synthetic" if installation.get("fixture_id") else "game"}
        if installation.get("fixture_id"):
            # Physical source trees are siblings of domain state, so original
            # source and mutable state roots cannot overlap.
            from backend.engine.fixture_directories import materialize_fixture
            root = self.service.root.parent / (self.service.root.name + "-fixtures") / installation["fixture_id"]
            if installation["fixture_id"] not in {"F15", "F16"}:
                if not root.exists():
                    materialize_fixture(installation["fixture_id"], root)
                payload["fixture_path"] = str(root)
        else:
            payload["sources"] = installation.get("sources", [])
        workspace = self.service.create_workspace(payload)
        self.store.put("lab_eu5_installation", {"installation_id": installation["id"], "domain_workspace_id": workspace["id"],
            "configuration_hash": digest(canonical(installation))})
        return workspace

    def _inventory(self, installation):
        if installation.get("fixture_id"):
            from backend.engine.fixtures import load_fixture
            case = load_fixture(installation["fixture_id"])
            return case["inputs"], case["playset"]
        workspace = self.store.get("workspace", installation["workspace_id"]) if installation.get("workspace_id") else None
        if workspace and workspace.get("game_id", "eu5") != "eu5":
            raise ValueError("Configured installation is not EU5")
        if workspace and workspace.get("generation"):
            scan = self._snapshot(workspace["generation"])
            return scan["inputs"], scan["playset"]
        sources = workspace["sources"] if workspace else installation.get("sources", [])
        return [{"owner": row["owner"], "unscanned": True} for row in sources], [row["owner"] for row in sources]

    def mods_list(self, args):
        installation = self._installation(args.installation_id)
        files, _ = self._inventory(installation)
        rows = []
        for owner in sorted({row["owner"] for row in files}):
            entries = [{key: row.get(key) for key in ("path", "sha256")} for row in files if row["owner"] == owner]
            scanned = not any(row.get("unscanned") for row in files if row["owner"] == owner)
            rows.append({"id": owner, "owner": owner, "files": len(entries) if scanned else None,
                "fingerprint": digest(canonical(entries)) if scanned else None,
                "limitations": [] if scanned else ["Not captured; file count and fingerprint are unknown"]})
        return self._page(rows, args, scope={"operation": "mods", "installation": installation["id"], "digest": digest(canonical(rows))}, model=Mod)

    def playsets_list(self, args):
        installation = self._installation(args.installation_id)
        _, order = self._inventory(installation)
        row = Playset(id=installation["id"] + ":configured", ordered_mod_ids=order,
                      version=digest(canonical(order)), provenance="fixture" if installation.get("fixture_id") else "operator_configured")
        return Page[Playset](items=[row], total=1)

    def scan_start(self, args):
        installation = self._installation(args.installation_id)
        def start():
            workspace = self._workspace(installation)
            _, default_order = self._inventory(installation)
            if (args.ordered_mod_ids is None) == (args.playset_id is None):
                raise ValueError("Select explicit ordered_mod_ids or one versioned playset_id")
            if args.playset_id is not None and args.playset_id != installation["id"] + ":configured":
                raise ValueError("Unknown configured playset")
            order = args.ordered_mod_ids if args.ordered_mod_ids is not None else default_order
            if len(order) != len(set(order)) or not set(order) <= set(default_order):
                raise ValueError("Mod selection must contain unique registered IDs")
            payload = {} if workspace.get("fixture_path") else {"playset": order}
            if installation.get("fixture_id") and not workspace.get("fixture_path"):
                payload["fixture_id"] = installation["fixture_id"]
            if workspace.get("fixture_path") and order != default_order:
                raise ValueError("Physical fixture order is fixed by its immutable contract")
            response = self.service.command({"command": "scan", "workspace_id": workspace["id"], "payload": payload,
                "expected_revision": workspace["revision"], "idempotency_key": uid("eu5_scan")})
            return RunAccepted(run_id=response["job_id"], workspace_id=workspace["id"])
        return self._idempotent("scan", args, RunAccepted, start)

    @staticmethod
    def _finding(scan, row):
        return Finding(id=scan["id"] + "/" + row["id"], kind=row["kind"], category=row["category"],
            target=str(row.get("target", "")), message=row.get("message", ""), certainty=str(row.get("certainty", "unknown")),
            coverage=str(row.get("coverage", "unknown")), repairability=str(row.get("repairability", "investigate"))).model_dump()

    @snapshot_read
    def findings_list(self, args):
        scan = self._snapshot(args.snapshot_id)
        self._disclosure(scan)
        rows = [self._finding(scan, row) for row in scan["analysis"]["findings"]
                if (args.category is None or row["category"] == args.category) and (args.kind is None or row["kind"] == args.kind)]
        return self._page(rows, args, scope={"operation": "findings", "snapshot": scan["id"], "category": args.category, "kind": args.kind}, model=Finding)

    def _record(self, scan, identifier, value, cursor):
        raw = canonical(value)
        scope = {"snapshot": scan["id"], "record": identifier, "hash": digest(raw), "policy": self._policy()}
        offset = _offset(scope, cursor)
        chunk = raw[offset:offset + 8192]
        # JSON chunks are UTF-8 text; move the end back to a codepoint boundary.
        while chunk:
            try:
                text = chunk.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                if offset and exc.start == 0:
                    raise ValueError("Continuation is not on a UTF-8 boundary") from exc
                chunk = chunk[:-1]
        else:
            text = ""
        end = offset + len(chunk)
        return RecordChunk(record_id=identifier, snapshot_id=scan["id"], json_chunk=text, sha256=digest(raw),
            total_bytes=len(raw), next_cursor=_cursor(scope, end) if end < len(raw) else None)

    @snapshot_read
    def finding_get(self, args):
        snapshot, separator, identifier = args.finding_id.partition("/")
        if not separator:
            raise ValueError("Use the snapshot-bound finding ID from eu5_findings_list")
        scan = self._snapshot(snapshot)
        self._disclosure(scan)
        row = next((row for row in scan["analysis"]["findings"] if row["id"] == identifier), None)
        if row is None:
            raise ValueError("Finding is not in this snapshot")
        return self._record(scan, args.finding_id, row, args.cursor)

    @snapshot_read
    def source_read(self, args):
        scan = self._snapshot(args.snapshot_id)
        self._disclosure(scan)
        file = next((row for row in scan["analysis"]["files"] if row["id"] == args.source_id), None)
        if file is None:
            raise ValueError("Source ID is not in this snapshot")
        item = next(row for row in scan["inputs"] if row["owner"] == file["owner"] and row["path"] == file["path"])
        if "bytes_base64" not in item:
            raise ValueError("Opaque source is preserved but unavailable as source text")
        raw = base64.b64decode(item["bytes_base64"], validate=True)
        if digest(raw) != file["sha256"]:
            raise Conflict("Captured source hash changed")
        chunk = raw[args.offset:args.offset + args.limit]
        end = args.offset + len(chunk)
        return SourceData(snapshot_id=scan["id"], source_id=file["id"], owner=file["owner"], path=file["path"],
            sha256=file["sha256"], offset=args.offset, bytes_base64=base64.b64encode(chunk).decode(),
            text=chunk.decode("utf-8", errors="backslashreplace"), total_bytes=len(raw), next_offset=end if end < len(raw) else None)

    @snapshot_read
    def definitions_search(self, args):
        scan = self._snapshot(args.snapshot_id)
        self._disclosure(scan)
        rows = []
        for entry in scan["analysis"]["definitions"]:
            key = entry.get("key", entry.get("id", ""))
            if args.query not in key or (args.stage and not key.startswith(args.stage + "/")) or (args.domain and "/" + args.domain + "/" not in key):
                continue
            rows.append({"id": key, "state": str(entry.get("state", "unknown")),
                "contribution_count": sum(row.get("key") == key for row in scan["analysis"]["source_definitions"])})
        return self._page(rows, args, scope={"operation": "definitions", "snapshot": scan["id"], "query": args.query, "stage": args.stage, "domain": args.domain}, model=Definition)

    @snapshot_read
    def definition_compare(self, args):
        scan = self._snapshot(args.snapshot_id)
        self._disclosure(scan)
        definition = next((row for row in scan["analysis"]["definitions"] if row.get("key", row.get("id")) == args.definition_id), None)
        if definition is None:
            raise ValueError("Definition is not in this snapshot")
        value = {"effective": definition, "contributions": [row for row in scan["analysis"]["source_definitions"] if row["key"] == args.definition_id],
                 "baseline": scan.get("baselines", {}).get(args.definition_id), "meaning": "Predicted by the pinned profile; not an observed game result"}
        return self._record(scan, args.definition_id, value, args.cursor)

    @snapshot_read
    def references_list(self, args):
        scan = self._snapshot(args.snapshot_id)
        self._disclosure(scan)
        rows = []
        for graph, edges in scan["analysis"].get("graphs", {}).items():
            for edge in edges:
                if args.definition_id in {edge.get("from"), edge.get("to"), edge.get("source"), edge.get("target")}:
                    rows.append({"graph": graph, "source": edge.get("from", edge.get("source")), "target": edge.get("to", edge.get("target")), "details": edge})
        page = self._page(rows, args, scope={"operation": "references", "snapshot": scan["id"], "definition": args.definition_id}, model=Reference)
        page.limitations = ["Dynamic and unsupported references remain unknown; this is not a complete runtime call graph"]
        return page

    def intent_record(self, args):
        def record():
            scan = self._snapshot(args.snapshot_id)
            self._disclosure(scan)
            self.service._current_scan(self.store.get("workspace", scan["workspace_id"]), scan["id"])
            chosen = []
            for identifier in args.finding_ids:
                prefix, sep, local = identifier.partition("/")
                row = next((row for row in scan["analysis"]["findings"] if row["id"] == local), None)
                if not sep or prefix != scan["id"] or row is None:
                    raise ValueError("Intent findings must belong to this snapshot")
                chosen.append(row)
            target, choices = None, []
            if args.selected_outcome != "preserve_all":
                targets = {row.get("target") for row in chosen}
                if len(targets) != 1:
                    raise ValueError("A gameplay choice must identify one contradictory definition")
                target = targets.pop()
                choices = sorted({row["owner"] for row in scan["analysis"]["obligations"] if row.get("definition_key", row.get("target")) == target})
                if not choices or args.choice not in choices:
                    raise ValueError("Choose one exact contributed alternative")
            intent = self.store.put("lab_eu5_intent", {"snapshot_id": scan["id"], "workspace_id": scan["workspace_id"],
                "manifest_hash": scan["manifest_hash"], "profile_hash": scan["profile_hash"], "provenance": "agent_proposed",
                "selected_outcome": args.selected_outcome, "finding_ids": args.finding_ids, "reason": args.reason, "state": "proposed"})
            if args.selected_outcome == "preserve_all":
                return IntentData(intent_id=intent["id"], state="proposed")
            payload = {"scan_id": scan["id"], "choice": args.choice, "reason": args.reason, "definition_id": target}
            alternatives = {owner: [{"field": row.get("field"), "value": row.get("expected_value", row.get("expected"))}
                for row in scan["analysis"]["obligations"] if row["owner"] == owner and row.get("definition_key", row.get("target")) == target]
                for owner in choices}
            question = {"workspace_id": scan["workspace_id"], "intent_id": intent["id"], "snapshot_id": scan["id"],
                "manifest_hash": scan["manifest_hash"], "profile_hash": scan["profile_hash"], "decision": payload,
                "choices": choices, "alternatives": alternatives, "state": "pending", "provenance": "agent_proposed"}
            question["question_hash"] = digest(canonical(question))
            saved = self.store.put("lab_eu5_question", question)
            return IntentData(intent_id=intent["id"], state="awaiting_operator", question_id=saved["id"], choices=choices,
                limitations=["No contribution was waived. A trusted native response or lab decide is required."])
        return self._idempotent("intent", args, IntentData, record)

    def _staging(self, args):
        workspace = self.store.get("lab_eu5_workspace", args.workspace_id)
        if workspace["workspace_revision"] != args.expected_revision:
            raise Conflict("Staging revision changed; inspect the current revision before retrying")
        candidate = self.store.get("candidate", workspace["candidate_id"])
        domain = self.store.get("workspace", workspace["domain_workspace_id"])
        self._disclosure(self._snapshot(workspace["snapshot_id"]))
        self.service._current_scan(domain, workspace["snapshot_id"])
        if candidate["status"] == "stale" or candidate["decision_set_hash"] != self.service._decision_hash(domain["id"], workspace["snapshot_id"]):
            raise Conflict("Staging or approved intent is stale")
        return workspace, candidate, domain

    @staticmethod
    def _workspace_data(workspace, candidate):
        return WorkspaceData(workspace_id=workspace["id"], snapshot_id=workspace["snapshot_id"],
            workspace_revision=workspace["workspace_revision"], candidate_id=candidate["id"], state=candidate["status"], files=len(candidate["plan"].get("files", [])))

    def workspace_create(self, args):
        def create():
            scan = self._snapshot(args.snapshot_id)
            self._disclosure(scan)
            for identifier in args.intent_ids:
                intent = self.store.get("lab_eu5_intent", identifier)
                if intent["snapshot_id"] != scan["id"]:
                    raise ValueError("Intent belongs to a different snapshot")
                if intent["selected_outcome"] != "preserve_all" and intent.get("state") != "operator_selected":
                    raise ValueError("A proposed gameplay choice still needs the operator's decision")
            domain = self.store.get("workspace", scan["workspace_id"])
            response = self.service._stage(domain, {"scan_id": scan["id"]}, lambda *args: None)
            candidate = self.store.get("candidate", response["candidate_id"])
            workspace = self.store.put("lab_eu5_workspace", {"snapshot_id": scan["id"], "domain_workspace_id": domain["id"],
                "candidate_id": candidate["id"], "workspace_revision": 0})
            return self._workspace_data(workspace, candidate)
        return self._idempotent("workspace_create", args, WorkspaceData, create)

    def workspace_patch(self, args):
        from backend.engine.parser import Edit, apply_edits, parse
        from backend.engine.safety import validate_relative_path
        def patch():
            workspace, candidate, _ = self._staging(args)
            files = copy.deepcopy(candidate["plan"].get("files", []))
            used = set()
            for file in files:
                operations = [(index, op) for index, op in enumerate(args.operations) if op.path == file["path"]]
                if not operations:
                    continue
                validate_relative_path(file["path"])
                raw = base64.b64decode(file["bytes_base64"], validate=True)
                doc = parse(raw)
                if not doc.eligible:
                    raise ValueError("Recovered or unsupported syntax cannot be rewritten")
                edits = []
                def nodes(children):
                    for node in children:
                        yield node
                        yield from nodes(node.children)
                for index, op in operations:
                    if op.document_hash != doc.sha256:
                        raise Conflict("Patch document hash changed")
                    selected = next((node for node in nodes(doc.children) if not node.is_block and
                        node.value_start == op.start and node.value_end == op.end and node.ordinal == op.ordinal), None)
                    if selected is None:
                        raise ValueError("Patch must select an exact scalar occurrence")
                    replacement = base64.b64decode(op.replacement_base64, validate=True)
                    check = parse(b"probe = " + replacement + b"\n")
                    if len(replacement) > 4096 or not check.eligible or len(check.children) != 1 or check.children[0].is_block or check.children[0].value_end != 8 + len(replacement):
                        raise ValueError("Replacement must be exactly one supported scalar")
                    edits.append(Edit(op.start, op.end, replacement, op.old_hash, op.ordinal))
                    used.add(index)
                updated = apply_edits(doc, edits, doc.sha256)
                file.update(bytes_base64=base64.b64encode(updated).decode(), text=updated.decode("utf-8"), sha256=self.store.blob(updated))
            if len(used) != len(args.operations):
                raise ValueError("Patch is outside the staged write set")
            new = copy.deepcopy(candidate)
            new.update(id=uid("candidate"), parent_candidate_id=candidate["id"], status="staged")
            new.pop("validation", None)
            new["plan"]["files"] = files
            new["plan"]["write_set"] = [{"owner": "generated_patch", "path": item["path"], "sha256": item["sha256"]} for item in files]
            with self.store.atomic():
                self._staging(args)
                new = self.store.put("candidate", new)
                workspace["candidate_id"] = new["id"]
                workspace["workspace_revision"] += 1
                workspace = self.store.put("lab_eu5_workspace", workspace, expected_revision=workspace["revision"])
            return self._workspace_data(workspace, new)
        return self._idempotent("patch", args, WorkspaceData, patch)

    @snapshot_read
    def workspace_diff(self, args):
        workspace, candidate, _ = self._staging(args)
        self._disclosure(self._snapshot(workspace["snapshot_id"]))
        rows = []
        for item in candidate["plan"].get("files", []):
            raw = base64.b64decode(item["bytes_base64"], validate=True)
            for offset in range(0, max(len(raw), 1), 4096):
                chunk = raw[offset:offset + 4096]
                rows.append({"path": item["path"], "sha256": item["sha256"], "size": len(raw),
                             "text": f"byte offset {offset}:\n" + chunk.decode("utf-8", errors="backslashreplace")})
        return self._page(rows, args, scope={"operation": "diff", "workspace": workspace["id"], "revision": workspace["workspace_revision"],
            "candidate_id": candidate["id"], "write_set": candidate["plan"].get("write_set", [])}, model=Diff)

    def validate_start(self, args):
        def validate():
            workspace, candidate, domain = self._staging(args)
            response = self.service.command({"command": "validate", "workspace_id": domain["id"],
                "payload": {"candidate_id": candidate["id"]}, "idempotency_key": uid("eu5_validate"), "expected_revision": domain["revision"]})
            return RunAccepted(run_id=response["job_id"], workspace_id=workspace["id"])
        return self._idempotent("validate", args, RunAccepted, validate)

    def deployment_prepare(self, args):
        def prepare():
            workspace, candidate, _ = self._staging(args)
            validation = candidate.get("validation")
            if not validation:
                raise ValueError("Validate this exact staging revision before preparing a plan")
            contents = {"workspace_id": workspace["id"], "workspace_revision": workspace["workspace_revision"],
                "candidate_id": candidate["id"], "candidate_hash": digest(canonical(candidate["plan"])),
                "snapshot_id": workspace["snapshot_id"], "manifest_hash": candidate["manifest_hash"],
                "profile_hash": candidate["profile_hash"], "decision_set_hash": candidate["decision_set_hash"],
                "target_id": args.target_id, "writes": candidate["plan"].get("write_set", []),
                "validation": validation, "external_apply_available": False}
            from toolkit_core.authorization import PlanManager
            plan = PlanManager(self.store).prepare("eu5_deployment", args.target_id,
                contents["candidate_hash"], {key: contents[key] for key in ("snapshot_id", "manifest_hash", "profile_hash", "decision_set_hash", "workspace_revision")},
                contents["writes"], details=contents, eligible=False)
            checksum = plan["plan_digest"]
            return PlanData(plan_id=plan["id"], plan_digest=checksum, workspace_id=workspace["id"],
                workspace_revision=workspace["workspace_revision"], experimental_export_eligible=validation["experimental_eligible"],
                limitations=["Prepared for inactive experimental export only", "Production deployment awaits scoped game conformance and Windows qualification"])
        return self._idempotent("prepare", args, PlanData, prepare)

    def workspace_export(self, args):
        def export():
            workspace, candidate, domain = self._staging(args)
            self._disclosure(self._snapshot(workspace["snapshot_id"]))
            response = self.service._export(domain, {"candidate_id": candidate["id"]}, lambda *args: None)
            artifact = self.store.get("artifact", response["artifact_id"])
            relative = Path(artifact["path"]).relative_to(self.service.root).as_posix()
            raw = self.store.fs.read_bytes(relative)
            return ExportData(artifact_id=artifact["id"], sha256=digest(raw), size=len(raw))
        return self._idempotent("export", args, ExportData, export)


def accept_trusted_decision(service, question_id: str, choice: str) -> dict:
    """Trusted terminal/native channel only; deliberately absent from MCP tools."""
    with service.store.atomic():
        question = service.store.get("lab_eu5_question", question_id)
        if question["state"] == "answered" and question.get("selected_choice") == choice:
            return {"decision_id": question["decision_id"], "scan_id": question["snapshot_id"]}
        if question["state"] != "pending":
            raise Conflict("Question is no longer pending")
        bound = {key: value for key, value in question.items() if key not in {"id", "revision", "created_at", "updated_at", "question_hash"}}
        if digest(canonical(bound)) != question["question_hash"]:
            raise Conflict("The prepared question changed")
        if choice not in question["choices"]:
            raise ValueError("Choose one of the exact prepared alternatives")
        workspace = service.store.get("workspace", question["workspace_id"])
        scan = service._current_scan(workspace, question["snapshot_id"])
        EU5Operations(service, profile=getattr(service, "profile", "personal"))._disclosure(scan)
        if scan["manifest_hash"] != question["manifest_hash"] or scan["profile_hash"] != question["profile_hash"]:
            raise Conflict("Prepared choice no longer matches the current snapshot")
        result = service.decide(workspace, {**question["decision"], "choice": choice})
        question.update(state="answered", selected_choice=choice, decision_id=result["decision_id"], provenance="operator_selected")
        service.store.put("lab_eu5_question", question, expected_revision=question["revision"])
        intent = service.store.get("lab_eu5_intent", question["intent_id"])
        intent.update(state="operator_selected", operator_decision_id=result["decision_id"])
        service.store.put("lab_eu5_intent", intent, expected_revision=intent["revision"])
        return result


def build_operations(service, *, profile="dev", installations=None):
    from toolkit_core.contracts import OperationSpec
    operations = EU5Operations(service, profile=profile, installations=installations)
    definitions = [
        ("installations_list", PageInput, Page[Installation], True, "Page configured EU5 installations and synthetic fixtures."),
        ("mods_list", InstallationInput, Page[Mod], True, "Page registered mod IDs and captured fingerprints for one installation."),
        ("playsets_list", InstallationInput, Page[Playset], True, "Read configured mod order; this does not establish an active game playset."),
        ("scan_start", ScanInput, RunAccepted, False, "Capture registered sources and start a persistent EU5 scan. Poll lab_run_get."),
        ("findings_list", FindingsInput, Page[Finding], True, "Page evidence-backed findings from a pinned EU5 snapshot."),
        ("finding_get", FindingInput, RecordChunk, True, "Read a bounded JSON chunk of one snapshot-bound finding; follow its cursor."),
        ("source_read", SourceInput, SourceData, True, "Read at most 8192 captured source bytes by source ID, never a host path."),
        ("definitions_search", DefinitionsInput, Page[Definition], True, "Page indexed definition identities and contributor counts."),
        ("definition_compare", DefinitionInput, RecordChunk, True, "Read effective prediction, exact contributors and baseline as bounded JSON chunks."),
        ("references_list", DefinitionInput, Page[Reference], True, "Page known graph edges with explicit limits on dynamic/runtime coverage."),
        ("intent_record", IntentInput, IntentData, False, "Record proposed intent; gameplay sacrifices require a separate trusted operator decision."),
        ("workspace_create", WorkspaceCreateInput, WorkspaceData, False, "Create isolated revision-zero staging using the existing deterministic planner."),
        ("workspace_patch", PatchInput, WorkspaceData, False, "Apply exact hash/span/ordinal-preconditioned scalar edits to staging only."),
        ("workspace_diff", WorkspaceReadInput, Page[Diff], True, "Read every proposed output in bounded chunks for an exact staging revision."),
        ("validate_start", MutationInput, RunAccepted, False, "Start complete final-stack validation of a frozen staging revision."),
        ("deployment_prepare", PrepareInput, PlanData, False, "Prepare an immutable review plan; production deployment remains unavailable."),
        ("workspace_export", MutationInput, ExportData, False, "Export a validated inactive experimental ZIP. Never activates a playset."),
    ]
    specs = [OperationSpec(name="eu5_" + name, description=description, input_model=inputs, output_model=outputs,
            handler=getattr(operations, name), read_only=read_only, effect="observation" if read_only else "internal")
            for name, inputs, outputs, read_only, description in definitions]
    async def intent_with_confirmation(args, context):
        result = operations.intent_record(args)
        if result.question_id is None or context is None:
            return result
        question = service.store.get("lab_eu5_question", result.question_id)
        if question["state"] == "answered":
            result.state, result.decision_id = "operator_selected", question["decision_id"]
            return result
        message = ("Choose the exact contribution to retain for " + question["decision"]["definition_id"]
            + ". Competing requirements for these fields will be explicitly waived. Other contributions remain required.\n"
            + json.dumps(question["alternatives"], ensure_ascii=False))
        operations._disclosure(operations._snapshot(question["snapshot_id"]))
        if len(message.encode("utf-8")) > 24 * 1024:
            result.limitations.append("This choice needs bounded evidence review and a trusted terminal decision; its native form exceeds the message budget.")
            return result
        selected = await context.choose(result.question_id, message, result.choices)
        if selected is not None:
            accepted = accept_trusted_decision(service, result.question_id, selected)
            result.state, result.decision_id = "operator_selected", accepted["decision_id"]
            result.limitations = []
        return result
    return [OperationSpec(name=spec.name, description=spec.description, input_model=spec.input_model,
            output_model=spec.output_model, handler=intent_with_confirmation, requires_context=True,
            effect=spec.effect, read_only=spec.read_only) if spec.name == "eu5_intent_record" else spec for spec in specs]
