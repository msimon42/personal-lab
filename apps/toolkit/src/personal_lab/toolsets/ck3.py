"""Direct CK3 adapter. Sources, native receipts and repair gates remain in domain services."""
from __future__ import annotations

import os
import re
import time
from typing import Literal

from pydantic import Field, create_model

from toolkit_core.contracts import LabError, OperationSpec, StrictModel
from toolkit_core.storage import canonical, digest


from . import ck3_models as outputs


class ContextInput(StrictModel):
    selection: Literal["current", "resume"] = "current"
    workspace_id: str | None = None
    playset_id: str | None = None


class CheckInput(StrictModel):
    context_id: str | None = None
    goal: str | None = Field(default=None, max_length=2000)


class FindPlaysets(StrictModel):
    playset_id: str | None = Field(default=None, max_length=200)


class ScanInput(StrictModel):
    workspace_id: str


class ImportInput(ScanInput):
    discovery_id: str


class IndexInput(ScanInput):
    scan_id: str


class JobInput(StrictModel):
    job_id: str


class CancelInput(ScanInput):
    job_id: str


class PrepareInput(ScanInput):
    index_id: str
    issue_id: str
    combined_base64: str | None = Field(default=None, max_length=65536)


class ResumeInput(StrictModel):
    workspace_id: str | None = None


