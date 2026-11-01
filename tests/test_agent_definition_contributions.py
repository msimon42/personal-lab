"""Every captured declaration is reachable through bounded metadata pages."""
import json
import copy

import pytest

from backend.agent import investigation as module
from backend.agent.investigation import Investigation
from backend.agent import projection
from backend.agent.projection import clear_projection_cache
from backend.agent.workflow import AgentWorkflow
from backend.app.service import Service
from backend.app.store import Conflict, canonical, digest


TARGET = "game/trait/shared_trait"


@pytest.fixture
def captured(tmp_path):
    originals = {}
    owners = [f"mod-{number:02}" for number in range(12)]
    for number, owner in enumerate(owners):
        raw = ("# caf\u00e9: source coordinates remain bytes\r\n"
               f"shared_trait={{diplomacy={number}}}\r\n"
               + ("shared_trait={diplomacy=99}\r\n" if number == 11 else "")
               + "root_trait={parent=shared_trait}\r\n").encode()
        originals[owner, "common/traits/shared.txt"] = raw
    originals[owners[-1], "common/traits/alternate.txt"] = b"shared_trait={diplomacy=100}\n"
    for (owner, relative), raw in originals.items():
        path = tmp_path / "originals" / owner / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "target_platform": "macos", "mode": "game",
            "name": "All contributors", "sources": [{"owner": owner, "path": str(tmp_path / "originals" / owner)}
                                                        for owner in owners]})
        scan = service._scan(workspace, {}, lambda *args: None)
        workspace = service.store.get("workspace", workspace["id"])
        index = AgentWorkflow(service).index(workspace, {"scan_id": scan["scan_id"]}, lambda *args: None)
        yield service, Investigation(service), workspace["id"], index["index_id"], originals
        assert all((tmp_path / "originals" / owner / relative).read_bytes() == raw
                   for (owner, relative), raw in originals.items())
    finally:
        service.close()


def pages(api, arguments):
    rows, responses = [], []
    while arguments is not None:
        response = api.definition_contributions(**arguments)
        responses.append(response)
        rows.extend(response["occurrences"])
        arguments = response["occurrences_continuation"]
    return rows, responses


def test_symbol_preview_reaches_all_contributors_and_same_file_repeats(captured):
    _, api, workspace, index, originals = captured
    preview = api.search_sources(workspace, index, "shared_trait", kind="symbol", limit=1)
    assert preview["next_offset"] is None  # No additional names, but contributors continue.
    definition = preview["results"][0]
    assert definition["occurrence_count"] == 14 and len(definition["occurrences"]) == 8
    assert definition["occurrences_offset"] == 0 and definition["occurrences_next_offset"] == 8
    assert definition["occurrences_truncated"]
    rest, responses = pages(api, definition["occurrences_continuation"])
    rows = definition["occurrences"] + rest
    assert len(rows) == 14 and {row["owner"] for row in rows} == {owner for owner, _ in originals}
    assert responses[-1]["next_offset"] is None and responses[-1]["continuation"] is None
    repeated = [row for row in rows if row["owner"] == "mod-11" and row["path"].endswith("shared.txt")]
    assert len(repeated) == 2 and repeated[0]["span"] != repeated[1]["span"]
    assert repeated[0]["ordinal"] != repeated[1]["ordinal"]
    for row in rows:
        start, end = row["span"]
        assert originals[row["owner"], row["path"]][start:end].startswith(b"shared_trait=")
    assert all(response["coverage"]["source_bytes"] == "not_read_metadata_only" for response in responses)
    assert all("no game-selected winner" in response["meaning"] for response in responses)


def test_owner_and_path_filters_apply_to_actual_declarations_and_continuations(captured):
    _, api, workspace, index, _ = captured
    preview = api.search_sources(workspace, index, "shared_trait", kind="symbol", owner="mod-11")
    result = preview["results"][0]
    assert result["occurrence_count"] == 3
    assert {row["owner"] for row in result["occurrences"]} == {"mod-11"}
    assert result["coverage"]["indexed_occurrence_count"] == 14
    rows, responses = pages(api, {"workspace_id": workspace, "index_id": index, "target": TARGET,
                                  "limit": 1, "owner": "mod-11", "path_prefix": "common/traits/shared"})
    assert len(rows) == 2 and {row["path"] for row in rows} == {"common/traits/shared.txt"}
    assert all(row["owner"] == "mod-11" for row in rows)
    assert [response["offset"] for response in responses] == [0, 1]
    assert all(response["occurrence_count"] == 2 for response in responses)
    alternate = api.search_sources(workspace, index, "shared_trait", kind="symbol",
                                   path_prefix="common/traits/alternate")
    assert alternate["results"][0]["occurrence_count"] == 1
    assert alternate["results"][0]["occurrences"][0]["path"] == "common/traits/alternate.txt"


