"""Version-bound, reusable metadata views of immutable CK3 captures.

Only derived metadata is cached. Source bytes remain hash checked on each request.
Small SQL headers avoid repeatedly decoding the full scan. Projection construction
is serialized per service so simultaneous callers share work and memory limits.
"""
from __future__ import annotations

from collections import OrderedDict, defaultdict
import sys
import threading
import time

from backend.app.store import Conflict, canonical, digest
from backend.engine.parser import PARSER_VERSION

PROJECTION_VERSION = "ck3-investigation-projection-2"
MAX_ENTRIES = 4
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_HEADER_BYTES = 1024 * 1024
MAX_HEADERS = 64
_CREATION_LOCK = threading.Lock()


def _header(store, kind, identifier, fields):
    """Read small revision columns first; decode large JSON only on a revision miss.

    SQLite json_extract still parses the full source JSON even for tiny fields.
    Its result can be reused while the indexed record revision is unchanged. The
    revision and database identity checks remain live on every request.
    """
    fields = tuple(fields)
    cache_key = (kind, identifier, fields)
    with store.lock:
        store._check_database()
        row = store.connection.execute(
            "SELECT revision,workspace_id FROM records WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        if row is None:
            raise ValueError(f"The saved {kind} is unavailable.")
        if not hasattr(store, "_investigation_header_cache"):
            store._investigation_header_cache = OrderedDict()
            store._investigation_header_bytes = 0
            store._investigation_header_hits = 0
            store._investigation_header_loads = 0
        cache = store._investigation_header_cache
        old = cache.get(cache_key)
        if old is not None:
            header, size = old
            if (header["revision"], header["workspace_id"]) == (row[0], row[1]):
                cache.move_to_end(cache_key)
                store._investigation_header_hits += 1
                return dict(header)
            cache.pop(cache_key)
            store._investigation_header_bytes -= size
        columns = ",".join("json_extract(payload, ?)" for _ in fields)
        row = store.connection.execute(
            "SELECT revision, workspace_id" + ("," + columns if fields else "") +
            " FROM records WHERE kind=? AND id=?", [*("$." + field for field in fields), kind, identifier]).fetchone()
        if row is None:
            raise ValueError(f"The saved {kind} is unavailable.")
        header = {"id": identifier, "revision": row[0], "workspace_id": row[1],
                  **dict(zip(fields, row[2:], strict=True))}
        store._investigation_header_loads += 1
        size = _memory_size((cache_key, header))
        while cache and (len(cache) >= MAX_HEADERS or store._investigation_header_bytes + size > MAX_HEADER_BYTES):
            _, (_, removed) = cache.popitem(last=False)
            store._investigation_header_bytes -= removed
        if size <= MAX_HEADER_BYTES:
            cache[cache_key] = (header, size)
            store._investigation_header_bytes += size
        return dict(header)


def _identity(service, workspace_id, index_id):
    from .issues import INDEX_VERSION
    workspace = service.store.get("workspace", workspace_id)
    service._ck3_workspace(workspace)
    index = _header(service.store, "agent_index", index_id,
                    ("scan_id", "version", "manifest_hash", "profile_hash", "schema_hash"))
    if index["workspace_id"] != workspace_id or workspace.get("generation") != index["scan_id"]:
        raise Conflict("A newer scan or different selection is available. Please use its results.")
    if index["version"] != INDEX_VERSION:
        raise Conflict("The inspection tools changed. Please examine the saved scan again.")
    scan = _header(service.store, "scan", index["scan_id"],
                   ("game_id", "target_platform", "parser_version", "schema_hash", "manifest_hash", "profile_hash",
                    "discovery_id", "configuration_hash"))
    if (scan["workspace_id"] != workspace_id or scan["game_id"] != "ck3" or
            scan["target_platform"] != workspace["target_platform"] or scan["parser_version"] != PARSER_VERSION or
            scan["schema_hash"] != service._schema_hash("ck3") or
            any(index[key] != scan[key] for key in ("manifest_hash", "profile_hash", "schema_hash"))):
        raise Conflict("The saved inspection no longer matches the current tools. Please check the playset again.")
    discovery = None
    if scan["discovery_id"]:
        discovery = _header(service.store, "discovery", scan["discovery_id"],
                            ("game_id", "target_platform", "configuration_hash"))
        if (discovery["workspace_id"] != workspace_id or discovery["game_id"] != "ck3" or
                discovery["target_platform"] != scan["target_platform"] or
                discovery["configuration_hash"] != scan["configuration_hash"]):
            raise Conflict("The captured launcher descriptors belong to a different saved selection.")
    return {"version": PROJECTION_VERSION, "workspace_id": workspace_id, "index": index,
            "scan": scan, "discovery": discovery}


def _memory_size(value):
    # Count shared objects once, including Python dictionary/list overhead rather
    # than treating the serialized JSON size as the in-memory size.
    seen, queue, total = set(), [value], 0
    while queue:
        item = queue.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            queue.extend(item.keys())
            queue.extend(item.values())
        elif isinstance(item, (list, tuple)):
            queue.extend(item)
    return total


def _hydrate(payload):
    files = payload["files"]
    by_id = {item["source_id"]: item for item in files}
    definitions = defaultdict(list)
    for key, entries in payload["definitions"].items():
        definitions[key] = [(by_id[identifier], record) for identifier, record in entries]
    return {"scan": payload["scan"], "index": payload["index"], "files": files, "by_id": by_id,
            "by_file_id": {item["id"]: item for item in files if "id" in item},
            "by_path": {(item["owner"], item["path"]): item for item in files},
            "launcher_descriptors": [item for item in files if item["origin"] == "launcher_descriptor"],
            "definition_sources": definitions}


class ProjectionCache:
    def __init__(self):
        self.lock = threading.RLock()
        self.entries = OrderedDict()
        self.bytes = 0
        self.stats = {"builds": 0, "disk_loads": 0, "hits": 0, "misses": 0, "build_seconds": 0.0}

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.bytes = 0

    def get(self, service, identity, builder):
        key = digest(canonical(identity))
        record_id = "investigation_projection_" + key
        with self.lock:
            # The managed record revision is part of the cache key. Tampering or
            # replacing persisted projection data must not bypass validation.
            with service.store.lock:
                service.store._check_database()
                row = service.store.connection.execute(
                    "SELECT revision FROM records WHERE kind='investigation_projection' AND id=?", (record_id,)).fetchone()
            revision = row[0] if row else None
            if key in self.entries:
                cached, old_revision, size = self.entries[key]
                if old_revision == revision:
                    self.entries.move_to_end(key)
                    self.stats["hits"] += 1
                    return cached
                self.entries.pop(key)
                self.bytes -= size
            self.stats["misses"] += 1
            if revision is not None:
                record = service.store.get("investigation_projection", record_id)
                payload = record.get("projection")
                if (record.get("version") != PROJECTION_VERSION or record.get("identity") != identity or
                        record.get("workspace_id") != identity["workspace_id"] or
                        digest(canonical(payload)) != record.get("sha256")):
                    raise Conflict("The saved investigation projection changed. Rebuild the saved investigation.")
                self.stats["disk_loads"] += 1
            else:
                start = time.perf_counter()
                payload = builder()
                # Recheck after the large read, before persisting/reusing derived identities.
                if _identity(service, identity["workspace_id"], identity["index"]["id"]) != identity:
                    raise Conflict("The saved inspection changed while preparing its evidence. Please retry.")
                record = service.store.put("investigation_projection", {
                    "id": record_id, "workspace_id": identity["workspace_id"], "version": PROJECTION_VERSION,
                    "identity": identity, "projection": payload, "sha256": digest(canonical(payload))})
                revision = record["revision"]
                self.stats["builds"] += 1
                self.stats["build_seconds"] += time.perf_counter() - start
            result = _hydrate(payload)
            size = _memory_size(result)
            # Forget stale versions for the same index immediately.
            for old_key, (entry, _, old_size) in list(self.entries.items()):
                if entry["index"]["id"] == identity["index"]["id"]:
                    self.entries.pop(old_key)
                    self.bytes -= old_size
            while self.entries and (len(self.entries) >= MAX_ENTRIES or self.bytes + size > max(0, MAX_CACHE_BYTES - MAX_HEADER_BYTES)):
                _, (_, _, removed) = self.entries.popitem(last=False)
                self.bytes -= removed
            if size <= max(0, MAX_CACHE_BYTES - MAX_HEADER_BYTES):
                self.entries[key] = (result, revision, size)
                self.bytes += size
            return result


def cache_for(service):
    with _CREATION_LOCK:
        if not hasattr(service, "_investigation_projection_cache"):
            service._investigation_projection_cache = ProjectionCache()
        return service._investigation_projection_cache


def get_projection(service, workspace_id, index_id, builder):
    identity = _identity(service, workspace_id, index_id)
    return cache_for(service).get(service, identity, builder)


def prepare_projection(service, workspace_id, index_id):
    from .investigation import _Snapshot
    _Snapshot(service, workspace_id, index_id)


def clear_projection_cache(service):
    cache = getattr(service, "_investigation_projection_cache", None)
    if cache is not None:
        cache.clear()
    with service.store.lock:
        if hasattr(service.store, "_investigation_header_cache"):
            service.store._investigation_header_cache.clear()
            service.store._investigation_header_bytes = 0


def projection_metrics(service):
    cache = cache_for(service)
    with cache.lock:
        return {**cache.stats, "cache_entries": len(cache.entries), "cache_bytes": cache.bytes,
                "max_cache_bytes": MAX_CACHE_BYTES, "max_entries": MAX_ENTRIES,
                "header_cache_bytes": getattr(service.store, "_investigation_header_bytes", 0),
                "header_cache_hits": getattr(service.store, "_investigation_header_hits", 0),
                "header_json_loads": getattr(service.store, "_investigation_header_loads", 0)}