def build_operations(application):
    from backend.agent import contracts as inputs
    from backend.agent.approvals import sign, signing_key
    from backend.agent.release import installation_status, release_info

    service = application.service
    loaded = service.agent_release["fingerprint"]

    def public_names(result, operation):
        # Rewrite only application-authored routing metadata. Captured text,
        # saved discussion, definitions and evidence contents remain untouched.
        if operation == "context" and result.get("next_step"):
            result["next_step"] = re.sub(r"(?<![\w])check_playset(?![\w])", "ck3_check_playset", result["next_step"])
        if result.get("missing_mods_tool") == "get_investigation_report":
            result["missing_mods_tool"] = "ck3_get_investigation_report"
        if operation == "concern_evidence":
            for related in result.get("related", []):
                continuation = related.get("continuation")
                if isinstance(continuation, dict) and continuation.get("tool") == "related_definitions":
                    continuation["tool"] = "ck3_related_definitions"
        return result

    def current_release():
        now, installed = release_info(), installation_status()
        if now["fingerprint"] != loaded or installed["status"] == "mismatch":
            raise LabError("STALE_INPUT", "The loaded toolkit and installed CK3 workflow differ. Reconnect the matching release before preparing or approving changes.")

    def agent(operation):
        return lambda args: public_names(service.agent_request(operation,
            args.model_dump(exclude_none=True, exclude={"idempotency_key"})), operation)

    def command(operation):
        def execute(args):
            data = args.model_dump(exclude_none=True, exclude={"idempotency_key"})
            workspace_id = data.pop("workspace_id")
            return service.command({"command": operation, "workspace_id": workspace_id, "payload": data,
                                    "idempotency_key": args.idempotency_key})
        return execute

    def context(args):
        values = args.model_dump(exclude_none=True, exclude={"idempotency_key"})
        if application.configuration.ck3_locations:
            values["locations"] = application.configuration.ck3_locations
        result = public_names(service.agent_request("context", values), "context")
        result["connection"] = installation_status()
        return result

    def check(args):
        context_id = args.context_id
        if context_id is None:
            briefing = context(ContextInput())
            if briefing["state"] != "ready":
                return briefing
            context_id = briefing["id"]
        return service.agent_request("check", {"context_id": context_id, "goal": args.goal})

    def discover(args):
        platform = "windows" if os.name == "nt" else "macos"
        workspace = next((row for row in service.store.list("workspace", limit=1000)
                          if row.get("game_id") == "ck3" and row.get("target_platform") == platform), None)
        if workspace is None:
            workspace = service.command({"command": "create_workspace", "payload": {"name": "CK3 mod conversation",
                "mode": "game", "game_id": "ck3", "target_platform": platform},
                "idempotency_key": "ck3-workspace:" + args.idempotency_key})["workspace"]
        payload = {"platform": platform, **application.configuration.ck3_locations}
        if args.playset_id:
            payload["playset_id"] = args.playset_id
        return {**service.command({"command": "discover_ck3", "workspace_id": workspace["id"],
            "payload": payload, "idempotency_key": args.idempotency_key}), "workspace_id": workspace["id"]}

    def pending_question(workspace_id, proposal_id, purpose):
        # SDK resolvers rerun before the tool body. Reuse a still-current
        # challenge so a multi-round-trip answer stays tied to one nonce.
        proposal = service.store.get("agent_proposal", proposal_id)
        for question in service.store.list("agent_question", workspace_id, limit=1000):
            if (question.get("proposal_id") == proposal_id and question.get("purpose") == purpose
                    and question.get("proposal_hash") == proposal["content_hash"]
                    and question.get("state") == "pending" and question.get("expires_at", 0) > time.time()
                    and not question.get("relocation_historical")):
                return question
        return service.agent_request("challenge", {"workspace_id": workspace_id, "proposal_id": proposal_id,
                                                    "purpose": purpose})

    def user_answer(purpose):
        async def answer(args, native=None):
            current_release()
            if purpose == "choose":
                proposal = service.agent_request("proposal", {"workspace_id": args.workspace_id,
                                                               "proposal_id": args.proposal_id})
                labels = {option["label"]: option["id"] for option in proposal["options"]}
                message = proposal["title"] + "\n\n" + "\n\n".join(
                    f"{option['label']}. {option['effect']}" for option in proposal["options"])
                message += "\n\nSeparate inactive files only. Downloaded mods remain unchanged."
            else:
                labels = {"Yes, continue": "Yes, continue", "No, leave it unchanged": "No, leave it unchanged"}
                message = "Undo this exact inactive change, preserving downloaded mods and intervening user edits?"
            question = pending_question(args.workspace_id, args.proposal_id, purpose)
            selected = await native.choose(question["id"], message, list(labels)) if native else None
            action = getattr(native, "action", "unavailable")
            if action == "unavailable":
                return {"state": "awaiting_operator", "question_id": question["id"],
                        "next_step": "Use lab decide " + question["id"] + " in a trusted terminal."}
            current_release()
            if action == "accept" and selected not in labels:
                raise LabError("VALIDATION_FAILED", "The native response did not select an offered choice.")
            response = {"question_id": question["id"], "nonce": question["nonce"],
                "proposal_hash": question["proposal_hash"], "action": action,
                "choice": labels.get(selected)}
            if purpose != "choose" and selected == "No, leave it unchanged":
                response["action"] = "decline"
            result = service.agent_request("answer", {"answer": response,
                "signature": sign(signing_key(service.store), response)})
            if purpose == "undo" and action == "accept" and selected == "Yes, continue":
                return service.agent_request("undo", {"workspace_id": args.workspace_id,
                                                       "proposal_id": args.proposal_id})
            return result
        return answer

    def resume(args):
        if args.workspace_id is None:
            return context(ContextInput(selection="resume"))
        session = service.agent_request("session", {"workspace_id": args.workspace_id})
        if session.get("proposal_id"):
            try:
                session["proposal"] = service.agent_request("proposal", {"workspace_id": args.workspace_id,
                                                                         "proposal_id": session["proposal_id"]})
            except ValueError:
                session["proposal"] = {"state": "requires_fresh_review"}
        return session

    descriptions = {
        "get_context": "Start or resume CK3 discussion with saved work, source freshness and capability limits.",
        "list_missing_mods": "Page unavailable enabled mods from the saved briefing.",
        "check_playset": "Capture and check current CK3 files; returns persistent work without launching the game.",
        "remember_progress": "Save the objective, discussion notes and next step; notes never grant approval.",
        "search_knowledge": "Search sourced CK3 guidance with explicit version applicability.",
        "read_knowledge": "Read a cited CK3 guidance entry; documentation is distinct from executed conformance.",
        "search_sources": "Search captured paths, text or symbols and follow all continuation fields.",
        "read_source": "Read a bounded hash-checked source excerpt under current source policy.",
        "related_definitions": "Inspect saved contributions and supported references with explicit unknowns.",
        "get_definition_contributions": "Page every captured declaration of one exact definition.",
        "investigate_change": "Read a concise contribution comparison with base-game evidence and remedy limits.",
        "read_sources": "Read bounded captured excerpts together under source exclusions.",
        "list_concerns": "Page all saved concerns with complete review accounting.",
        "get_concern_evidence": "Read bounded combined concern evidence; follow every continuation.",
        "record_concern_review": "Record a revision-checked evidence-backed review without authorizing changes.",
        "get_investigation_report": "Resume saved concern totals and remaining work without rescanning.",
        "find_missing_mod_candidates": "Search configured local roots for missing mod candidates without changing the launcher.",
        "inspect_mod_candidate": "Capture an operator-selected alternative candidate separately from the active selection.",
        "set_version_preference": "Remember campaign-version preference without approving updates.",
        "find_playsets": "Discover CK3 playsets using trusted startup roots; does not activate a selection.",
        "use_playset": "Import a discovered playset for investigation without activating it.",
        "scan_playset": "Capture immutable selected CK3 sources and start a persistent scan.",
        "get_progress": "Inspect persistent CK3 work; lab_run_get is the shared cross-toolset equivalent.",
        "cancel_work": "Request safe cancellation without changing source mods.",
        "examine_changes": "Build a complete saved conversation index from the selected scan.",
        "list_changes": "Page all indexed changes, including exact totals and explicit coverage.",
        "inspect_change": "Inspect bounded saved contributions under current disclosure policy.",
        "prepare_call_allies_choices": "Prepare exact inactive call-allies options; this grants no approval.",
        "ask_about_changes": "Collect a trusted native operator choice, with a separate terminal fallback.",
        "make_selected_files": "Create only previously approved files in a separate inactive fixes mod.",
        "undo_selected_changes": "Ask before undoing an exact inactive change; preserve intervening edits.",
        "resume_conversation": "Resume saved discussion and pending choices without assuming completion.",
    }
    reads = {
        "list_missing_mods": (inputs.MissingMods, agent("missing_mods")),
        "search_knowledge": (inputs.KnowledgeSearch, agent("search_knowledge")),
        "read_knowledge": (inputs.KnowledgeRead, agent("read_knowledge")),
        "search_sources": (inputs.SourceSearch, agent("search_sources")),
        "read_source": (inputs.SourceRead, agent("read_source")),
        "related_definitions": (inputs.Related, agent("related_definitions")),
        "get_definition_contributions": (inputs.DefinitionContributions, agent("definition_contributions")),
        "investigate_change": (inputs.Brief, agent("brief")),
        "read_sources": (inputs.SourcesRead, agent("read_sources")),
        "list_concerns": (inputs.Concerns, agent("concerns")),
        "get_concern_evidence": (inputs.ConcernEvidence, agent("concern_evidence")),
        "get_investigation_report": (inputs.InvestigationReport, agent("investigation_report")),
        "get_progress": (JobInput, lambda args: application.project_run(args.job_id)),
        "list_changes": (inputs.Issues, agent("issues")),
        "inspect_change": (inputs.Inspect, agent("inspect")),
    }
    mutations = {
        "get_context": (ContextInput, context), "check_playset": (CheckInput, check),
        "remember_progress": (inputs.Remember, agent("remember")),
        "record_concern_review": (inputs.Review, agent("record_review")),
        "find_missing_mod_candidates": (inputs.MissingMods, agent("missing_candidates")),
        "inspect_mod_candidate": (inputs.CandidateInspection, agent("inspect_candidate")),
        "set_version_preference": (inputs.VersionPreference, agent("version_preference")),
        "find_playsets": (FindPlaysets, discover), "use_playset": (ImportInput, command("import_ck3")),
        "scan_playset": (ScanInput, command("scan")), "cancel_work": (CancelInput, command("cancel")),
        "examine_changes": (IndexInput, command("agent_index")),
        "prepare_call_allies_choices": (PrepareInput, command("agent_prepare")),
        "make_selected_files": (inputs.Proposal, agent("apply")),
        "resume_conversation": (ResumeInput, resume),
    }

    def guarded(name, handler):
        def response_scope(payload, result):
            objects = [payload, result]
            objects.extend(result[key] for key in ("context", "conversation", "proposal")
                           if isinstance(result.get(key), dict))
            scope = application._scope("ck3", {})
            scope["release_fingerprint"] = loaded
            seen = set()

            def append(binding):
                identity = binding["kind"], binding["id"]
                if identity not in seen:
                    scope["bindings"].append(binding)
                    seen.add(identity)

            def bind_selectors(selectors):
                try:
                    for binding in application._scope("ck3", selectors)["bindings"]:
                        append(binding)
                except (KeyError, LabError):
                    # A legitimate fresh response may explicitly redact an
                    # unavailable historical scope, or acknowledge a queued
                    # operation whose domain checks will report failure. Keep
                    # that first result; never replay it without a proven scope.
                    scope["unavailable_binding"] = True

            def bind_record(kind, identifier):
                try:
                    header = service.store.record_header(kind, identifier)
                except KeyError:
                    scope["unavailable_binding"] = True
                else:
                    append({"kind": kind, "id": header["id"], "revision": header["revision"]})

            for obj in objects:
                selectors = {key: obj[key] for key in ("workspace_id", "context_id", "index_id", "snapshot_id")
                             if obj.get(key)}
                if obj.get("scan_id"):
                    selectors["snapshot_id"] = obj["scan_id"]
                if obj.get("source_status") is not None and obj.get("id"):
                    selectors["context_id"] = obj["id"]
                bind_selectors(selectors)
                for key, selector in (("notes_scan_id", "snapshot_id"), ("notes_index_id", "index_id")):
                    if obj.get(key):
                        bind_selectors({selector: obj[key]})
                for key, kind in (("saved_scan", "scan"), ("saved_index", "agent_index")):
                    saved = obj.get(key)
                    if isinstance(saved, dict) and saved.get("id"):
                        bind_record(kind, saved["id"])
                for identifier, kind in ((obj.get("proposal_id"), "agent_proposal"),
                        (obj.get("id") if str(obj.get("id", "")).startswith("conversation_") else None, "agent_session")):
                    if identifier:
                        bind_record(kind, identifier)
            # Context summaries omit the session ID intentionally. Bind its
            # small row header without decoding notes or full scan payloads.
            workspace_ids = [binding["id"] for binding in scope["bindings"] if binding["kind"] == "workspace"]
            for workspace_id in workspace_ids:
                rows = service.store.connection.execute(
                    "SELECT id,revision FROM records WHERE kind='agent_session' AND workspace_id=?", (workspace_id,)).fetchall()
                for row in rows:
                    append({"kind": "agent_session", "id": row["id"], "revision": row["revision"]})
            return scope

        def execute(args):
            if name in {"prepare_call_allies_choices", "make_selected_files"}:
                current_release()
            payload = args.model_dump(exclude={"idempotency_key"}, exclude_none=True)
            hashed = digest(canonical({"payload": payload, "source_policy": service.inference_policy()}))
            key = f"{application.instance_id}:ck3:{name}:{args.idempotency_key}"
            with service.command_lock, service.store.atomic():
                cached = service.store.cached_request(key, hashed)
                if cached is not None:
                    if (cached.get("format") != "ck3-scoped-response-1" or not isinstance(cached.get("data"), dict)
                            or not isinstance(cached.get("scope"), dict) or cached["scope"].get("unavailable_binding")
                            or cached["scope"].get("release_fingerprint") != loaded
                            or not application._authorize_artifact({"scope": cached["scope"]})):
                        raise LabError("STALE_INPUT", "The saved response scope changed. Inspect current saved work; the original operation was not repeated.")
                    return cached["data"]
                result = handler(args)
                scope = response_scope(payload, result)
                service.store.cache_request(key, hashed, {"format": "ck3-scoped-response-1", "scope": scope, "data": result})
                return result
        return execute

    result = []
    output_models = {
        "get_context": outputs.ContextData, "check_playset": outputs.CheckData,
        "remember_progress": outputs.SessionData, "resume_conversation": outputs.ResumeData,
        "list_missing_mods": outputs.MissingModsData, "search_knowledge": outputs.KnowledgeSearchData,
        "read_knowledge": outputs.KnowledgeEntryData, "search_sources": outputs.SearchData,
        "read_source": outputs.SourceData, "related_definitions": outputs.RelatedData,
        "get_definition_contributions": outputs.ContributionData, "investigate_change": outputs.BriefData,
        "read_sources": outputs.SourcesData, "list_concerns": outputs.ConcernsData,
        "get_concern_evidence": outputs.ConcernEvidenceData, "record_concern_review": outputs.ReviewData,
        "get_investigation_report": outputs.ReportData, "find_missing_mod_candidates": outputs.CandidateSearchData,
        "inspect_mod_candidate": outputs.QueuedWork, "set_version_preference": outputs.VersionPreferenceData,
        "find_playsets": outputs.QueuedWork, "use_playset": outputs.ImportData,
        "scan_playset": outputs.QueuedWork, "get_progress": outputs.WorkData,
        "cancel_work": outputs.CancellationData, "examine_changes": outputs.QueuedWork,
        "list_changes": outputs.IssuePage, "inspect_change": outputs.IssueData,
        "prepare_call_allies_choices": outputs.QueuedWork, "make_selected_files": outputs.QueuedWork,
        "ask_about_changes": outputs.ChoiceData, "undo_selected_changes": outputs.CheckData,
    }
    for name, (model, handler) in reads.items():
        result.append(OperationSpec("ck3_" + name, descriptions[name], model, output_models[name], handler,
                                    read_only=True, effect="observation"))
    for name, (model, handler) in mutations.items():
        augmented = create_model("CK3" + "".join(part.title() for part in name.split("_")) + "Input", __base__=model,
                                 idempotency_key=(str, Field(min_length=1, max_length=128)))
        result.append(OperationSpec("ck3_" + name, descriptions[name], augmented, output_models[name], guarded(name, handler)))
    for name, purpose in (("ask_about_changes", "choose"), ("undo_selected_changes", "undo")):
        result.append(OperationSpec("ck3_" + name, descriptions[name], inputs.Proposal, outputs.ChoiceOutcome,
                                    user_answer(purpose), requires_context=True))
    return result