def test_related_preview_continuations_reach_every_related_declaration(captured):
    _, api, workspace, index, _ = captured
    related = api.related_definitions(workspace, index, TARGET)
    main = related["definition"]
    rows, _ = pages(api, main["occurrences_continuation"])
    assert len(main["occurrences"]) + len(rows) == 14
    root = next(item for item in related["related_definitions"] if item["target"] == "game/trait/root_trait")
    assert len(root["occurrences"]) == 4 and root["occurrence_count"] == 12
    rows, _ = pages(api, root["occurrences_continuation"])
    assert len(root["occurrences"]) + len(rows) == 12


def test_missing_and_filtered_empty_targets_are_explicit_and_not_game_absence(captured):
    _, api, workspace, index, _ = captured
    missing = api.definition_contributions(workspace, index, "game/trait/not_indexed")
    assert missing["status"] == "not_in_available_index"
    assert missing["occurrence_count"] == 0 and missing["occurrences"] == []
    assert missing["occurrences_continuation"] is None
    assert missing["coverage"]["unsupported_or_deferred_definitions"] == "may_be_absent"
    empty = api.definition_contributions(workspace, index, TARGET, owner="not-a-contributor")
    assert empty["status"] == "no_matching_contributions" and empty["occurrence_count"] == 0
    assert empty["coverage"]["indexed_occurrence_count"] == 14
    with pytest.raises(ValueError, match="outside"):
        api.definition_contributions(workspace, index, TARGET, offset=15)


@pytest.mark.parametrize("changed", ["target", "owner", "path_prefix", "offset", "limit"])
def test_continuation_rejects_changed_target_filters_or_page(captured, changed):
    _, api, workspace, index, _ = captured
    first = api.definition_contributions(workspace, index, TARGET, limit=1)
    arguments = dict(first["occurrences_continuation"])
    arguments[changed] = {"target": "game/trait/root_trait", "owner": "mod-11",
                          "path_prefix": "common/traits", "offset": 2, "limit": 2}[changed]
    with pytest.raises(Conflict, match="different inputs"):
        api.definition_contributions(**arguments)


@pytest.mark.parametrize("changed", ["index", "scan", "workspace", "policy"])
def test_continuation_rejects_revised_capture_or_source_policy(captured, changed):
    service, api, workspace, index, _ = captured
    first = api.definition_contributions(workspace, index, TARGET, limit=1)
    arguments = first["occurrences_continuation"]
    if changed == "policy":
        service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": ["*"]}})
    elif changed == "index":
        record = service.store.get("agent_index", index)
        service.store.put("agent_index", {**record, "extra_revision": True})
    elif changed == "scan":
        record = service.store.get("scan", first["scan_id"])
        service.store.put("scan", {**record, "extra_revision": True})
    else:
        record = service.store.get("workspace", workspace)
        service.store.put("workspace", {**record, "generation": "another-generation"})
    with pytest.raises(Conflict):
        api.definition_contributions(**arguments)


def test_one_excluded_contributor_redacts_whole_target_even_with_owner_filter(captured):
    service, api, workspace, index, _ = captured
    service.store.put("configuration", {"id": "inference-policy", "policy": {
        "excluded_patterns": ["common/traits/alternate.txt"]}})
    response = api.definition_contributions(workspace, index, TARGET, owner="mod-00")
    assert response["excluded"] is True
    assert not {"target", "occurrences", "occurrence_count", "coverage"} & response.keys()
    assert "mod-11" not in json.dumps(response) and "alternate" not in json.dumps(response)
    assert api.search_sources(workspace, index, "shared_trait", kind="symbol")["results"] == []


@pytest.mark.parametrize("change", ["policy", "generation"])
def test_definition_page_rechecks_scope_and_policy_before_return(captured, monkeypatch, change):
    service, api, workspace, index, _ = captured
    original = module._Snapshot.definition_page
    def change_before_return(view, *args, **kwargs):
        response = original(view, *args, **kwargs)
        if change == "policy":
            service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": ["*"]}})
        else:
            record = service.store.get("workspace", workspace)
            service.store.put("workspace", {**record, "generation": "another-generation"})
        return response
    monkeypatch.setattr(module._Snapshot, "definition_page", change_before_return)
    with pytest.raises(Conflict):
        api.definition_contributions(workspace, index, TARGET)


