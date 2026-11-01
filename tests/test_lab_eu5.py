"""Headless workflows use physical fixtures and the existing domain gates."""
import asyncio
import base64
import json
from pathlib import Path
import time
import zipfile

import pytest
from pydantic import ValidationError

from backend.app.eu5_operations import (
    EU5Operations, DefinitionInput, DefinitionsInput, FindingInput, FindingsInput,
    InstallationInput, IntentInput, MutationInput, PageInput, PatchInput, PrepareInput,
    ScanInput, SourceInput, WorkspaceCreateInput, WorkspaceReadInput,
    accept_trusted_decision, build_operations,
)
from backend.app.service import Service
from backend.app.store import Conflict, digest
from backend.engine.parser import parse


@pytest.fixture
def lab(tmp_path):
    service = Service(tmp_path / "state", initialize_default=False)
    try:
        yield service, EU5Operations(service)
    finally:
        service.close()


def complete(service, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        record = service.store.get("job", run_id)
        if record["state"] in {"completed", "failed", "cancelled"}:
            assert record["state"] == "completed", record
            return record["result"]
        time.sleep(0.01)
    raise AssertionError("Run did not terminate")


def scan(lab, fixture="F03", key="scan"):
    service, operations = lab
    accepted = operations.scan_start(ScanInput(installation_id="fixture:" + fixture,
        playset_id="fixture:" + fixture + ":configured", idempotency_key=key))
    return complete(service, accepted.run_id)["scan_id"]


def stage(lab, snapshot):
    return lab[1].workspace_create(WorkspaceCreateInput(snapshot_id=snapshot, idempotency_key="stage"))


def validate(lab, workspace, key="validate"):
    accepted = lab[1].validate_start(MutationInput(workspace_id=workspace.workspace_id,
        expected_revision=workspace.workspace_revision, idempotency_key=key))
    return complete(lab[0], accepted.run_id)


def source_hashes(service, snapshot):
    record = service.store.get("scan", snapshot)
    return {str(Path(source["path"]) / entry["path"]): digest((Path(source["path"]) / entry["path"]).read_bytes())
        for source in record["source_roots"] for entry in record["inputs"] if entry["owner"] == source["owner"]}


def test_real_fixture_scan_inspect_staging_validation_plan_and_exact_zip(lab):
    service, operations = lab
    snapshot = scan(lab)
    before = source_hashes(service, snapshot)
    assert before
    findings = operations.findings_list(FindingsInput(snapshot_id=snapshot))
    assert findings.total and any(row.kind == "independent_intents" for row in findings.items)
    detail = operations.finding_get(FindingInput(finding_id=findings.items[0].id))
    assert json.loads(detail.json_chunk)["kind"] == findings.items[0].kind
    definitions = operations.definitions_search(DefinitionsInput(snapshot_id=snapshot))
    comparison = operations.definition_compare(DefinitionInput(snapshot_id=snapshot, definition_id=definitions.items[0].id))
    assert len(json.loads(comparison.json_chunk)["contributions"]) == 3
    source = service.store.get("scan", snapshot)["analysis"]["files"][0]
    excerpt = operations.source_read(SourceInput(snapshot_id=snapshot, source_id=source["id"]))
    assert digest(base64.b64decode(excerpt.bytes_base64)) == source["sha256"]
    workspace = stage(lab, snapshot)
    assert workspace.workspace_revision == 0 and workspace.state == "staged"
    diff = operations.workspace_diff(WorkspaceReadInput(workspace_id=workspace.workspace_id, expected_revision=0))
    assert "x = 2 y = 3" in diff.items[0].text
    validation = validate(lab, workspace)
    assert validation["validation"]["experimental_eligible"]
    assert not validation["validation"]["validated_promotion_eligible"]
    plan = operations.deployment_prepare(PrepareInput(workspace_id=workspace.workspace_id, expected_revision=0, idempotency_key="plan"))
    assert plan.experimental_export_eligible and not plan.external_apply_available
    exported = operations.workspace_export(MutationInput(workspace_id=workspace.workspace_id, expected_revision=0, idempotency_key="export"))
    artifact = service.store.get("artifact", exported.artifact_id)
    with zipfile.ZipFile(artifact["path"]) as bundle:
        data = [bundle.read(name) for name in bundle.namelist() if name.endswith(".txt")]
        assert b"REPLACE:item = { x = 2 y = 3 }\n" in data
    assert source_hashes(service, snapshot) == before
    assert not (service.root / "managed" / "active-release.json").exists()


def test_conflict_proposal_never_waives_until_trusted_choice(lab):
    service, operations = lab
    snapshot = scan(lab, "F04")
    finding = next(row for row in operations.findings_list(FindingsInput(snapshot_id=snapshot)).items if row.kind == "contradictory_intents")
    intent = operations.intent_record(IntentInput(snapshot_id=snapshot, finding_ids=[finding.id],
        selected_outcome="choose_owner", choice="B", reason="Keep B's exact target", idempotency_key="intent"))
    assert intent.state == "awaiting_operator"
    assert service.store.list("decision") == []
    blocked = stage(lab, snapshot)
    assert blocked.state == "blocked"
    with pytest.raises(ValueError, match="operator"):
        operations.workspace_create(WorkspaceCreateInput(snapshot_id=snapshot, intent_ids=[intent.intent_id], idempotency_key="premature"))
    receipt = accept_trusted_decision(service, intent.question_id, "B")
    decision = service.store.get("decision", receipt["decision_id"])
    assert decision["actor"] == "user" and len(decision["waive"]) == 1
    assert accept_trusted_decision(service, intent.question_id, "B") == receipt
    selected = operations.workspace_create(WorkspaceCreateInput(snapshot_id=snapshot, intent_ids=[intent.intent_id], idempotency_key="selected"))
    assert selected.state == "staged"
    assert validate(lab, selected)["validation"]["experimental_eligible"]


@pytest.mark.parametrize("fixture", ["F13", "F18"])
def test_malformed_and_unknown_obligations_block_export(lab, fixture):
    service, operations = lab
    snapshot = scan(lab, fixture)
    before = source_hashes(service, snapshot)
    workspace = stage(lab, snapshot)
    checked = validate(lab, workspace)
    assert not checked["validation"]["experimental_eligible"]
    with pytest.raises(ValueError, match="Validate"):
        operations.workspace_export(MutationInput(workspace_id=workspace.workspace_id, expected_revision=0, idempotency_key="no-export"))
    assert not service.store.list("artifact")
    assert source_hashes(service, snapshot) == before


def test_update_after_validation_rejects_export_without_artifacts(lab):
    service, operations = lab
    snapshot = scan(lab)
    workspace = stage(lab, snapshot)
    validate(lab, workspace)
    record = service.store.get("scan", snapshot)
    source = next(row for row in record["source_roots"] if row["owner"] == "A")
    path = Path(source["path"]) / "in_game/common/testdb/10.txt"
    path.write_bytes(b"manual source update")
    with pytest.raises(Conflict, match="source changed"):
        operations.workspace_export(MutationInput(workspace_id=workspace.workspace_id, expected_revision=0, idempotency_key="stale"))
    assert service.store.list("artifact") == []


def test_retry_dedup_and_changed_key_conflict(lab):
    service, operations = lab
    args = ScanInput(installation_id="fixture:F03", playset_id="fixture:F03:configured", idempotency_key="same")
    one = operations.scan_start(args)
    two = operations.scan_start(args)
    assert one == two
    complete(service, one.run_id)
    with pytest.raises(Conflict, match="Idempotency"):
        operations.scan_start(args.model_copy(update={"installation_id": "fixture:F04", "playset_id": "fixture:F04:configured"}))
    assert len(service.store.list("scan")) == 1


def test_exact_scalar_patch_revisions_and_preservation_gate(lab):
    service, operations = lab
    snapshot = scan(lab)
    workspace = stage(lab, snapshot)
    candidate = service.store.get("candidate", workspace.candidate_id)
    file = candidate["plan"]["files"][0]
    doc = parse(base64.b64decode(file["bytes_base64"]))
    node = doc.children[0].children[0]
    patch = PatchInput(workspace_id=workspace.workspace_id, expected_revision=0, idempotency_key="patch",
        operations=[{"kind": "replace_scalar", "path": file["path"], "document_hash": doc.sha256,
            "start": node.value_start, "end": node.value_end, "ordinal": node.ordinal,
            "old_hash": digest(doc.raw[node.value_start:node.value_end]), "replacement_base64": base64.b64encode(b"8").decode()}])
    updated = operations.workspace_patch(patch)
    assert updated.workspace_revision == 1 and updated.candidate_id != candidate["id"]
    assert operations.workspace_patch(patch) == updated
    with pytest.raises(Conflict, match="revision"):
        operations.workspace_patch(patch.model_copy(update={"idempotency_key": "stale-revision"}))
    assert not validate(lab, updated)["validation"]["experimental_eligible"]


def test_paging_bound_to_snapshot_filters_and_policy(lab):
    service, operations = lab
    page = operations.installations_list(PageInput(limit=1))
    assert page.total == 20 and page.next_cursor
    seen = [page.items[0].id]
    while page.next_cursor:
        page = operations.installations_list(PageInput(limit=1, cursor=page.next_cursor))
        seen.extend(row.id for row in page.items)
    assert len(set(seen)) == 20
    snapshot = scan(lab)
    findings = operations.findings_list(FindingsInput(snapshot_id=snapshot, limit=1))
    if findings.next_cursor:
        with pytest.raises(Conflict):
            operations.findings_list(FindingsInput(snapshot_id=snapshot, kind="other", cursor=findings.next_cursor))
    service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": ["*"]}})
    with pytest.raises(ValueError, match="withheld"):
        operations.findings_list(FindingsInput(snapshot_id=snapshot))


def test_snapshot_revision_change_invalidates_an_existing_page(lab):
    service, operations = lab
    snapshot = scan(lab, "F20")
    page = operations.findings_list(FindingsInput(snapshot_id=snapshot, limit=1))
    assert page.next_cursor is not None
    record = service.store.get("scan", snapshot)
    service.store.put("scan", record, expected_revision=record["revision"])
    with pytest.raises(Conflict, match="Continuation"):
        operations.findings_list(FindingsInput(snapshot_id=snapshot, limit=1, cursor=page.next_cursor))


def test_source_policy_change_invalidates_cached_pending_intent_and_operator_choice(lab):
    service, operations = lab
    snapshot = scan(lab, "F04")
    finding = next(row for row in operations.findings_list(FindingsInput(snapshot_id=snapshot)).items if row.kind == "contradictory_intents")
    args = IntentInput(snapshot_id=snapshot, finding_ids=[finding.id], selected_outcome="choose_owner", choice="B", idempotency_key="private-choice")
    intent = operations.intent_record(args)
    service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": ["*"]}})
    with pytest.raises(Conflict, match="Idempotency"):
        operations.intent_record(args)
    with pytest.raises(ValueError, match="withheld"):
        accept_trusted_decision(service, intent.question_id, "B")
    assert service.store.list("decision") == []


@pytest.mark.parametrize("change", ["policy", "snapshot_revision"])
def test_read_rechecks_scope_after_constructing_the_complete_page(lab, monkeypatch, change):
    service, operations = lab
    snapshot = scan(lab)
    original = operations._page
    def change_after_projection(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == "policy":
            service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": ["*"]}})
        else:
            record = service.store.get("scan", snapshot)
            service.store.put("scan", record, expected_revision=record["revision"])
        return result
    monkeypatch.setattr(operations, "_page", change_after_projection)
    with pytest.raises(Conflict, match="disclosure or snapshot"):
        operations.findings_list(FindingsInput(snapshot_id=snapshot))


def test_export_rechecks_policy_before_disclosing_or_caching_artifact(lab, monkeypatch):
    service, operations = lab
    snapshot = scan(lab)
    workspace = stage(lab, snapshot)
    validate(lab, workspace)
    original = service._export
    def revoke_after_export(*args, **kwargs):
        result = original(*args, **kwargs)
        service.store.put("configuration", {"id": "inference-policy", "policy": {"excluded_patterns": ["*"]}})
        return result
    monkeypatch.setattr(service, "_export", revoke_after_export)
    with pytest.raises(Conflict, match="no output is authorized"):
        operations.workspace_export(MutationInput(workspace_id=workspace.workspace_id, expected_revision=0, idempotency_key="racing-export"))
    assert not service.store.list("lab_artifact")
    assert not (service.root / "managed" / "active-release.json").exists()


def test_paths_extra_approval_fields_and_foreign_ids_refused(lab):
    service, operations = lab
    with pytest.raises(ValidationError):
        ScanInput(installation_id="fixture:F03", playset_id="fixture:F03:configured", idempotency_key="x", roots=["/tmp"])
    with pytest.raises(ValidationError):
        IntentInput(snapshot_id="s", idempotency_key="x", human_approved=True)
    with pytest.raises(ValueError, match="registered"):
        operations.mods_list(InstallationInput(installation_id="../../secret"))
    snapshot = scan(lab)
    with pytest.raises(ValueError, match="Source ID"):
        operations.source_read(SourceInput(snapshot_id=snapshot, source_id="/etc/passwd"))
    with pytest.raises(ValidationError):
        service.command({"command": "scan", "workspace_id": "missing", "payload": {"arbitrary": True}, "idempotency_key": "invalid"})


def test_native_intent_bridge_uses_same_pending_question(lab):
    service, _ = lab
    snapshot = scan(lab, "F04")
    specs = {spec.name: spec for spec in build_operations(service)}
    finding = next(row for row in lab[1].findings_list(FindingsInput(snapshot_id=snapshot)).items if row.kind == "contradictory_intents")
    args = IntentInput(snapshot_id=snapshot, finding_ids=[finding.id], selected_outcome="choose_owner", choice="B", idempotency_key="native")
    spec = specs["eu5_intent_record"]
    class Context:
        async def choose(self, question_id, message, choices):
            assert "x" in message and {"A", "B"} <= set(choices)
            return "B"
    result = asyncio.run(spec.handler(args, Context()))
    assert result.state == "operator_selected" and result.decision_id
    assert len(service.store.list("lab_eu5_question")) == 1
    assert len(service.store.list("decision")) == 1


def test_no_production_apply_or_unbounded_tools_registered(lab):
    names = {spec.name for spec in build_operations(lab[0])}
    assert len(names) == 17
    assert "eu5_deployment_apply" not in names
    assert "eu5_runtime_probe_start" not in names
    assert not any(name.endswith(("shell", "sql", "python", "approve")) for name in names)


@pytest.mark.parametrize("fixture", [f"F{number:02}" for number in range(1, 21)])
def test_every_existing_fixture_uses_same_scanner_and_keeps_evidence_honest(lab, fixture):
    from backend.engine.fixtures import run_fixture

    snapshot = scan(lab, fixture)
    captured = lab[0].store.get("scan", snapshot)
    direct = run_fixture(fixture)
    assert captured["analysis"]["summary"] == direct["analysis"]["summary"]
    assert captured["analysis"]["coverage"]["conformed_rules"] == 0
    assert captured["analysis"]["coverage"]["game_runs"] == 0
    assert captured["analysis"]["summary"]["llm_calls"] == 0
