"""Explicit application contracts shared by all transports and toolsets."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Empty(StrictModel):
    pass


class Error(StrictModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class LabError(ValueError):
    def __init__(self, code: str, message: str, retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code, self.retryable, self.details = code, retryable, details or {}


class ArtifactRef(StrictModel):
    id: str
    media_type: str
    byte_size: int
    sha256: str
    run_id: str | None = None


T = TypeVar("T")


class Result(StrictModel, Generic[T]):
    schema_version: str = "1.0"
    instance_id: str
    status: Literal["ok", "accepted", "needs_input", "blocked", "error"] = "ok"
    summary: str
    data: T | None = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    next_cursor: str | None = None
    error: Error | None = None


@dataclass(frozen=True)
class OperationSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[..., Any]
    read_only: bool = False
    effect: str = "internal"
    requires_context: bool = False


def ensure_instance(store) -> str:
    from .storage import uid
    with store.atomic():
        try:
            return store.get("configuration", "lab-instance")["instance_id"]
        except KeyError:
            value = uid("instance")
            store.put("configuration", {"id": "lab-instance", "instance_id": value})
            return value
