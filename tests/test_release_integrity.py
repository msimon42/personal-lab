"""Release identity covers execution code and original advisory schema bytes."""
import hashlib

import pytest

from backend.agent import release
from backend.engine import ck3


@pytest.fixture
def resource_tree(tmp_path, monkeypatch):
    package = tmp_path / "backend"
    files = {"agent/server.py": b"connector", "app/service.py": b"coordinator",
             "engine/parser.py": b"parser", "cli.py": b"entrypoint",
             "agent/data/knowledge.json": b'{"revision":1}'}
    for name, raw in files.items():
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "_manifest.json").write_bytes(b'{"revision":1}')
    (schemas / "trait.json").write_bytes(b'{"entity":"trait"}')
    workflow = tmp_path / "workflow"
    (workflow / "agents").mkdir(parents=True)
    (workflow / "SKILL.md").write_bytes(b"instructions")
    (workflow / "agents/openai.yaml").write_bytes(b"metadata")
    roots = {"backend": package}
    for name in ("toolkit_core", "personal_lab"):
        roots[name] = tmp_path / name
        roots[name].mkdir()
        (roots[name] / "component.py").write_bytes(name.encode())
    monkeypatch.setattr(release.resources, "files", lambda name: roots[name])
    monkeypatch.setattr(ck3, "SCHEMA_ROOT", schemas)
    return package, schemas, workflow


@pytest.mark.parametrize("relative", ["app/service.py", "engine/parser.py", "cli.py"])
def test_core_only_update_invalidates_release_without_changing_agent_files(resource_tree, relative):
    package, _, workflow = resource_tree
    before = release.release_info(workflow_root=workflow)
    path = package / relative
    path.write_bytes(path.read_bytes() + b"\n")
    after = release.release_info(workflow_root=workflow)
    assert after["fingerprint"] != before["fingerprint"]
    assert after["workflow_files"] == before["workflow_files"]
    assert after["knowledge_files"] == before["knowledge_files"]
    assert after["schema_files"] == before["schema_files"]
    assert after["implementation_files"]["backend/agent/server.py"] == before["implementation_files"]["backend/agent/server.py"]
    assert after["implementation_files"]["backend/" + relative] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name", ["trait.json", "_manifest.json"])
def test_schema_byte_only_update_invalidates_release(resource_tree, name):
    _, schemas, workflow = resource_tree
    before = release.release_info(workflow_root=workflow)
    path = schemas / name
    path.write_bytes(path.read_bytes() + b"\n")
    after = release.release_info(workflow_root=workflow)
    assert after["fingerprint"] != before["fingerprint"]
    assert after["implementation_files"] == before["implementation_files"]
    assert after["workflow_files"] == before["workflow_files"]
    assert after["knowledge_files"] == before["knowledge_files"]
    assert after["schema_files"]["schemas/ck3/" + name] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("component", ["toolkit_core", "personal_lab"])
def test_shared_core_or_transport_change_invalidates_release(resource_tree, component):
    package, _, workflow = resource_tree
    before = release.release_info(workflow_root=workflow)
    path = package.parent / component / "component.py"
    path.write_bytes(path.read_bytes() + b"\n")
    after = release.release_info(workflow_root=workflow)
    assert after["fingerprint"] != before["fingerprint"]
    assert after["implementation_files"]["backend/agent/server.py"] == before["implementation_files"]["backend/agent/server.py"]


def test_release_fingerprint_is_location_independent_and_ignores_bytecode(tmp_path, monkeypatch):
    from backend.engine import ck3
    package = tmp_path / "package"
    (package / "agent/data").mkdir(parents=True)
    (package / "agent/data/knowledge.json").write_bytes(b'{"revision":1}')
    (package / "agent/server.py").write_bytes(b"implementation")
    (package / "engine").mkdir()
    (package / "engine/parser.py").write_bytes(b"parser")
    (package / "__pycache__").mkdir()
    (package / "__pycache__/server.pyc").write_bytes(b"bytecode")
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "_manifest.json").write_bytes(b'{"revision":1}')
    (schemas / "trait.json").write_bytes(b'{"entity":"trait"}')
    monkeypatch.setattr(ck3, "SCHEMA_ROOT", schemas)
    roots = {"backend": package}
    for name in ("toolkit_core", "personal_lab"):
        roots[name] = tmp_path / name
        roots[name].mkdir()
        (roots[name] / "component.py").write_bytes(name.encode())
    monkeypatch.setattr(release.resources, "files", lambda name: roots[name])
    one, two = tmp_path / "one", tmp_path / "two"
    for path in (one, two):
        (path / "agents").mkdir(parents=True)
        (path / "references").mkdir()
        (path / "SKILL.md").write_bytes(b"workflow")
        (path / "agents/openai.yaml").write_bytes(b"metadata")
        (path / "references/guide.md").write_bytes(b"reference")
    first = release.release_info(workflow_root=one)
    assert set(first["implementation_files"]) == {"backend/agent/server.py", "backend/engine/parser.py",
        "toolkit_core/component.py", "personal_lab/component.py"}
    assert set(first["schema_files"]) == {"schemas/ck3/_manifest.json", "schemas/ck3/trait.json"}
    assert first == release.release_info(workflow_root=two)
    (package / "__pycache__/server.pyc").write_bytes(b"different generated bytecode")
    assert release.release_info(workflow_root=one) == first
    (package / "agent/data/knowledge.json").write_bytes(b'{"revision":2}')
    assert release.release_info(workflow_root=one)["fingerprint"] != first["fingerprint"]
