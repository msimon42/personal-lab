"""CK3 source inspection. No loader winner, runtime conformance, or repair authority.

The supplied schemas describe an advisory vocabulary. Original JSON bytes remain
unchanged; the manifest records inspected corrections separately. Script and
descriptor projections are derived from the ordered, byte-preserving CST.
"""
from __future__ import annotations

import base64
import binascii
import copy
import json
import re
from collections import defaultdict
from graphlib import CycleError, TopologicalSorter
from importlib import resources
from pathlib import Path, PurePosixPath

from .formats import BINARY_EXTENSIONS, parse_localization, path_risks, windows_key
from .parser import PARSER_VERSION, digest, parse

ADAPTER_VERSION = "ck3-inspection-2"
# Wheels carry a byte-exact copy; editable development uses the original source.
SCHEMA_ROOT = resources.files("backend.engine").joinpath("data", "ck3")
if not SCHEMA_ROOT.is_dir():
    SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas" / "ck3"
BASE_OWNERS = {"V", "base", "vanilla"}
DESCRIPTOR_SCALARS = {"name", "version", "supported_version", "path", "archive", "picture", "remote_file_id"}
DESCRIPTOR_LISTS = {"tags", "dependencies"}
NUMBER = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")


def _json_hash(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def _base(owner):
    return owner in BASE_OWNERS or owner.startswith("DLC:")


def _scalar(value):
    if value and value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def _source_path(path):
    # These are immutable source identities, never paths authorized for output.
    # POSIX filenames can contain Windows-reserved spelling; retain and diagnose it.
    if not isinstance(path, str) or not path or len(path) > 32760 or path.startswith("/"):
        raise ValueError("invalid source-relative path")
    if any(p in {"", ".", ".."} for p in path.split("/")) or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError("source path traversal or control character")
    return path


def schema_manifest() -> dict:
    """Import the custom advisory format; bind every original and overlay hash."""
    manifest_raw = (SCHEMA_ROOT / "_manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    schemas, originals, changed = {}, [], []
    expected = {entry["path"]: entry["sha256"] for entry in manifest["originals"]}
    for path in sorted(SCHEMA_ROOT.iterdir(), key=lambda entry: entry.name):
        if not path.is_file() or not path.name.endswith(".json") or path.name.startswith("_"):
            continue
        raw = path.read_bytes()
        original = json.loads(raw)
        required_keys = {"entity", "game", "filePath", "requiredFields", "optionalFields", "localizationKeys"}
        if set(original) != required_keys or original["game"] != "ck3":
            raise ValueError(f"Unsupported CK3 advisory schema: {path.name}")
        if not isinstance(original["entity"], str) or original["entity"] in schemas:
            raise ValueError("CK3 advisory entity identity must be unique text")
        for field_group in ("requiredFields", "optionalFields"):
            if not isinstance(original[field_group], dict):
                raise ValueError("CK3 advisory fields must be objects")
            for field, spec in original[field_group].items():
                if not isinstance(field, str) or not isinstance(spec, dict) or spec.get("type") not in {
                    "block", "number", "string", "boolean", "reference", "list"
                }:
                    raise ValueError("Unsupported CK3 advisory field declaration")
        if not isinstance(original["localizationKeys"], list) or any(
            not isinstance(key, str) for key in original["localizationKeys"]
        ):
            raise ValueError("CK3 advisory localization patterns must be text")
        if not isinstance(original["filePath"], str) or not original["filePath"].endswith("/*.txt"):
            raise ValueError("CK3 advisory file selector must name a script directory")
        checksum = digest(raw)
        originals.append({"path": path.name, "sha256": checksum})
        if expected.get(path.name) != checksum:
            changed.append(path.name)
        schema = copy.deepcopy(original)
        overlay = manifest.get("overlays", {}).get(schema["entity"], {})
        if "filePath" in overlay:
            schema["filePath"] = overlay["filePath"]
        for field in overlay.get("optionalize", []):
            if field in schema["requiredFields"]:
                schema["optionalFields"][field] = schema["requiredFields"].pop(field)
        for field in overlay.get("removeFields", []):
            schema["requiredFields"].pop(field, None)
            schema["optionalFields"].pop(field, None)
        schema["optionalFields"].update(copy.deepcopy(overlay.get("fields", {})))
        for field in overlay.get("relaxEnums", []):
            for field_group in ("requiredFields", "optionalFields"):
                if field in schema[field_group]:
                    schema[field_group][field].pop("validValues", None)
        if "localizationKeys" in overlay:
            schema["localizationKeys"] = list(overlay["localizationKeys"])
        schema["advisory"] = True
        schema["notes"] = list(overlay.get("notes", []))
        schema["original_sha256"] = checksum
        schemas[schema["entity"]] = schema
    changed.extend(sorted(expected.keys() - {entry["path"] for entry in originals}))
    result = {"version": manifest["schema_format"], "revision": manifest["revision"],
              "source": manifest["source"], "policy": manifest["policy"], "originals": originals,
              "manifest_sha256": digest(manifest_raw), "originals_changed": sorted(changed),
              "inspection_evidence": manifest["inspection_evidence"], "schemas": schemas,
              "conformed_rules": 0}
    result["hash"] = _json_hash(result)
    return result


def classify_ck3(path: str, schemas: dict | None = None) -> dict:
    """Classify CK3's root-level layout without EU5 resource stages or modes."""
    return _classify(path, schemas if schemas is not None else schema_manifest()["schemas"])


def _classify(path, schemas):
    path = _source_path(path)
    parts, suffix = path.split("/"), PurePosixPath(path).suffix.lower()
    kind, family, entity = "opaque", "unknown", None
    if suffix == ".mod":
        kind, family = "descriptor", "descriptor"
    elif parts[0] == "localization" and suffix == ".yml":
        kind, family = "localization", "localization"
    elif parts[0] == "gui" and suffix == ".gui":
        kind, family = "gui", "gui"
    elif parts[0] == "events" and suffix == ".txt":
        kind, family, entity = "script", "event", "event"
    elif parts[0] == "common" and suffix == ".txt":
        kind, family = "script", "/".join(parts[:-1])
        # Directory boundaries matter: scheme agent types and scheme types differ.
        for name, schema in schemas.items():
            parent = schema["filePath"].rsplit("/", 1)[0]
            if path.startswith(parent + "/"):
                family, entity = name, name
                break
    elif parts[0] in {"history", "reader_export", "map_data", "gfx", "sound", "music"} and suffix in {".txt", ".asset", ".gfx"}:
        kind, family = "script", "/".join(parts[:-1])
    elif suffix in BINARY_EXTENSIONS:
        family = "asset"
    elif suffix == ".info":
        family = "documentation"
    return {"kind": kind, "family": family, "stage": "game", "entity": entity}


def _node_coordinates(node):
    """A bounded display identity, never a replacement script representation."""
    key = node.key
    value = node.value
    return {"kind": node.kind, "key": key if key is None or len(key) <= 4096 else None,
            "key_preview": key[:256] if key is not None and len(key) > 4096 else None,
            "key_omitted": key is not None and len(key) > 4096,
            "operator": node.operator, "ordinal": node.ordinal,
            "span": list(node.span), "value_span": [node.value_start, node.value_end],
            "recovered": node.recovered, "child_count": len(node.children),
            "value_preview": value[:256] if value is not None else None,
            "value_preview_truncated": value is not None and len(value) > 256}


def _cst_reference(file, node=None):
    return {"file_id": file["id"], "sha256": file["sha256"],
            "parser_version": file["document"].version,
            "span": list(node.span) if node is not None else [0, file["size"]],
            "ordinal": node.ordinal if node is not None else None,
            "recovered": node.recovered if node is not None else not file["document"].eligible}


def parse_descriptor(raw: bytes, *, full_entries: bool = True) -> dict:
    """Return the lossless document plus an ordered, multivalue derived view."""
    document = parse(raw)
    diagnostics = list(document.diagnostics)
    entries, data = [], defaultdict(list)
    for node in document.children:
        entry = node.as_dict() if full_entries else _node_coordinates(node)
        entries.append(entry)
        if not node.key or node.recovered or node.operator != "=":
            continue
        value = [_scalar(child.value) for child in node.children if child.kind == "bare"] if node.is_block else _scalar(node.value)
        data[node.key].append(value)
        bad_shape = (node.key in DESCRIPTOR_SCALARS | {"replace_path"} and node.is_block) or (
            node.key in DESCRIPTOR_LISTS and (not node.is_block or any(child.kind != "bare" for child in node.children))
        )
        if bad_shape:
            diagnostics.append({"code": "descriptor_field_shape", "field": node.key,
                                "span": list(node.span), "severity": "warning"})
    for field in DESCRIPTOR_SCALARS:
        if len(data.get(field, [])) > 1:
            diagnostics.append({"code": "descriptor_repeated_scalar", "field": field,
                                "span": [0, len(raw)], "severity": "warning"})
    return {"document": document, "entries": entries, "data": dict(data), "diagnostics": diagnostics}


def _issue(kind, target, message, occurrences=(), **extra):
    result = {"category": "potential_incompatibility", "kind": kind, "target": target,
              "message": message, "description": message, "impact": message,
              "title": kind.replace("_", " ").capitalize(), "certainty": "static_inspection",
              "coverage": "ck3_advisory", "source_occurrences": list(occurrences),
              "evidence_ids": [], "lost_obligation_ids": [], "runtime_requirements": [],
              "repairability": "blocked_unknown", **extra}
    result["id"] = "finding-" + _json_hash(result)[:20]
    result["cluster_id"] = "cluster-" + digest(target.encode())[:12]
    return result


def _decode(inputs):
    if not isinstance(inputs, list) or len(inputs) > 200_000:
        raise ValueError("inputs must be a bounded array")
    files, findings, seen, total = [], [], set(), 0
    for index, item in enumerate(inputs):
        try:
            if not isinstance(item, dict) or not isinstance(item.get("owner"), str) or not item["owner"]:
                raise ValueError("input owner missing")
            path = _source_path(item["path"])
            identity = (item["owner"], path)
            if identity in seen:
                raise ValueError("duplicate owner/path input")
            opaque = item.get("opaque_blob")
            if opaque is not None:
                if (not isinstance(opaque, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(opaque.get("sha256", "")))
                        or type(opaque.get("size")) is not int or not 0 <= opaque["size"] <= 2 ** 42
                        or item.get("sha256") != opaque["sha256"]):
                    raise ValueError("invalid immutable opaque blob reference")
                raw, checksum, size = None, opaque["sha256"], opaque["size"]
            else:
                raw = base64.b64decode(item["bytes_base64"], validate=True)
                if base64.b64encode(raw).decode() != item["bytes_base64"]:
                    raise ValueError("noncanonical base64")
                total += len(raw)
                if len(raw) > 8_000_000 or total > 128_000_000:
                    raise ValueError("snapshot byte budget exceeded")
                checksum, size = digest(raw), len(raw)
                if item.get("sha256") is not None and item["sha256"] != checksum:
                    raise ValueError("declared input hash mismatch")
            seen.add(identity)
            files.append({"id": _json_hash([item["owner"], path, checksum])[:24], "owner": item["owner"],
                          "path": path, "sha256": checksum, "size": size, "_raw": raw,
                          "opaque_blob": opaque})
        except (ValueError, TypeError, KeyError, binascii.Error) as exc:
            findings.append(_issue("invalid_input", str(index), str(exc), repairability="invalid_input"))
    return files, findings


def _occurrence(file, node=None):
    return {"file_id": file["id"], "owner": file["owner"], "path": file["path"],
            "span": list(node.span) if node else [0, file["size"]]}


def _definition_nodes(file):
    document = file.get("document")
    if not document or file["kind"] not in {"script", "gui"}:
        return []
    if file["kind"] == "gui":
        # Only explicitly named declarations; widgets are ordered children.
        return [node for top in document.children for node in top.walk()
                if node.kind in {"declaration", "gui_head"} and node.is_block
                and len(node.head) > 1 and node.head[0] in {"type", "template"}]
    return [node for node in document.children if node.key and node.is_block and node.operator == "="
            and not node.key.startswith(("@", "$"))]


def _schema_diagnostics(node, schema):
    fields = {**schema["requiredFields"], **schema["optionalFields"]}
    children = [child for child in node.children if child.key and not child.recovered]
    diagnostics = []
    for name in schema["requiredFields"]:
        if not any(child.key == name for child in children):
            diagnostics.append({"code": "schema_missing_field", "field": name, "span": list(node.span)})
    for child in children:
        spec = fields.get(child.key)
        if not spec:
            continue
        if child.value and any(c in child.value for c in "@$["):
            continue  # Expressions remain symbolic; do not coerce or evaluate them.
        kinds = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        valid = any((kind in {"block", "list"} and child.is_block) or (
            kind in {"string", "reference"} and not child.is_block) or (
            kind == "number" and child.value is not None and bool(NUMBER.fullmatch(_scalar(child.value)))) or (
            kind == "boolean" and child.value in {"yes", "no", "true", "false"}) for kind in kinds)
        if not valid:
            diagnostics.append({"code": "schema_field_shape", "field": child.key, "span": list(child.span)})
        elif spec.get("validValues") and _scalar(child.value) not in spec["validValues"]:
            diagnostics.append({"code": "schema_enum_advisory", "field": child.key, "span": list(child.span)})
    return diagnostics


def _packages(files, metadata, playset):
    descriptors, findings = defaultdict(list), []
    if metadata is not None and (not isinstance(metadata, list) or len(metadata) > 100_000):
        raise ValueError("package_metadata must be a bounded array")
    metadata_bytes = 0
    for index, item in enumerate(metadata or []):
        try:
            if not isinstance(item, dict) or not isinstance(item.get("owner"), str) or not item["owner"]:
                raise ValueError("package metadata owner missing")
            raw = base64.b64decode(item["bytes_base64"], validate=True)
            metadata_bytes += len(raw)
            if len(raw) > 1_000_000 or metadata_bytes > 32_000_000:
                raise ValueError("package metadata byte budget exceeded")
            if base64.b64encode(raw).decode() != item["bytes_base64"] or digest(raw) != item["sha256"]:
                raise ValueError("package descriptor hash or base64 mismatch")
            if not isinstance(item.get("descriptor_path"), str):
                raise ValueError("package descriptor path missing")
            parsed = parse_descriptor(raw, full_entries=False)
            descriptors[item["owner"]].append({"owner": item["owner"], "path": item["descriptor_path"],
                "id": "package-" + _json_hash([item["owner"], item["descriptor_path"], digest(raw)])[:24],
                "sha256": digest(raw), "size": len(raw), "data": parsed["data"],
                "diagnostics": parsed["diagnostics"], "source": "registry_descriptor"})
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            findings.append(_issue("invalid_input", f"package_metadata/{index}", str(exc), repairability="invalid_input"))
    for file in files:
        if file["kind"] == "descriptor" and file["path"] == "descriptor.mod" and file["owner"] not in descriptors:
            descriptors[file["owner"]].append({**{k: file[k] for k in ("owner", "path", "id", "sha256", "size", "data", "diagnostics")},
                                               "source": "payload_descriptor"})
    packages = [item for group in descriptors.values() for item in group]
    names = defaultdict(set)
    for package in packages:
        for name in package["data"].get("name", []):
            if isinstance(name, str) and len(name) <= 4096:
                names[name].add(package["owner"])
        for diagnostic in package["diagnostics"]:
            findings.append(_issue("descriptor_diagnostic", package["owner"], diagnostic["code"],
                                   [_occurrence(package)], diagnostic=diagnostic))
    edges = []
    for package in packages:
        owner = package["owner"]
        if len(descriptors[owner]) > 1:
            findings.append(_issue("ambiguous_package_descriptor", owner, "Multiple captured registry descriptors name this owner; metadata remains ambiguous."))
        if owner not in playset:
            continue
        for values in package["data"].get("dependencies", []):
            if not isinstance(values, list):
                continue
            for name in values:
                if not isinstance(name, str) or len(name) > 4096:
                    findings.append(_issue("descriptor_symbol_unindexed", owner, "Dependency name exceeds the bounded symbol index; original descriptor bytes are retained."))
                    continue
                candidates = sorted(names.get(name, set()))
                enabled = [candidate for candidate in candidates if candidate in playset or _base(candidate)]
                edges.append({"source": owner, "target": name, "kind": "declared_dependency",
                              "certainty": "descriptor_declaration", "owners": candidates,
                              "enabled_owners": enabled, "order_semantics": "unknown"})
                if len(candidates) != 1 or len(enabled) != 1:
                    kind = "missing_dependency" if not candidates else "disabled_dependency" if not enabled else "ambiguous_dependency"
                    findings.append(_issue(kind, owner, f"Declared dependency {name!r} has {len(candidates)} captured package candidates and {len(enabled)} enabled candidates."))
        for mask in package["data"].get("replace_path", []):
            if not isinstance(mask, str):
                continue
            try:
                prefix = _source_path(mask.rstrip("/"))
            except ValueError:
                findings.append(_issue("replace_path_unresolved", owner, "Declared replace_path is not a supported relative directory."))
                continue
            affected = [file["id"] for file in files if file["active"] and file["owner"] != owner
                        and (file["path"] == prefix or file["path"].startswith(prefix + "/"))]
            findings.append(_issue("replace_path_scope", owner,
                f"Declared replace_path {mask!r} overlaps {len(affected)} captured files from other enabled sources; selection and preservation require investigation.",
                affected_file_ids=affected))
    adjacency = defaultdict(set)
    for edge in edges:
        adjacency[edge["source"]].update(edge["enabled_owners"])
    try:
        TopologicalSorter(adjacency).prepare()
    except CycleError as exc:
        findings.append(_issue("dependency_cycle", "packages", "Declared dependencies form a cycle; this is not a proven strict-order contradiction.",
                               cycle=exc.args[1]))
    return packages, findings, edges


def analyze_ck3(inputs: list[dict], playset: list[str], target_platform: str = "macos",
                package_metadata: list[dict] | None = None) -> dict:
    if (not isinstance(playset, list) or any(not isinstance(owner, str) or not owner for owner in playset)
            or len(playset) != len(set(playset))):
        raise ValueError("playset must be an ordered list of unique owner IDs")
    if target_platform not in {"macos", "windows"}:
        raise ValueError("CK3 target platform must be macos or windows")
    manifest = schema_manifest()
    files, findings = _decode(inputs)
    path_groups, aliases, records, by_key = defaultdict(list), defaultdict(list), [], defaultdict(list)
    locale_groups = defaultdict(list)
    indexed_families, partial_files = set(), set()
    for file in files:
        info = _classify(file["path"], manifest["schemas"])
        file.update(info)
        file.update(active=file["owner"] in playset or _base(file["owner"]), shadowed_by=None,
                    selection_state="unknown", diagnostics=[], entries=[], data=None)
        raw = file["_raw"]
        if raw is None:
            file.update(kind="opaque", opaque=True)
        elif file["kind"] == "descriptor":
            file.update(parse_descriptor(raw, full_entries=False))
        elif file["kind"] == "localization":
            file.update(parse_localization(raw, file["path"]))
            if not file["document"].bom:
                file["diagnostics"].append({"code": "ck3_localization_bom_advisory", "span": [0, 0], "severity": "warning"})
        elif file["kind"] in {"script", "gui"}:
            file["document"] = parse(raw, dialect="gui" if file["kind"] == "gui" else "script")
            file["diagnostics"] = list(file["document"].diagnostics)
        else:
            file["opaque"] = True
        if file["active"]:
            path_groups[file["path"]].append(file)
            aliases[windows_key(file["path"])].append(file)
            if file["entity"] and file.get("document") and file["document"].eligible:
                indexed_families.add(file["family"])
        for diagnostic in file["diagnostics"]:
            findings.append(_issue("parse_diagnostic", file["path"], diagnostic["code"],
                                   [_occurrence(file)], diagnostic=diagnostic))
        risks = path_risks(file["path"])
        from .safety import validate_relative_path
        try:
            if validate_relative_path(file["path"]) != file["path"]:
                risks.append("windows_separator_alias")
        except ValueError:
            risks.append("windows_output_path_unsupported")
        for risk in risks:
            findings.append(_issue("portability", file["path"], risk, [_occurrence(file)]))
        for node in _definition_nodes(file):
            name = _scalar(node.key)
            if len(name) > 4096:
                partial_files.add(file["id"])
                findings.append(_issue("symbol_coverage_unknown", file["path"],
                    "An oversized identifier remains outside the bounded symbol index; original bytes are retained.", [_occurrence(file, node)]))
                continue
            if file["kind"] == "gui":
                name = node.head[0] + ":" + name
            elif not file["entity"]:
                # E.g. history dates can be local to the file/character. Do not
                # infer a global entity identity from an arbitrary top block.
                name = "source:" + file["path"] + "#" + str(node.ordinal) + ":" + name
            key = f"game/{file['family']}/{name}"
            record = {"key": key, "owner": file["owner"], "file_id": file["id"], "path": file["path"],
                      "sha256": file["sha256"], "span": list(node.span), "ordinal": node.ordinal,
                      "mode": "declaration", "value": None, "state": "unknown", "active": file["active"],
                      "identity_state": "declared" if file["entity"] or file["kind"] == "gui" else "provisional",
                      "child_count": len(node.children), "cst_reference": _cst_reference(file, node),
                      "_node": node, "_file": file}
            records.append(record)
            if file["active"]:
                by_key[key].append(record)
            if file["entity"] and file.get("document") and file["document"].eligible and not node.recovered:
                for diagnostic in _schema_diagnostics(node, manifest["schemas"][file["entity"]]):
                    findings.append(_issue(diagnostic["code"], key,
                        f"Field {diagnostic['field']!r} differs from the advisory schema; game validity is unconfirmed.",
                        [_occurrence(file, node)], diagnostic=diagnostic))
        if file["kind"] == "localization" and file["active"]:
            for entry in file["entries"]:
                locale_groups[(entry["language"], entry["key"])].append((file, entry))
    for path, group in path_groups.items():
        if len(group) > 1 and not all(file["kind"] == "descriptor" for file in group):
            findings.append(_issue("overlapping_path", path,
                "Enabled sources share this relative path. All originals remain indexed; no CK3 file winner is assumed.",
                [_occurrence(file) for file in group], owners=[file["owner"] for file in group]))
    for group in aliases.values():
        if len({file["path"] for file in group}) > 1:
            findings.append(_issue("windows_path_alias", group[0]["path"],
                "Distinct source spellings share a Windows comparison key. Filesystem and game selection remain unmeasured.",
                [_occurrence(file) for file in group]))
    definitions = []
    for key, group in by_key.items():
        family = group[0]["_file"]["family"]
        provenance = [record["file_id"] for record in group]
        definitions.append({"key": key, "stage": "game", "family": family, "state": "unknown", "value": None,
                            "identity_state": group[0]["identity_state"],
                            "field_states": {}, "operations": [], "transitions": [], "provenance": provenance,
                            "evidence_ids": [], "confidence": "inspection_only", "occurrences": [
                                {k: record[k] for k in ("owner", "file_id", "span", "ordinal")} for record in group]})
        if len(group) > 1 and group[0]["identity_state"] == "declared":
            findings.append(_issue("duplicate_definition", key,
                "Repeated definition identity requires CK3 family-specific loading evidence; no declaration is discarded.",
                [_occurrence(record["_file"], record["_node"]) for record in group]))
    for (language, name), group in locale_groups.items():
        if len(name) > 4096:
            partial_files.update(file["id"] for file, _ in group)
            findings.append(_issue("symbol_coverage_unknown", "localization",
                                   "An oversized localization key remains outside the bounded symbol index; original bytes are retained."))
            continue
        key = f"game/localization/{language}:{name}"
        definitions.append({"key": key, "stage": "game", "family": "localization", "state": "unknown", "value": None,
                            "field_states": {}, "operations": [], "transitions": [],
                            "provenance": [file["id"] for file, _ in group], "evidence_ids": [], "confidence": "inspection_only"})
        if len(group) > 1:
            findings.append(_issue("localization_overlap", key,
                "Repeated localization keys retain every source occurrence; ordinary/replace tiers and runtime selection remain unqualified.",
                [{**_occurrence(file), "span": entry["span"]} for file, entry in group]))
    definition_edges = []
    for record in records:
        if not record["active"]:
            continue
        schema = manifest["schemas"].get(record["_file"]["entity"], {})
        fields = {**schema.get("requiredFields", {}), **schema.get("optionalFields", {})}
        for node in record["_node"].children:
            ref_type = fields.get(node.key, {}).get("refType")
            if not ref_type or node.value is None:
                continue
            expression = _scalar(node.value)
            oversized = len(expression) > 4096
            recovered = not record["_file"]["document"].eligible or record["_node"].recovered or node.recovered
            dynamic = any(c in expression for c in "@$[]")
            target = None if dynamic or oversized or recovered else f"game/{ref_type}/{expression}"
            status = "source_unparsed" if recovered else "oversized_expression" if oversized else "dynamic" if dynamic else "candidate_present" if target in by_key else (
                "missing_in_snapshot" if ref_type in indexed_families else "family_unindexed")
            definition_edges.append({"source": record["key"], "target": target,
                                     "expression": expression if not oversized else "<oversized expression retained in source>",
                                     "certainty": "recovered" if recovered else "dynamic" if dynamic or oversized else "literal", "resolved": None,
                                     "resolution_state": status, "span": list(node.span), "evidence_ids": []})
            if status != "candidate_present":
                findings.append(_issue("literal_reference_advisory" if status == "missing_in_snapshot" else "reference_coverage_unknown",
                    record["key"], f"Reference {expression[:4096]!r}: {status.replace('_', ' ')}; runtime validity is unconfirmed.",
                    [_occurrence(record["_file"], node)]))
    packages, package_findings, package_edges = _packages(files, package_metadata, playset)
    findings.extend(package_findings)
    captured_owners = {file["owner"] for file in files} | {package["owner"] for package in packages}
    for owner in playset:
        if owner not in captured_owners:
            findings.append(_issue("playset_owner_unavailable", owner,
                "Enabled owner has no captured source files or package descriptor; inspection coverage is incomplete."))
    if manifest["originals_changed"]:
        findings.append(_issue("schema_source_changed", "schemas/ck3", "Original advisory schema hashes differ from the recorded baseline; review and requalify overlays."))
    obligations = [{"id": "obl-" + _json_hash([file["id"], "source_contribution"])[:20],
                    "target": "resource/" + file["path"], "definition_key": "resource/" + file["path"],
                    "owner": file["owner"], "field": "contribution", "expected": file["sha256"],
                    "expected_value": file["sha256"], "actual": None, "state": "unknown", "required": True,
                    "kind": "source_contribution", "source_occurrence": _occurrence(file),
                    "evidence_ids": [], "observability": "runtime_required",
                    "description": f"Preserve all contributions from {file['owner']}: {file['path']}"}
                   for file in files if file["active"] and not _base(file["owner"])]
    profile = {"id": "ck3-advisory-v1", "adapter_version": ADAPTER_VERSION,
               "kind": "documentation_only", "adapter_kind": "ck3_inspection",
               "game_id": "ck3", "target_platform": target_platform,
               "families": [], "readers": {}, "schema_hash": manifest["hash"], "conformed_rules": [],
               "evidence_label": "V", "production_export_enabled": False}
    profile["hash"] = _json_hash(profile)
    public_files = []
    for file in files:
        doc = file.get("document")
        public_files.append({**{k: file[k] for k in ("id", "owner", "path", "sha256", "size", "kind", "family", "stage", "active", "shadowed_by", "selection_state", "diagnostics", "opaque_blob")},
            "encoding": doc.encoding if doc else None, "bom": doc.bom.hex() if doc else None,
            "newline": doc.newline if doc else None,
            "snippet": (file["_raw"] or b"")[:512].decode("utf-8", "backslashreplace"),
            "entries": file.get("entries", [])[:200], "metadata": file.get("data"),
            "occurrences": [_node_coordinates(node) for node in doc.children[:200]] if doc else [],
            "occurrence_count": len(doc.children) if doc else 0,
            "occurrences_truncated": bool(doc and len(doc.children) > 200),
            "entry_count": len(file.get("entries", [])), "entries_truncated": len(file.get("entries", [])) > 200,
            "cst_reference": _cst_reference(file) if doc else None})
    identity = {"files": sorted([{k: file[k] for k in ("owner", "path", "sha256")} for file in files], key=lambda row: (row["owner"], row["path"])),
                "playset": playset, "package_metadata": [{k: package[k] for k in ("owner", "path", "sha256")} for package in packages],
                "profile_hash": profile["hash"]}
    return {"input_manifest_hash": _json_hash(identity), "playset": list(playset), "profile": profile, "baselines": {},
            "files": public_files, "definitions": definitions,
            "source_definitions": [{k: value for k, value in record.items() if not k.startswith("_")} for record in records],
            "package_metadata": packages, "schema_manifest": {k: value for k, value in manifest.items() if k != "schemas"},
            "findings": findings, "obligations": obligations,
            "graphs": {"package": package_edges, "definition": definition_edges, "operation": [], "behavior": []},
            "coverage": {"mode": "ck3_static_inspection", "supported_families": [],
                "advisory_schema_families": sorted(manifest["schemas"]),
                "opaque_or_unsupported_files": [file["id"] for file in files if file["kind"] == "opaque" or not file["entity"] or file["id"] in partial_files],
                "partially_indexed_files": sorted(partial_files),
                "derived_projection": {"version": "ck3-coordinates-v1", "recursive_trees_stored": False,
                    "source_definitions": "Complete definition occurrence identities and byte coordinates; child bodies are reconstructed from source bytes.",
                    "file_occurrences": "At most 200 shallow top-level coordinate previews; complete counts and source references are retained."},
                "cst_reconstruction": {"authority": "immutable_source_bytes", "parser_version": PARSER_VERSION,
                    "method": "Reparse the hash-checked complete source blob, then select the recorded whole-document byte span and occurrence ordinal.",
                    "changes_semantic_coverage": False},
                "conformed_rules": 0, "game_runs": 0, "production_export_enabled": False,
                "remaining": ["Build-scoped CK3 loading conformance", "Complete preservation and gameplay observations",
                              "macOS and Windows runtime qualification", "Nested schemas and dynamic reference coverage"]},
            "summary": {"files": len(files), "definitions": len(definitions), "findings": len(findings),
                "obligations": len(obligations), "unknown_obligations": len(obligations), "violated_obligations": 0,
                "state": "invalid_input" if any(issue["kind"] == "invalid_input" for issue in findings) else "needs_attention" if findings else "analyzed",
                "llm_calls": 0, "game_runs": 0}}
