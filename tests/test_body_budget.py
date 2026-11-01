"""Request and response bounds survive retirement of HTTP body middleware."""
from pydantic import ValidationError
import pytest

from backend.app.operations import Command
from toolkit_core.artifacts import ArtifactManager
from toolkit_core.contracts import ensure_instance
from toolkit_core.storage import Store


def test_unknown_mutation_fields_are_rejected_before_execution():
    with pytest.raises(ValidationError):
        Command.model_validate({"command": "scan", "workspace_id": "w", "payload": {"shell": "forbidden"},
                                "idempotency_key": "key"})


def test_request_identifier_and_typed_operation_limits_are_enforced():
    with pytest.raises(ValidationError):
        Command.model_validate({"command": "scan", "workspace_id": "w", "payload": {},
                                "idempotency_key": "x" * 129})
    from backend.app.eu5_operations import PatchInput
    with pytest.raises(ValidationError):
        PatchInput.model_validate({"workspace_id": "w", "expected_revision": 0,
                                  "idempotency_key": "k", "operations": []})


def test_artifact_pages_preserve_exact_bytes_and_explicit_continuation(tmp_path):
    store = Store(tmp_path / "state")
    try:
        manager = ArtifactManager(store, ensure_instance(store))
        raw = b"12\x0034\xff"
        artifact = manager.register_bytes(raw, "application/octet-stream")
        import base64
        first = manager.read(artifact.id, limit=4)
        second = manager.read(artifact.id, offset=first["next_offset"], limit=4)
        assert base64.b64decode(first["bytes_base64"]) + base64.b64decode(second["bytes_base64"]) == raw
        assert second["next_offset"] is None
        with pytest.raises(ValueError):
            manager.read(artifact.id, limit=8193)
    finally:
        store.close()
