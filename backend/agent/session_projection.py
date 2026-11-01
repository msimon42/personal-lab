"""Policy-bound views of saved conversation text; raw managed history is retained."""
from __future__ import annotations

import copy

from backend.app.store import Conflict, canonical, digest
from .projection import _header

TEXT_FIELDS = ("goal", "notes", "next_step")
BINDINGS_KEY = "text_bindings"
BINDING_FORMAT = "ck3-conversation-text-1"
SCAN_FIELDS = ("game_id", "target_platform", "manifest_hash", "profile_hash", "schema_hash", "parser_version")
INDEX_FIELDS = (*SCAN_FIELDS, "scan_id", "version")


class SessionProjection:
    """Read only small identity headers and recheck them before disclosing text."""

    def __init__(self, service, workspace=None):
        self.service, self.store = service, service.store
        self.patterns = list(service.inference_policy()["excluded_patterns"])
        self.policy_hash = digest(canonical(self.patterns))
        self.workspace = None
        self.headers = {}
        if workspace is not None:
            self.watch_workspace(workspace)

    def watch_workspace(self, workspace):
        self.service._ck3_workspace(workspace)
        if self.workspace is not None and self.workspace != workspace:
            raise Conflict("The saved discussion scope changed. Get fresh context before continuing.")
        self.workspace = dict(workspace)

    def _source_header(self, kind, identifier):
        key = kind, identifier
        if key not in self.headers:
            fields = SCAN_FIELDS if kind == "scan" else INDEX_FIELDS
            try:
                self.headers[key] = _header(self.store, kind, identifier, fields)
            except ValueError:
                self.headers[key] = None
        return self.headers[key]

    def _sources(self, scan_id, index_id):
        scan = self._source_header("scan", scan_id) if scan_id else None
        index = self._source_header("agent_index", index_id) if index_id else None
        if (scan_id and scan is None) or (index_id and (scan is None or index is None)):
            raise Conflict("The saved discussion's source scope is unavailable.")
        for header in (scan, index):
            if header is not None and any(header.get(key) != self.workspace.get(key) for key in (
                    "game_id", "target_platform")):
                raise Conflict("The saved discussion's source scope belongs to another target.")
            if header is not None and header["workspace_id"] != self.workspace["id"]:
                raise Conflict("The saved discussion's source scope belongs to another workspace.")
        if index is not None and index["scan_id"] != scan_id:
            raise Conflict("The saved discussion's source index and scan differ.")
        return scan, index

    def bind_updates(self, session, updates, *, scan_id=None, index_id=None):
        """Only explicitly replaced fields receive the current policy and scope."""
        if self.workspace is None:
            self.watch_workspace(self.store.get("workspace", session["workspace_id"]))
        if session.get("workspace_id") != self.workspace["id"]:
            raise Conflict("The saved discussion belongs to another workspace.")
        if not any(field in updates for field in TEXT_FIELDS):
            return {**session, **updates}
        scan, index = self._sources(scan_id, index_id)
        bindings = copy.deepcopy(session.get(BINDINGS_KEY) or {})
        if not isinstance(bindings, dict):
            raise Conflict("The saved discussion has unsupported text provenance.")
        for field in TEXT_FIELDS:
            if field in updates:
                bindings[field] = {"format": BINDING_FORMAT, "policy_hash": self.policy_hash,
                    "value_hash": digest(canonical(updates[field])),
                    "workspace_id": self.workspace["id"], "game_id": self.workspace["game_id"],
                    "target_platform": self.workspace["target_platform"], "scan": scan, "index": index}
        return {**session, **updates, BINDINGS_KEY: bindings}

    def project(self, session):
        if self.workspace is None:
            self.watch_workspace(self.store.get("workspace", session["workspace_id"]))
        if session.get("workspace_id") != self.workspace["id"]:
            raise Conflict("The saved discussion belongs to another workspace.")
        result = copy.deepcopy(session)
        bindings = result.pop(BINDINGS_KEY, {}) or {}
        bindings_valid = isinstance(bindings, dict)
        if not bindings_valid:
            bindings = {}
        states = {}
        session_scope_valid = all(session.get(key) == self.workspace.get(key) for key in (
            "game_id", "target_platform"))
        for field in TEXT_FIELDS:
            if not session.get(field):
                continue
            binding = bindings.get(field)
            reason = None
            legacy = binding is None
            historical = False
            if not bindings_valid:
                reason = "unsupported_text_provenance"
            elif not session_scope_valid:
                reason = "discussion_scope_changed"
            elif legacy:
                if self.patterns:
                    reason = "legacy_text_has_no_disclosure_policy"
                else:
                    historical = session.get("notes_scan_id") != self.workspace.get("generation")
            elif not isinstance(binding, dict) or binding.get("format") != BINDING_FORMAT:
                reason = "unsupported_text_provenance"
            elif binding.get("policy_hash") != self.policy_hash:
                reason = "source_policy_changed"
            elif binding.get("value_hash") != digest(canonical(session[field])):
                reason = "saved_text_changed"
            elif any(binding.get(key) != self.workspace.get("id" if key == "workspace_id" else key)
                     for key in ("workspace_id", "game_id", "target_platform")):
                reason = "discussion_scope_changed"
            else:
                scan, index = binding.get("scan"), binding.get("index")
                try:
                    if ((scan is not None and not isinstance(scan, dict)) or
                            (index is not None and not isinstance(index, dict))):
                        raise ValueError("Unsupported saved source identity")
                    current_scan, current_index = self._sources(
                        (scan or {}).get("id"), (index or {}).get("id"))
                    if scan != current_scan or index != current_index:
                        reason = "source_scope_changed"
                    historical = bool(scan and scan["id"] != self.workspace.get("generation"))
                except (ValueError, KeyError, TypeError):
                    reason = "source_scope_unavailable"
            if reason:
                result[field] = None
                states[field] = {"state": "redacted", "reason": reason}
            else:
                states[field] = {"state": "historical" if historical else "available",
                                 "provenance": "legacy_unbound" if legacy else BINDING_FORMAT}
        result["text_disclosure"] = {"fields": states,
            "redacted_fields": [field for field, state in states.items() if state["state"] == "redacted"],
            "meaning": "Saved text is context only; redaction retains the original managed record."}
        return result

    def validate(self):
        if self.patterns != self.service.inference_policy()["excluded_patterns"]:
            raise Conflict("Source permissions changed while reading saved discussion. No text was returned.")
        if self.workspace is not None:
            current = self.store.get("workspace", self.workspace["id"])
            if current != self.workspace:
                raise Conflict("The saved discussion scope changed while reading it. Please retry.")
        for (kind, identifier), expected in self.headers.items():
            fields = SCAN_FIELDS if kind == "scan" else INDEX_FIELDS
            try:
                current = _header(self.store, kind, identifier, fields)
            except ValueError:
                current = None
            if current != expected:
                raise Conflict("The saved discussion's source scope changed while reading it. Please retry.")


def public_session(service, session):
    view = SessionProjection(service)
    result = view.project(session)
    view.validate()
    return result
