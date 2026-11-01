"""Small startup briefings and resumable playset checks, never gameplay authority."""
from __future__ import annotations

import json
import sys
import time

from backend.app.store import Conflict, uid
from .issues import INDEX_VERSION
from .session_projection import BINDINGS_KEY, SessionProjection


def capabilities():
    return {
        "inspection": {"available": True, "reason": "Check available files and investigate shared changes."},
        "source_search": {"available": True, "reason": "Search saved scripts and related definitions."},
        "inactive_preparation": {"available": True, "scope": "call_ally_interaction",
            "reason": "Prepare reviewed call-allies choices in a separate inactive mod; approval is required."},
        "game_test": {"available": False, "limitation_kind": "application",
            "reason": "A separate CK3 gameplay runner has not yet been qualified. Do not offer to launch it."},
        "activation": {"available": False, "limitation_kind": "application",
            "reason": "Enabling fixes in a normal playset is not implemented and qualified."},
        "undo_inactive": {"available": True, "reason": "Retire an approved inactive change and retain its saved copy."},
        "undo_live": {"available": False, "limitation_kind": "application",
            "reason": "Live playset activation and restoration have not been qualified."},
        "other_repairs": {"available": False, "limitation_kind": "application",
            "reason": "Other repair families need their own supported preparation path."},
    }


def headers(store, kind, fields, *, workspace_id=None, identifier=None, limit=1000):
    """Select small JSON fields in SQLite, without materializing huge saved scans."""
    fields = tuple(dict.fromkeys(("id", *fields)))
    query = "SELECT json_object(" + ",".join("?,json_extract(payload,?)" for _ in fields) + ") FROM records WHERE kind=?"
    values = [v for field in fields for v in (field, "$." + field)] + [kind]
    for column, value in (("workspace_id", workspace_id), ("id", identifier)):
        if value is not None:
            query += f" AND {column}=?"
            values.append(value)
    query += " ORDER BY updated DESC,id LIMIT ?"
    values.append(min(limit, 1000))
    with store.lock:
        store._check_database()
        return [json.loads(row[0]) for row in store.connection.execute(query, values)]


def selection_identity(discovery):
    installation = discovery.get("installation") or {}
    return (discovery.get("selected_playset_id"), discovery.get("user_data"),
            installation.get("path") or installation.get("content_root"))


def public_context(context, service, *, projection=None):
    view = projection or SessionProjection(service)
    if context.get("workspace_id") and view.workspace is None:
        view.watch_workspace(service.store.get("workspace", context["workspace_id"]))
    missing = context["source_status"]["missing_enabled_mods"]
    from collections import Counter
    preflight = context.get("preflight", {})
    versions = preflight.get("versions", {})
    mods = versions.get("mods", [])
    ordered = sorted(mods, key=lambda mod: (mod["relation"] != "version_question", mod.get("owner", "")))
    result = {**context, "preflight": {
        "versions": {**versions, "mods": ordered[:20], "total": len(mods),
                     "counts": dict(Counter(mod["relation"] for mod in mods)), "truncated": len(mods) > 20,
                     "follow_up": "Read captured descriptors in concern evidence for full per-mod declarations."},
        "known_search_locations": len(preflight.get("search_scope", [])),
        "scope": preflight.get("scope"), "version": preflight.get("version")},
        "source_status": {**context["source_status"], "missing_enabled_mods": missing[:5],
        "missing_enabled_mods_total": len(missing), "missing_mods_next_offset": 5 if len(missing) > 5 else None}}
    if context.get("conversation"):
        result["conversation"] = view.project({"workspace_id": context.get("workspace_id"),
            "game_id": context.get("game_id"), "target_platform": context.get("target_platform"),
            **context["conversation"]})
        notes = result["conversation"]
        notes["applies_to_current_scan"] = bool(notes.get("notes") and
            notes["text_disclosure"]["fields"].get("notes", {}).get("state") == "available")
    captured_policy = result.pop("_disclosure_policy_hash", None)
    if result.get("proposal") and captured_policy != view.policy_hash:
        result["proposal"] = {"state": "requires_fresh_review"}
    view.validate()
    return result


