"""Immutable bounded artifacts. Possession of an ID is not authorization."""
from __future__ import annotations

import base64

from .contracts import ArtifactRef, LabError
from .storage import digest, uid


class ArtifactManager:
    def __init__(self, store, instance_id: str, authorize=None):
        self.store, self.instance_id, self.authorize = store, instance_id, authorize

    def register_bytes(self, raw: bytes, media_type: str, run_id=None, scope=None) -> ArtifactRef:
        checksum = self.store.blob(raw)
        record = self.store.put("lab_artifact", {"id": uid("artifact"), "instance_id": self.instance_id,
            "media_type": media_type, "byte_size": len(raw), "sha256": checksum,
            "run_id": run_id, "scope": scope or {"toolset": "core"}})
        return ArtifactRef.model_validate({k: record[k] for k in ArtifactRef.model_fields})

    def read(self, artifact_id: str, offset: int = 0, limit: int = 8192) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8192:
            raise LabError("VALIDATION_FAILED", "Artifact reads require a nonnegative offset and at most 8192 bytes.")
        record = self.store.get("lab_artifact", artifact_id)
        if record["instance_id"] != self.instance_id:
            raise LabError("PATH_DENIED", "Artifact belongs to another instance.")
        scope = record["scope"]
        if scope.get("toolset") != "core" and (self.authorize is None or not self.authorize(record)):
            raise LabError("PATH_DENIED", "Artifact is outside the active profile or disclosure policy.")
        checksum = record["sha256"]
        raw = self.store.fs.read_bytes(f"blobs/sha256/{checksum[:2]}/{checksum}")
        if digest(raw) != checksum or len(raw) != record["byte_size"]:
            raise LabError("STALE_INPUT", "Immutable artifact integrity check failed.")
        current = self.store.get("lab_artifact", artifact_id)
        if current != record:
            raise LabError("STALE_INPUT", "Artifact metadata changed during the bounded read.")
        if scope.get("toolset") != "core" and (self.authorize is None or not self.authorize(record)):
            raise LabError("PATH_DENIED", "Artifact disclosure changed during the bounded read.")
        end = min(offset + limit, len(raw))
        return {"artifact": ArtifactRef.model_validate({k: record[k] for k in ArtifactRef.model_fields}).model_dump(),
            "offset": offset, "bytes_base64": base64.b64encode(raw[offset:end]).decode(),
            "next_offset": end if end < len(raw) else None}
