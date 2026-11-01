"""One application boundary for CLI, MCP, and direct deterministic tests."""
from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path
from functools import partial

import anyio

from pydantic import ValidationError

from toolkit_core.artifacts import ArtifactManager
from toolkit_core.contracts import Error, LabError, Result
from toolkit_core.storage import canonical, digest

from .profiles import PROFILES, Configuration, load_configuration


MAX_RESULT_BYTES = 32 * 1024
MAX_INPUT_BYTES = 256 * 1024


class LabApplication:
    def __init__(self, state_dir: Path, profile="dev", configuration: Configuration | None = None, service=None):
        from .migration import validate_state_ready
        from .registry import build_operations

        if profile not in PROFILES:
            raise ValueError("Unknown startup profile.")
        validate_state_ready(state_dir)
        self.profile = PROFILES[profile]
        self.configuration = configuration or Configuration()
        self.service = service
        self.owns_runtime = service is None
        if service is not None:
            self.runtime = service
        elif any(name in self.profile.toolsets for name in ("eu5", "ck3")):
            from backend.app.service import Service
            self.runtime = self.service = Service(Path(state_dir), profile=profile, initialize_default=False)
        else:
            from toolkit_core.jobs import RunManager
            self.runtime = RunManager(Path(state_dir), profile=profile)
        self.store, self.instance_id = self.runtime.store, self.runtime.instance_id
        self.artifacts = ArtifactManager(self.store, self.instance_id, authorize=self._authorize_artifact)
        try:
            self.operations = build_operations(self)
        except BaseException:
            if self.owns_runtime:
                self.runtime.close()
            raise

    def _scope(self, toolset, arguments):
        if toolset == "lab" and arguments.get("run_id"):
            run = self.project_run(arguments["run_id"])
            toolset = run.get("game_id", "core")
            arguments = {**arguments, "workspace_id": run.get("workspace_id")}
        elif toolset == "lab":
            toolset = "core"
        scope = {"toolset": toolset, "profile": self.profile.name}
        if toolset in {"eu5", "ck3"} and self.service is not None:
            scope["policy_digest"] = self._policy_digest()
            identifiers = {key: value for key, value in arguments.items()
                           if key in {"workspace_id", "index_id", "snapshot_id", "context_id"} and value}
            scope["bindings"] = []
            seen = set()

            def bind_header(kind, record):
                identity = kind, record["id"]
                if identity not in seen:
                    scope["bindings"].append({"kind": kind, "id": record["id"], "revision": record["revision"]})
                    seen.add(identity)
            for key, identifier in identifiers.items():
                kinds = {"workspace_id": ("workspace", "lab_eu5_workspace"), "index_id": ("agent_index",),
                         "snapshot_id": ("scan",), "context_id": ("agent_context",)}[key]
                for kind in kinds:
                    try:
                        record = self.store.record_header(kind, identifier)
                        break
                    except KeyError:
                        continue
                else:
                    raise LabError("PATH_DENIED", "Artifact source binding is unavailable.")
                bind_header(kind, record)
                if record.get("workspace_id"):
                    bind_header("workspace", self.store.record_header("workspace", record["workspace_id"]))
        return scope

    def _policy_digest(self):
        return digest(canonical({key: value for key, value in self.service.inference_policy().items()
                                 if key != "key_configured"}))

    def _require_response_policy(self, captured):
        if captured is not None and self._policy_digest() != captured:
            raise LabError("STALE_INPUT", "Source permissions changed while forming the response. No result content was returned; inspect saved work before retrying.")

    def _authorize_artifact(self, record):
        scope = record["scope"]
        if scope.get("toolset") not in self.profile.toolsets or scope.get("profile") != self.profile.name:
            return False
        if self.service is None or scope.get("policy_digest") != self._policy_digest():
            return False
        try:
            return all(self.store.record_header(binding["kind"], binding["id"])["revision"] == binding["revision"]
                       for binding in scope.get("bindings", []))
        except (KeyError, ValueError):
            return False

    def project_run(self, run_id):
        policy_before = self._policy_digest() if self.service is not None else None
        record = self.runtime.run_get(run_id)
        game = record.get("game_id")
        if not game:
            try:
                legacy = self.store.get("job", run_id)
                game = "eu5"
                if legacy.get("workspace_id"):
                    game = self.store.get("workspace", legacy["workspace_id"]).get("game_id", "eu5")
                record = {**record, "game_id": game}
            except KeyError:
                pass
        if game and game not in self.profile.toolsets:
            raise LabError("PATH_DENIED", "This run belongs to a disabled toolset.")
        if game and self.service is not None:
            captured = record.get("source_policy_hash")
            policy_after = self._policy_digest()
            policy_changed = (policy_before != policy_after
                              or captured is not None and captured != policy_after)
            excluded = bool(self.service.inference_policy()["excluded_patterns"])
            if policy_changed or excluded:
                # Generic job history has no per-field source projection. Even
                # an equal policy hash cannot prove its arbitrary result was
                # constructed under exclusions by an older optional adapter.
                safe_result = {}
                for field, prefix in {"scan_id": "scan", "index_id": "index", "candidate_id": "candidate",
                        "proposal_id": "proposal", "workspace_id": "workspace", "discovery_id": "discovery",
                        "investigation_id": "investigation"}.items():
                    value = (record.get("result") or {}).get(field)
                    if isinstance(value, str) and re.fullmatch(prefix + r"_[a-f0-9]{8,64}", value):
                        safe_result[field] = value
                outcome = (record.get("result") or {}).get("state")
                if ((record.get("command") == "agent_undo" and outcome == "undone")
                        or (record.get("command") == "agent_apply" and outcome == "prepared")):
                    safe_result["state"] = outcome
                    safe_result["enabled"] = False
                record = {key: value for key, value in record.items() if key in {
                    "id", "run_id", "state", "instance_id", "game_id", "target_platform", "command",
                    "created_at", "updated_at", "workspace_id", "profile", "progress"}}
                record.update(result=safe_result or None, result_disclosure={"state": "withheld",
                    "reason": "Current source policy does not authorize this historical run payload."})
        return record

    def project_events(self, run_id, cursor=0, limit=50):
        policy_before = self._policy_digest() if self.service is not None else None
        run = self.project_run(run_id)
        page = self.runtime.run_events(run_id, cursor, limit)
        final = self.project_run(run_id)
        changed = self.service is not None and policy_before != self._policy_digest()
        if (changed or run.get("result_disclosure", {}).get("state") == "withheld"
                or final.get("result_disclosure", {}).get("state") == "withheld"):
            page["events"] = [{"sequence": event["sequence"], "data": {
                key: value for key, value in event["data"].items() if key in {"id", "state", "progress", "created_at", "updated_at"}}}
                for event in page["events"]]
            page["disclosure"] = {"state": "withheld",
                "reason": "Current source policy does not authorize the full historical event payload."}
        return page

    def cancel_run(self, run_id):
        self.project_run(run_id)
        return self.runtime.cancel_run(run_id)

    def validate_arguments(self, operation, arguments):
        try:
            size = len(canonical(arguments))
        except (TypeError, ValueError):
            raise LabError("VALIDATION_FAILED", "Operation arguments must contain valid JSON values.") from None
        if size > MAX_INPUT_BYTES:
            raise LabError("VALIDATION_FAILED", "Operation arguments exceed the 262144-byte application input limit.")
        return operation.input_model.model_validate(arguments)

    def _failure(self, envelope, code, message, *, retryable=False, details=None, status="error"):
        message = str(message)[:1024]
        details = details or {}
        try:
            if len(canonical(details)) > 8192:
                details = {"details_omitted": True, "reason": "Error details exceed the bounded response limit."}
        except (TypeError, ValueError):
            details = {"details_omitted": True, "reason": "Error details are not JSON values."}
        return envelope(instance_id=self.instance_id, status=status, summary=message,
            error=Error(code=str(code)[:100], message=message, retryable=retryable, details=details))

    async def execute(self, name: str, arguments: dict, context=None):
        if name not in self.operations:
            raise KeyError("Tool is not enabled by this profile.")
        operation = self.operations[name]
        envelope = Result[operation.output_model]
        try:
            captured_policy = self._policy_digest() if self.service is not None else None
            args = self.validate_arguments(operation, arguments)
            call = partial(operation.handler, args, context) if operation.requires_context else partial(operation.handler, args)
            value = call() if inspect.iscoroutinefunction(operation.handler) else await anyio.to_thread.run_sync(call)
            if inspect.isawaitable(value):
                value = await value
            data = operation.output_model.model_validate(value)
            values = data.model_dump(mode="json")
            self._require_response_policy(captured_policy)
            status = "ok"
            if any(key in values for key in ("run_id", "job_id")) and values.get("state") not in {
                    "succeeded", "completed", "failed", "cancelled", "interrupted"}:
                status = "accepted"
            if values.get("state") in {"awaiting_operator", "awaiting_choice", "needs_input", "setup_needed", "needs_selection"}:
                status = "needs_input"
            result = envelope(instance_id=self.instance_id, summary=operation.description, data=data, status=status,
                              next_cursor=str(values["next_cursor"]) if values.get("next_cursor") is not None else None)
            if len(result.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
                # Store only the already policy-projected result. A later read
                # must still satisfy the same disclosure and source bindings.
                bindings = {**arguments, **{key: values[key] for key in
                    ("workspace_id", "index_id", "snapshot_id", "context_id") if values.get(key)}}
                if name == "ck3_get_context" and values.get("id"):
                    bindings["context_id"] = values["id"]
                scope = self._scope(name.split("_", 1)[0], bindings)
                if scope.get("toolset") in {"eu5", "ck3"}:
                    if scope.get("policy_digest") != captured_policy:
                        self._require_response_policy(captured_policy)
                        raise LabError("STALE_INPUT", "Artifact disclosure scope changed while forming the response.")
                    # Retain the epoch under which the source projection was
                    # produced. Never relabel old bytes to a later policy.
                    scope["policy_digest"] = captured_policy
                self._require_response_policy(captured_policy)
                artifact = self.artifacts.register_bytes(canonical(values), "application/json",
                    run_id=values.get("run_id", values.get("job_id")),
                    scope=scope)
                result = envelope(instance_id=self.instance_id, status=status,
                    summary="Complete result is available through bounded lab_artifact_read calls.", artifacts=[artifact],
                    limitations=["Result exceeds the 32768-byte response budget; no result fields were discarded."])
            self._require_response_policy(captured_policy)
            return result
        except LabError as exc:
            return self._failure(envelope, exc.code, str(exc), retryable=exc.retryable, details=exc.details,
                status="blocked" if exc.code in {"CAPABILITY_UNAVAILABLE", "APPROVAL_REQUIRED", "RECOVERY_REQUIRED", "PATH_DENIED"} else "error")
        except ValidationError as exc:
            return self._failure(envelope, "VALIDATION_FAILED", "Operation contract validation failed.",
                details={"fields": [".".join(map(str, row["loc"]))[:256] for row in exc.errors()[:50]],
                         "total_errors": exc.error_count()})
        except (ValueError, KeyError, OSError) as exc:
            from toolkit_core.storage import Conflict
            code = "STALE_INPUT" if isinstance(exc, Conflict) else "VALIDATION_FAILED"
            return self._failure(envelope, code, str(exc))
        except Exception as exc:
            logging.getLogger(__name__).error("Operation %s failed (%s)", name, type(exc).__name__)
            return self._failure(envelope, "INTERNAL_ERROR", "The operation failed unexpectedly; inspect local diagnostics before retrying.")

    def contracts(self):
        return [{"name": spec.name, "description": spec.description,
                 "input_schema": spec.input_model.model_json_schema(),
                 "output_schema": Result[spec.output_model].model_json_schema(),
                 "effect": spec.effect, "read_only": spec.read_only}
                for spec in self.operations.values()]

    def close(self):
        if self.owns_runtime:
            self.runtime.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def create_application(state_dir: Path | None = None, *, profile="dev", config_path: Path | None = None):
    if state_dir is None:
        from .migration import default_state_dir
        state_dir = default_state_dir("personal")
    return LabApplication(Path(state_dir), profile, load_configuration(config_path))


def administrative_application(state_dir: Path, *, profile="personal"):
    """Read projections while another process owns execution; starts no worker."""
    from types import SimpleNamespace
    from toolkit_core.storage import Store
    from toolkit_core.jobs import run_projection
    from .migration import validate_state_ready

    validate_state_ready(state_dir)
    if not (Path(state_dir) / "workspace.sqlite3").is_file():
        raise ValueError("No saved instance exists at the selected state directory.")
    application = LabApplication.__new__(LabApplication)
    application.profile, application.configuration = PROFILES[profile], Configuration()
    application.store = Store(state_dir)
    application.instance_id = application.store.get("configuration", "lab-instance")["instance_id"]
    application.owns_runtime = False
    application.service = None
    if any(name in application.profile.toolsets for name in ("eu5", "ck3")):
        from .cli import _administrative_service
        application.service = _administrative_service(application.store)
    def events(run_id, cursor=0, limit=50):
        run_projection(application.store, run_id, application.instance_id)
        if not 0 <= cursor or not 1 <= limit <= 200:
            raise ValueError("Invalid event range.")
        rows = application.store.events(run_id, cursor)[:limit]
        return {"events": rows, "next_cursor": rows[-1]["sequence"] if len(rows) == limit else None}
    application.runtime = SimpleNamespace(run_get=lambda run_id: run_projection(application.store, run_id, application.instance_id),
                                          run_events=events)
    return application
