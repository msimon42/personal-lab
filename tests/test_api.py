"""Former browser-domain assertions exercised at the shared service boundary.

HTTP host/origin/session behavior retires with HTTP. Typed requests, idempotency,
preservation, and complete repair gates remain application requirements.
"""
import time
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from backend.app.service import Service
from backend.app.store import Conflict


@pytest.fixture
def application(tmp_path):
    service = Service(tmp_path / "state")
    try:
        yield service, service.bootstrap(), service.root
    finally:
        service.close()


def command(service, workspace, name, payload, **kwargs):
    result = service.command({"command": name, "workspace_id": workspace, "payload": payload,
                              "idempotency_key": kwargs.pop("key", uuid.uuid4().hex), **kwargs})
    if result.get("job_id"):
        for _ in range(500):
            job = service.store.get("job", result["job_id"])
            if job["state"] in {"completed", "failed", "cancelled", "interrupted"}:
                assert job["state"] == "completed", job
                return job["result"]
            time.sleep(0.01)
        pytest.fail("Job did not finish within bounded test wait")
    return result


def test_full_synthetic_scan_stage_validate_export_preserves_inactive_state(application):
    service, bootstrap, root = application
    workspace = bootstrap["workspaces"][0]["id"]
    scan = command(service, workspace, "scan", {"fixture_id": "003-disjoint-properties"})
    record = service.store.get("scan", scan["scan_id"])
    assert record["status"] == "complete"
    assert record["analysis"]["profile"]["kind"] == "synthetic"
    staged = command(service, workspace, "stage", {"scan_id": scan["scan_id"]})
    validated = command(service, workspace, "validate", {"candidate_id": staged["candidate_id"]})
    assert validated["validation"]["experimental_eligible"]
    assert not validated["validation"]["validated_promotion_eligible"]
    exported = command(service, workspace, "export", {"candidate_id": staged["candidate_id"], "kind": "experimental_bundle"})
    artifact = service.store.get("artifact", exported["artifact_id"])
    with zipfile.ZipFile(BytesIO(service.store.fs.read_bytes(str(Path(artifact["path"]).relative_to(root))))) as bundle:
        assert "manifest.json" in bundle.namelist()
        assert b"x = 2" in b"\n".join(bundle.read(name) for name in bundle.namelist() if name.endswith(".txt"))
    assert not (root / "managed/active-release.json").exists()
    assert not (root / "managed/playsets.json").exists()


def test_shared_schema_and_request_idempotency(application):
    service, bootstrap, _ = application
    workspace = bootstrap["workspaces"][0]["id"]
    body = {"command": "scan", "workspace_id": workspace, "payload": {"fixture_id": "003-disjoint-properties"},
            "idempotency_key": "same"}
    first = service.command(body)
    assert service.command(body) == first
    with pytest.raises(Conflict):
        service.command({**body, "payload": {"fixture_id": "other"}})
    with pytest.raises(ValueError):
        service.command({**body, "idempotency_key": "bad", "payload": {"shell": "echo forbidden"}})


def test_rescan_stales_candidates_and_decisions(application):
    service, bootstrap, _ = application
    workspace = bootstrap["workspaces"][0]["id"]
    first = command(service, workspace, "scan", {"fixture_id": "003-disjoint-properties"})
    candidate = command(service, workspace, "stage", {"scan_id": first["scan_id"]})
    command(service, workspace, "scan", {"fixture_id": "003-disjoint-properties"})
    assert service.store.get("candidate", candidate["candidate_id"])["status"] == "stale"
    with pytest.raises(Conflict):
        service.decide(service.store.get("workspace", workspace),
                       {"scan_id": first["scan_id"], "choice": "A", "reason": "stale"})


def test_trusted_design_decision_is_narrow_and_revalidated(application):
    service, bootstrap, _ = application
    workspace = bootstrap["workspaces"][0]["id"]
    scan = command(service, workspace, "scan", {"fixture_id": "F04"})
    blocked = command(service, workspace, "stage", {"scan_id": scan["scan_id"]})
    assert blocked["status"] == "blocked"
    # This direct internal call models the already verified operator adapter.
    # Public agent intent calls cannot create this authority; tested separately.
    service.decide(service.store.get("workspace", workspace), {
        "scan_id": scan["scan_id"], "choice": "B", "reason": "Retain B and waive only A's competing exact value"})
    staged = command(service, workspace, "stage", {"scan_id": scan["scan_id"]})
    validated = command(service, workspace, "validate", {"candidate_id": staged["candidate_id"]})
    assert validated["validation"]["experimental_eligible"]
    waived = [entry for entry in validated["validation"]["obligations"] if entry["state"] == "waived_by_user"]
    assert len(waived) == 1 and waived[0]["owner"] == "A"
    assert waived[0]["decision_id"]


def test_valid_syntax_cannot_hide_lost_contribution_or_omit_obligation(application):
    import base64
    import hashlib
    service, bootstrap, _ = application
    workspace = bootstrap["workspaces"][0]["id"]
    scan = command(service, workspace, "scan", {"fixture_id": "F03"})
    staged = command(service, workspace, "stage", {"scan_id": scan["scan_id"]})
    candidate = service.store.get("candidate", staged["candidate_id"])
    raw = b"REPLACE:item = { x = 1 y = 3 }\n"
    candidate["plan"]["files"][0].update(bytes_base64=base64.b64encode(raw).decode(), sha256=hashlib.sha256(raw).hexdigest())
    candidate["plan"]["obligations"] = []
    service.store.put("candidate", candidate)
    validation = command(service, workspace, "validate", {"candidate_id": candidate["id"]})["validation"]
    assert not validation["experimental_eligible"]
    assert any(entry["owner"] == "A" and entry["state"] == "violated" for entry in validation["obligations"])
    assert next(gate for gate in validation["gates"] if gate["id"] == "obligation_closure")["outcome"] == "fail"


def test_single_coordinator_and_restart_job_reconciliation(tmp_path):
    root = tmp_path / "state"
    first = Service(root)
    with pytest.raises((ValueError, TimeoutError, OSError)):
        Service(root)
    first.store.put("job", {"id": "interrupted", "state": "running", "phase": "scan"})
    first.close()
    second = Service(root)
    try:
        reconciled = second.store.get("job", "interrupted")
        assert reconciled["state"] == "failed" and reconciled["phase"] == "interrupted"
    finally:
        second.close()