class AssistantContext:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def get(self, selection="current", workspace_id=None, playset_id=None, locations=None):
        from .knowledge import knowledge_manifest
        from .preflight import briefing_preflight, version_preference, candidate_search_summary
        disclosure = SessionProjection(self.service)
        locations = locations or {}
        if playset_id and locations.get("playset_id") and playset_id != locations["playset_id"]:
            raise ValueError("Two different playsets were requested. Select one playset.")
        playset_id = playset_id or locations.get("playset_id")
        platform = locations.get("platform") or ("macos" if sys.platform == "darwin" else "windows")
        workspaces = [w for w in self.store.list("workspace", limit=1000)
                      if w.get("game_id") == "ck3" and w["mode"] == "game" and
                      (w["id"] == workspace_id or w.get("inspection_scope", {}).get("kind") != "alternative_candidate")]
        selected = None
        if workspace_id:
            selected = self.store.get("workspace", workspace_id)
            self.service._ck3_workspace(selected)
            platform = selected["target_platform"]
        workspaces = [w for w in workspaces if w["target_platform"] == platform]
        sessions = headers(self.store, "agent_session", ("workspace_id", "updated_at"))
        if selection == "resume" and selected is None:
            selected = next((w for s in sessions for w in workspaces if w["id"] == s["workspace_id"]), None)
        old = {}
        anchor = selected or (workspaces[0] if workspaces else None)
        if anchor and anchor.get("discovery_id"):
            old = self.store.get("discovery", anchor["discovery_id"])
        options = {**old.get("options", {}), **{k: v for k, v in locations.items() if v is not None}, "platform": platform}
        options.pop("playset_id", None)
        if playset_id:
            options["playset_id"] = playset_id
        elif selected and old.get("selected_playset_id"):
            options["playset_id"] = old["selected_playset_id"]
        manual = bool(selected and not selected.get("discovery_id") and selected.get("sources") and not locations)
        discovery, discovery_error = None, None
        if not manual:
            try:
                self.service._guard_discovery_roots(options)
                discovery = self.service._discover_selection(options)
                self.service._guard_discovery_roots(discovery.get("options", {}))
            except (ValueError, OSError) as exc:
                discovery_error = str(exc)
        if discovery:
            desired = discovery.get("selected_playset_id")
            last_launch = any(d.get("code") == "last_launch_only" for d in discovery.get("diagnostics", []))
            if selected is None or selection_identity(old) != selection_identity(discovery):
                selected = None
                for workspace in workspaces:
                    if not workspace.get("discovery_id"):
                        continue
                    saved = headers(self.store, "discovery", ("selected_playset_id", "user_data", "installation"),
                                    identifier=workspace["discovery_id"], limit=1)
                    if saved and (desired is not None or last_launch) and selection_identity(saved[0]) == selection_identity(discovery):
                        selected = workspace
                        break
        session = (self.store.list("agent_session", selected["id"], limit=1) or [None])[0] if selected else None
        if selected:
            disclosure.watch_workspace(selected)
        scan = headers(self.store, "scan", ("created_at", "discovery_id", "schema_hash", "parser_version"),
                       identifier=selected["generation"], limit=1) if selected and selected.get("generation") else []
        indexes = headers(self.store, "agent_index", ("scan_id", "version", "summary", "updated_at"),
                          workspace_id=selected["id"]) if selected else []
        index = next((i for i in indexes if scan and i["scan_id"] == scan[0]["id"] and i["version"] == INDEX_VERSION), None)
        jobs = self.store.list("job", selected["id"], limit=10) if selected else []
        pending = [{key: job.get(key) for key in ("id", "command", "state", "phase", "progress", "error")}
                   for job in jobs if job["state"] in {"queued", "running"} or job.get("phase") == "interrupted"]
        selected_id = discovery.get("selected_playset_id") if discovery else old.get("selected_playset_id")
        playsets = [{k: p.get(k) for k in ("id", "name", "active")} for p in (discovery or old).get("playsets", [])]
        playset = next((p for p in playsets if p["id"] == selected_id), None)
        last_launch = any(d.get("code") == "last_launch_only" for d in (discovery or {}).get("diagnostics", []))
        if last_launch:
            playset = {"id": None, "name": "Last launch selection", "active": None}
        mods = (discovery or old).get("mods", [])
        missing = [{"name": m.get("name") or "An unavailable mod", "owner": m["owner"], "reason": m.get("reason")}
                   for m in mods if m.get("enabled") and not m.get("available")]
        ready = manual or bool(discovery and discovery.get("consistent") and (selected_id is not None or last_launch) and discovery.get("installation"))
        state = "ready" if ready else "needs_selection" if discovery and playsets else "setup_needed"
        previous_notes = None
        if session:
            previous_notes = {k: session.get(k) for k in ("goal", "notes", "next_step", "reviewed_issue_ids", "index_id", "proposal_id", "state",
                "workspace_id", "game_id", "target_platform", "notes_scan_id", "notes_index_id", BINDINGS_KEY)}
            previous_notes["trust"] = "Conversation notes are context, not approval or game evidence."
            previous_notes["applies_to_current_scan"] = bool(scan and session.get("notes_scan_id") == scan[0]["id"])
        proposal = None
        if session and session.get("proposal_id"):
            from .workflow import AgentWorkflow
            try:
                proposal = AgentWorkflow(self.service).proposal(selected["id"], session["proposal_id"])
                proposal = {k: proposal.get(k) for k in ("id", "title", "state", "selected_option", "options", "testing", "enabled", "scan_id", "requires_fresh_choices")}
                proposal["applies_to_current_scan"] = bool(scan and proposal["scan_id"] == scan[0]["id"])
            except (ValueError, KeyError):
                proposal = {"state": "requires_fresh_review"}
        result = {"id": uid("context"), "workspace_id": selected["id"] if selected else None,
            "_disclosure_policy_hash": disclosure.policy_hash,
            "game_id": "ck3", "target_platform": platform, "state": state, "selection": selection,
            "mission": "Help the player understand their CK3 mods, investigate meaningful clashes, and make only reviewed, supported changes.",
            "playset": playset or ({"id": None, "name": selected["name"]} if selected else None),
            "available_playsets": playsets[:100], "available_playsets_total": len(playsets),
            "installation": (discovery or old).get("installation") or (selected or {}).get("ck3_selection", {}).get("installation"),
            "source_status": {"launcher_checked_at": time.time() if discovery else None,
                "files": "saved_results_not_freshly_verified" if scan else "not_checked",
                "last_file_check_at": (session or {}).get("last_checked_at"),
                "available_enabled_mods": sum(m.get("enabled", False) and m.get("available", False) for m in mods),
                "missing_enabled_mods": missing, "complete": bool(discovery and discovery.get("complete")),
                "discovery_error": discovery_error},
            "preflight": briefing_preflight(discovery or old),
            "saved_scan": scan[0] if scan else None, "saved_index": index, "pending_work": pending,
            "conversation": previous_notes, "proposal": proposal, "capabilities": capabilities(),
            "preferences": {"small_groups": True, "separate_fixes_mod": True, "research": "automatic_public_sources",
                "review_depth": "complete", "missing_mod_search": "automatic_known_roots",
                "candidate_inspection": "ask_first",
                "private_contents_in_web_queries": False, "ask_before_game_test": True},
            "knowledge": knowledge_manifest(), "release": {k: v for k, v in self.service.agent_release.items() if k in {"version", "fingerprint"}},
            "next_step": ("Choose one of the named playsets." if state == "needs_selection" else
                "Identify the CK3 installation and game-data folder." if state == "setup_needed" else
                "Follow the running job before starting more work." if any(j["state"] in {"queued", "running"} for j in pending) else
                "Resume the saved discussion; check current files before making any change." if selection == "resume" and session else
                "Use check_playset to verify current files, then investigate a small group of meaningful issues."),
            "options": (discovery or {}).get("options", options), "manual": manual,
            "configuration_hash": (discovery or {}).get("configuration_hash")}
        result["inspection_scope"] = (selected or {}).get("inspection_scope", {"kind": "launcher_selection"})
        if result["inspection_scope"]["kind"] == "alternative_candidate":
            result["preflight"] = selected.get("inspection_preflight", result["preflight"])
            result["source_status"].update(
                missing_enabled_mods=selected.get("inspection_missing_mods", []), complete=False,
                available_enabled_mods=len(selected.get("default_playset", [])))
        result["preferences"]["version"] = version_preference(self.store, result)
        result["missing_candidate_search"] = candidate_search_summary(
            self.store, result, self.service.inference_policy()["excluded_patterns"])
        # Structured review progress is a small summary; full reports are paged.
        if selected and index:
            from .reviews import ReviewWorkflow
            result["investigation"] = ReviewWorkflow(self.service).context_summary(selected["id"], index["id"])
        # A context record is a lightweight managed receipt, never an approval.
        saved = self.store.put("agent_context", result)
        return public_context(saved, self.service, projection=disclosure)

    def missing_mods(self, context_id, offset=0, limit=20):
        context = self.store.get("agent_context", context_id)
        missing = context["source_status"]["missing_enabled_mods"]
        return {"context_id": context_id, "checked_at": context["source_status"]["launcher_checked_at"],
            "total": len(missing), "mods": missing[offset:offset + limit],
            "next_offset": offset + limit if offset + limit < len(missing) else None,
            "scope": "launcher_metadata_at_briefing_time"}

    def remember(self, context_id, goal=None, notes=None, next_step=None, reviewed_issue_ids=None):
        from .workflow import AgentWorkflow
        with self.service.command_lock, self.store.atomic():
            context = self.store.get("agent_context", context_id)
            if not context.get("workspace_id"):
                raise ValueError("Select and check a playset before saving its discussion.")
            flow = AgentWorkflow(self.service)
            session = flow.session(context["workspace_id"])
            disclosure = SessionProjection(self.service, self.store.get("workspace", context["workspace_id"]))
            values = {k: v for k, v in {"goal": goal, "notes": notes, "next_step": next_step}.items() if v is not None}
            if notes is not None:
                values.update(notes_scan_id=(context.get("saved_scan") or {}).get("id"),
                              notes_index_id=(context.get("saved_index") or {}).get("id"))
            if reviewed_issue_ids is not None:
                if not session.get("index_id"):
                    raise ValueError("No checked issue list is available yet.")
                if (context.get("saved_index") or {}).get("id") != session["index_id"]:
                    raise Conflict("The discussion refers to an older check. Get fresh context before marking current issues reviewed.")
                _, index = flow.current_index(context["workspace_id"], session["index_id"])
                if not set(reviewed_issue_ids) <= {item["id"] for item in index["issues"]}:
                    raise ValueError("A reviewed issue does not belong to this checked playset.")
                values["reviewed_issue_ids"] = list(dict.fromkeys([*session.get("reviewed_issue_ids", []), *reviewed_issue_ids]))[:500]
            updated = disclosure.bind_updates(session, values,
                scan_id=(context.get("saved_scan") or {}).get("id"),
                index_id=(context.get("saved_index") or {}).get("id"))
            disclosure.validate()
            saved = self.store.put("agent_session", {**updated,
                "notes_trust": "assistant_recorded_context_not_approval_or_evidence"}, expected_revision=session["revision"])
            result = disclosure.project(saved)
            disclosure.validate()
            return result

    def request_check(self, context_id, goal=None):
        from .workflow import AgentWorkflow
        with self.service.command_lock:
            context = self.store.get("agent_context", context_id)
            if context["state"] != "ready":
                return {"state": context["state"], "context": public_context(context, self.service)}
            if not context.get("workspace_id"):
                # Another conversation may have started this selection since
                # the briefing. Reuse its workspace under the coordinator lock.
                previous = headers(self.store, "agent_context", ("workspace_id", "configuration_hash", "target_platform"))
                matched = next((p for p in previous if p["workspace_id"] and context.get("configuration_hash") and
                    p["configuration_hash"] == context["configuration_hash"] and p["target_platform"] == context["target_platform"]), None)
                if matched:
                    workspace_id = matched["workspace_id"]
                else:
                    workspace_id = self.service.create_workspace({"game_id": "ck3", "target_platform": context["target_platform"],
                        "mode": "game", "name": context["playset"]["name"] or "CK3 mod conversation"})["id"]
                context = self.store.put("agent_context", {**context, "workspace_id": workspace_id})
            workspace_id = context["workspace_id"]
            for job in self.store.list("job", workspace_id, limit=100):
                if job["state"] in {"queued", "running"}:
                    return {"job_id": job["id"], "workspace_id": workspace_id, "context_id": context_id,
                            "already_running": True, "message": "The current work is still running. Follow its progress first."}
            flow = AgentWorkflow(self.service)
            with self.store.atomic():
                session = flow.session(workspace_id)
                disclosure = SessionProjection(self.service, self.store.get("workspace", workspace_id))
                updated = disclosure.bind_updates(session, {"goal": goal} if goal is not None else {},
                    scan_id=(context.get("saved_scan") or {}).get("id"),
                    index_id=(context.get("saved_index") or {}).get("id"))
                disclosure.validate()
                self.store.put("agent_session", {**updated, "context_id": context_id}, expected_revision=session["revision"])
            result = self.service.command({"command": "agent_check", "workspace_id": workspace_id,
                "payload": {"context_id": context_id}, "idempotency_key": uid("check")})
            return {**result, "context_id": context_id, "workspace_id": workspace_id}

    def check(self, workspace, payload, checkpoint):
        from .workflow import AgentWorkflow
        context = self.store.get("agent_context", payload["context_id"])
        self.service._owns(workspace, context)
        checkpoint("checking the selected playset")
        if context.get("preflight") and context["source_status"].get("missing_enabled_mods"):
            from .preflight import Preflight
            checkpoint("looking for missing mods in known local locations")
            Preflight(self.service).find_missing_mod_candidates(context["id"])
        if not context["manual"]:
            found = self.service._discover_ck3(workspace, context["options"], checkpoint)
            discovery = self.store.get("discovery", found["discovery_id"])
            if discovery.get("configuration_hash") != context["configuration_hash"]:
                raise Conflict("The selected playset changed since the briefing. Get fresh context before continuing.")
        scan, reused = None, False
        if workspace.get("generation"):
            checkpoint("checking whether the saved files are still current")
            try:
                if not context["manual"]:
                    saved = self.store.get("discovery", workspace["discovery_id"])
                    if saved.get("configuration_hash") != context["configuration_hash"]:
                        raise Conflict("The requested selection differs from the saved scan.")
                scan = self.service._current_scan(workspace, workspace["generation"], checkpoint=checkpoint)
                reused = True
            except Conflict:
                scan = None
        if scan is None:
            if not context["manual"]:
                self.service._import_ck3(workspace, {"discovery_id": found["discovery_id"]})
                workspace = self.store.get("workspace", workspace["id"])
            if not workspace.get("sources"):
                raise ValueError("No available game or mod files were found to check.")
            result = self.service._scan(workspace, {}, checkpoint)
            workspace = self.store.get("workspace", workspace["id"])
            scan = self.store.get("scan", result["scan_id"])
        indexes = headers(self.store, "agent_index", ("scan_id", "version", "summary"), workspace_id=workspace["id"])
        index = next((i for i in indexes if i["scan_id"] == scan["id"] and i["version"] == INDEX_VERSION), None)
        flow = AgentWorkflow(self.service)
        previous_session = flow.session(workspace["id"])
        if index is None:
            result = flow.index(workspace, {"scan_id": scan["id"]}, checkpoint, verified_scan=scan)
        else:
            result = {"index_id": index["id"], "summary": index["summary"]}
            from .reviews import ReviewWorkflow
            ReviewWorkflow(self.service).initialize(workspace["id"], index["id"])
        with self.service.command_lock, self.store.atomic():
            session = flow.session(workspace["id"])
            same_scan = previous_session.get("scan_id") == scan["id"]
            self.store.put("agent_session", {**session, "scan_id": scan["id"], "index_id": result["index_id"],
                "state": session["state"] if same_scan else "reviewing", "last_checked_at": time.time(),
                "reviewed_issue_ids": session.get("reviewed_issue_ids", []) if same_scan else [],
                "context_id": context["id"]}, expected_revision=session["revision"])
        self.store.put("agent_context", {**self.store.get("agent_context", context["id"]),
            "saved_scan": {k: scan.get(k) for k in ("id", "created_at", "discovery_id", "schema_hash", "parser_version")},
            "saved_index": {"id": result["index_id"], "scan_id": scan["id"], "version": INDEX_VERSION,
                            "summary": result["summary"]}})
        checkpoint("finished checking available files", 1, 1)
        return {**result, "workspace_id": workspace["id"], "scan_id": scan["id"], "reused_scan": reused,
            "files_verified_at": time.time(), "scope": "available_sources_only", "game_runs": 0,
            "message": "The available files are checked. Investigate the saved changes before recommending a fix."}