@pytest.mark.parametrize("arguments", [
    {"limit": 51}, {"limit": True}, {"offset": -1}, {"offset": True},
    {"target": ""}, {"target": "x" * 4097}, {"owner": True}, {"path_prefix": "x" * 501},
    {"continuation": "not-a-returned-cursor"}, {"continuation": "x" * 513},
])
def test_invalid_definition_requests_are_rejected(captured, arguments):
    _, api, workspace, index, _ = captured
    with pytest.raises(ValueError):
        api.definition_contributions(workspace, index, **{"target": TARGET, **arguments})


def test_response_budget_pages_large_physical_declaration_metadata(tmp_path):
    # Long virtual family/path metadata and Unicode owner IDs are real input
    # dimensions. Owner IDs do not have to be filesystem directory names.
    relative = "common/" + "/".join(chr(97 + number) * 145 for number in range(5)) + "/shared.txt"
    originals = {}
    sources = []
    for number in range(50):
        owner = "\U0001f984" * 190 + f"{number:02}"
        root = tmp_path / "sources" / str(number)
        path = root / relative
        path.parent.mkdir(parents=True)
        raw = f"shared_definition={{value={number}}}\n".encode()
        path.write_bytes(raw)
        originals[path] = raw
        sources.append({"owner": owner, "path": str(root)})
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "target_platform": "macos", "mode": "game",
                                              "name": "Bounded large metadata", "sources": sources})
        scan = service._scan(workspace, {}, lambda *args: None)
        workspace = service.store.get("workspace", workspace["id"])
        index = AgentWorkflow(service).index(workspace, {"scan_id": scan["scan_id"]}, lambda *args: None)
        target = service.store.get("scan", scan["scan_id"])["analysis"]["source_definitions"][0]["key"]
        api = Investigation(service)
        rows, responses = pages(api, {"workspace_id": workspace["id"], "index_id": index["index_id"],
                                      "target": target, "limit": 50})
        assert len(rows) == 50 and len(responses) > 1
        assert len(responses[0]["occurrences"]) < 50
        assert all(len(json.dumps(response, ensure_ascii=True, indent=2).encode()) <= 128 * 1024
                   for response in responses)
        assert all(response["next_offset"] is None or response["next_offset"] > response["offset"]
                   for response in responses)
        assert {row["owner"] for row in rows} == {source["owner"] for source in sources}
        assert all(path.read_bytes() == raw for path, raw in originals.items())
    finally:
        service.close()


def test_single_oversized_record_refuses_without_nonadvancing_cursor(captured, monkeypatch):
    _, api, workspace, index, _ = captured
    monkeypatch.setattr(module, "MAX_DEFINITION_RESPONSE_BYTES", 100)
    with pytest.raises(ValueError, match="exceeds the response budget"):
        api.definition_contributions(workspace, index, TARGET)


def test_continuation_survives_cache_rebuild_without_losing_capture_order(captured):
    service, api, workspace, index, _ = captured
    first = api.definition_contributions(workspace, index, TARGET, limit=1)
    clear_projection_cache(service)
    rest, _ = pages(api, first["occurrences_continuation"])
    complete = api.definition_contributions(workspace, index, TARGET, limit=50)
    assert first["occurrences"] + rest == complete["occurrences"]


@pytest.fixture(params=[False, True], ids=["parsed_localization", "deferred_localization"])
def localized(tmp_path, monkeypatch, request):
    if request.param:
        from backend.app import service as service_module
        monkeypatch.setattr(service_module, "TEXT_CAPTURE_BUDGET", 0)
    path = tmp_path / "originals/localization/english/example_l_english.yml"
    path.parent.mkdir(parents=True)
    raw = '\ufeffl_english:\r\n # caf\u00e9\r\n repeated:0 "First"\r\n other:0 "Other"\r\n repeated:1 "Second"\r\n'.encode()
    path.write_bytes(raw)
    service = Service(tmp_path / "managed")
    try:
        workspace = service.create_workspace({"game_id": "ck3", "target_platform": "macos", "mode": "game",
            "name": "Localization occurrences", "sources": [{"owner": "one", "path": str(tmp_path / "originals")}]})
        scan = service._scan(workspace, {}, lambda *args: None)
        workspace = service.store.get("workspace", workspace["id"])
        index = AgentWorkflow(service).index(workspace, {"scan_id": scan["scan_id"]}, lambda *args: None)
        yield service, Investigation(service), workspace["id"], index["index_id"], path, raw
        assert path.read_bytes() == raw
    finally:
        service.close()


