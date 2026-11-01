"""Transport-independent validation for the existing application commands."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import PAYLOADS


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    command: Literal[
        "create_workspace", "discover_ck3", "import_ck3", "scan", "stage", "decision",
        "validate", "export", "rollback", "investigate", "cancel", "backup", "runtime_probe",
        "configure_inference", "agent_index", "agent_prepare", "agent_apply", "agent_test",
        "agent_undo", "agent_check",
    ]
    workspace_id: str | None = None
    payload: dict = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_payload(self):
        self.payload = PAYLOADS[self.command].model_validate(self.payload).model_dump(exclude_none=True)
        if self.command != "create_workspace" and not self.workspace_id:
            raise ValueError("workspace_id is required")
        return self
