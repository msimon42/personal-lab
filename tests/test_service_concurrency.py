"""Approved intent must not change underneath a published candidate/report."""
from concurrent.futures import ThreadPoolExecutor
import threading
import uuid

import pytest

from backend.app.service import Service
from backend.app.store import Conflict
from backend.app.eu5_operations import EU5Operations, IntentInput, accept_trusted_decision


def noop_checkpoint(*_args):
    pass


@pytest.fixture
def service(tmp_path):
    instance = Service(tmp_path / "state")
    try:
        yield instance
    finally:
        instance.close()


def approved_conflict(service):
    workspace = service.store.list("workspace")[0]
    scan = service._scan(workspace, {"fixture_id": "F04"}, noop_checkpoint)
    workspace = service.store.get("workspace", workspace["id"])
    change_choice(service, workspace, scan["scan_id"], "A")
    return workspace, scan["scan_id"]


def change_choice(service, workspace, scan_id, choice):
    scan = service.store.get("scan", scan_id)
    finding = next(row for row in scan["analysis"]["findings"] if row["kind"] == "contradictory_intents")
    proposed = EU5Operations(service).intent_record(IntentInput(snapshot_id=scan_id,
        finding_ids=[scan_id + "/" + finding["id"]], selected_outcome="choose_owner", choice=choice,
        reason=f"Retain {choice}'s exact value and narrowly waive the competing value.", idempotency_key=uuid.uuid4().hex))
    return accept_trusted_decision(service, proposed.question_id, choice)


def barrier_at(phase):
    reached, resume = threading.Event(), threading.Event()

    def checkpoint(current, *_args):
        if current == phase:
            reached.set()
            if not resume.wait(timeout=10):
                raise TimeoutError(f"Test did not release checkpoint {phase}")

    return checkpoint, reached, resume


def test_decision_during_stage_prevents_candidate_publication(service):
    workspace, scan_id = approved_conflict(service)
    checkpoint, reached, resume = barrier_at("staging immutable candidate")
    with ThreadPoolExecutor(max_workers=1) as pool:
        staged = pool.submit(service._stage, workspace, {"scan_id": scan_id}, checkpoint)
        try:
            assert reached.wait(timeout=5), "Stage did not reach its publication checkpoint"
            change_choice(service, workspace, scan_id, "B")
        finally:
            resume.set()
        with pytest.raises(Conflict):
            staged.result(timeout=5)

    assert service.store.list("candidate", workspace["id"]) == []
    assert not (service.root / "managed" / "active-release.json").exists()


def test_decision_during_validation_cannot_overwrite_stale_with_ready(service):
    workspace, scan_id = approved_conflict(service)
    staged = service._stage(workspace, {"scan_id": scan_id}, noop_checkpoint)
    candidate_id = staged["candidate_id"]
    checkpoint, reached, resume = barrier_at("simulating complete final stack")
    with ThreadPoolExecutor(max_workers=1) as pool:
        validated = pool.submit(service._validate, workspace, {"candidate_id": candidate_id}, checkpoint)
        try:
            assert reached.wait(timeout=5), "Validation did not reach its final-stack checkpoint"
            change_choice(service, workspace, scan_id, "B")
        finally:
            resume.set()
        with pytest.raises(Conflict):
            validated.result(timeout=5)

    current = service.store.get("candidate", candidate_id)
    assert current["status"] == "stale"
    assert not current.get("validation", {}).get("experimental_eligible", False)
    assert service.store.list("artifact", workspace["id"]) == []


def test_previously_validated_candidate_cannot_export_after_intent_changes(service):
    workspace, scan_id = approved_conflict(service)
    staged = service._stage(workspace, {"scan_id": scan_id}, noop_checkpoint)
    candidate_id = staged["candidate_id"]
    validation = service._validate(workspace, {"candidate_id": candidate_id}, noop_checkpoint)
    assert validation["validation"]["experimental_eligible"]

    change_choice(service, workspace, scan_id, "B")

    with pytest.raises(Conflict):
        service._export(workspace, {
            "candidate_id": candidate_id, "kind": "experimental_bundle",
        }, noop_checkpoint)

    assert service.store.get("candidate", candidate_id)["status"] == "stale"
    assert service.store.list("artifact", workspace["id"]) == []
    assert not (service.root / "managed" / "active-release.json").exists()