def test_localization_repeats_retain_exact_saved_coordinates_even_when_text_was_deferred(localized):
    _, api, workspace, index, _, raw = localized
    target = "game/localization/english:repeated"
    rows, responses = pages(api, {"workspace_id": workspace, "index_id": index, "target": target, "limit": 1})
    assert len(rows) == 2 and rows[0]["source_id"] == rows[1]["source_id"]
    assert [row["ordinal"] for row in rows] == [0, 2]
    assert all(row["ordinal_scope"] == "file_localization_entry_order" for row in rows)
    assert all(row["coordinate_state"] == "exact" for row in rows)
    assert raw[slice(*rows[0]["span"])] == b' repeated:0 "First"'
    assert raw[slice(*rows[1]["span"])] == b' repeated:1 "Second"'
    assert responses[0]["coverage"]["unknown_coordinate_count"] == 0
    # The same occurrence appears in analysis and the issue index; it must not
    # be counted twice when exact coordinate sources are combined.
    assert responses[0]["occurrence_count"] == 2


@pytest.mark.parametrize("localized", [False], indirect=True, ids=["parsed_provenance"])
def test_provenance_only_coordinate_gaps_remain_explicit_without_invented_spans(localized):
    service, api, workspace, index, _, _ = localized
    scan_id = service.store.get("workspace", workspace)["generation"]
    scan = service.store.get("scan", scan_id)
    target = "game/localization/english:repeated"
    assert any(item["key"] == target for item in scan["analysis"]["definitions"])
    for file in scan["analysis"]["files"]:
        file["entries"] = []
    service.store.put("scan", scan)
    saved = service.store.get("agent_index", index)
    service.store.put("agent_index", {**saved, "issues": []})
    clear_projection_cache(service)
    result = api.definition_contributions(workspace, index, target)
    assert result["occurrence_count"] == result["coverage"]["unknown_coordinate_count"] == 2
    assert all(row["identity_state"] == "source_provenance_only" and row["coordinate_state"] == "unknown"
               for row in result["occurrences"])
    assert all("span" not in row and "ordinal" not in row for row in result["occurrences"])


def test_new_projection_rebuilds_from_saved_scan_and_retains_legacy_artifact(localized, monkeypatch):
    service, api, workspace, index, _, _ = localized
    current = next(record for record in service.store.list("investigation_projection", limit=1000)
                   if record["identity"]["index"]["id"] == index)
    legacy_identity = {**current["identity"], "version": "ck3-investigation-projection-1"}
    legacy_payload = copy.deepcopy(current["projection"])
    for entries in legacy_payload["definitions"].values():
        for _, record in entries:
            for key in ("span", "ordinal", "coordinate_state", "ordinal_scope", "key_ordinal"):
                record.pop(key, None)
    legacy = service.store.put("investigation_projection", {"id": "investigation_projection_" + digest(canonical(legacy_identity)),
        "workspace_id": workspace, "identity": legacy_identity, "version": "ck3-investigation-projection-1",
        "projection": legacy_payload, "sha256": digest(canonical(legacy_payload))})
    with service.store.atomic():
        service.store.connection.execute("DELETE FROM records WHERE id=?", (current["id"],))
    clear_projection_cache(service)
    monkeypatch.setattr(service, "_scan", lambda *args, **kwargs: pytest.fail("Projection upgrades must not rescan originals"))
    monkeypatch.setattr(service.store.fs, "read_bytes", lambda *args, **kwargs:
                        pytest.fail("Coordinate projection upgrade must not read source bodies"))
    result = api.definition_contributions(workspace, index, "game/localization/english:repeated")
    assert result["occurrence_count"] == 2 and result["coverage"]["unknown_coordinate_count"] == 0
    assert all(row["coordinate_state"] == "exact" for row in result["occurrences"])
    assert service.store.get("investigation_projection", legacy["id"]) == legacy
    rebuilt = service.store.get("investigation_projection", current["id"])
    assert rebuilt["version"] == projection.PROJECTION_VERSION == "ck3-investigation-projection-2"
