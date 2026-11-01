"""Small static registry with lazy, reviewed factories."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class Toolset:
    name: str
    version: str
    description: str
    module: str
    requirements: tuple[str, ...] = ()

    def build(self, application):
        return import_module(self.module).build_operations(application)


TOOLSETS = {
    "core": Toolset("core", "1.0", "Local capabilities, run history and authorized artifacts.",
                    "personal_lab.toolsets.core"),
    "eu5": Toolset("eu5", "1.0", "Evidence-scoped EU5 inspection and inactive staged repairs.",
                   "personal_lab.toolsets.eu5", ("mod-engine",)),
    "ck3": Toolset("ck3", "1.0", "Saved CK3 investigations and reviewed inactive fixes.",
                   "personal_lab.toolsets.ck3", ("mod-engine",)),
    "election": Toolset("election", "1.0", "Public aggregate election research and polling-error scenarios.",
                        "personal_lab.toolsets.election", ("election-model",)),
    "text_statistics": Toolset("text_statistics", "1.0", "Test-only unrelated text computation.",
                               "personal_lab.toolsets.testing"),
}


def build_operations(application):
    operations = {}
    for name in application.profile.toolsets:
        for operation in TOOLSETS[name].build(application):
            if operation.name in operations:
                raise ValueError(f"Duplicate trusted tool registration: {operation.name}")
            operations[operation.name] = operation
    return dict(sorted(operations.items()))
