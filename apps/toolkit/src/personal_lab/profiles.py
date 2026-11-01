"""Trusted startup selection. Tool requests cannot edit profiles or roots."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

from pydantic import Field

from toolkit_core.contracts import StrictModel


class Configuration(StrictModel):
    installations: list[dict] = Field(default_factory=list)
    ck3_locations: dict = Field(default_factory=dict)


@dataclass(frozen=True)
class Profile:
    name: str
    toolsets: tuple[str, ...]
    fixture_only: bool = False


PROFILES = {
    "core-only": Profile("core-only", ("core",)),
    "dev": Profile("dev", ("core", "eu5"), fixture_only=True),
    "personal": Profile("personal", ("core", "eu5", "ck3")),
    "eu5-local": Profile("eu5-local", ("core", "eu5")),
    "ck3-local": Profile("ck3-local", ("core", "ck3")),
    "election-research": Profile("election-research", ("core", "election")),
    # This tiny unrelated toolset proves the extension seam; no production
    # profile enables it, and configuration cannot name an import path.
    "test-only": Profile("test-only", ("core", "text_statistics")),
}


def load_configuration(path: Path | None) -> Configuration:
    if path is None:
        return Configuration()
    from toolkit_core.safety import read_source_bytes, reject_link_components

    path = reject_link_components(Path(path).absolute())
    return Configuration.model_validate(tomllib.loads(
        read_source_bytes(path.parent, path.name, max_bytes=1024 * 1024, native_paths=True).decode("utf-8")))
