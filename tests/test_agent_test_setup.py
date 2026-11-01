"""Isolation setup contracts; the native boundary check never executes a game."""
import sys

import pytest

from backend.agent.ck3_test import check_os_boundary, normal_inventory, policy_for, prepare_probe
from backend.agent.issues import base_relation
from backend.app.store import Store


def prepare(tmp_path):
    normal = tmp_path / "normal"
    normal.mkdir()
    (normal / "save").write_bytes(b"untouched")
    installation = tmp_path / "installation"
    executable = installation / "binaries/fake.app/Contents/MacOS/fake-game"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"not executable, must not launch")
    content = installation / "game"
    content.mkdir()
    store = Store(tmp_path / "managed")
    discovery = {"workspace_id": "synthetic", "platform": "macos", "user_data": str(normal),
                 "installation": {"executable": str(executable), "content_root": str(content)}}
    return store, discovery, normal


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac setup only; no Windows qualification")
def test_prepare_is_inactive_and_policy_has_no_original_write_permission(tmp_path):
    store, discovery, normal = prepare(tmp_path)
    try:
        before = normal_inventory(normal)
        record = prepare_probe(store, discovery)
        assert not record["game_launched"] and record["mods_enabled"] == []
        assert record["gameplay_outcome"] == "not_run"
        assert normal_inventory(normal) == before
        assert (normal / "save").read_bytes() == b"untouched"
        assert record["profile"] != str(normal)
        for overlapping in (normal, normal / "subfolder", normal.parent):
            with pytest.raises(ValueError, match="separate"):
                policy_for(overlapping, normal)
    finally:
        store.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Native Mac OS boundary check")
def test_native_os_write_boundary_uses_only_temporary_data(tmp_path):
    store, discovery, normal = prepare(tmp_path)
    try:
        record = prepare_probe(store, discovery)
        result = check_os_boundary(store, record)
        assert result["executed"] and not result["game_launched"]
        assert (normal / "save").read_bytes() == b"untouched"
        assert not (store.root / record["prefix"] / "boundary-forbidden").exists()
        store.fs.write_bytes(record["prefix"] + "/isolation.sb", b"changed")
        with pytest.raises(ValueError, match="changed"):
            check_os_boundary(store, record)
    finally:
        store.close()


def test_unreadable_contributions_cannot_be_declared_equal_or_different():
    occurrences = [{"owner": "V", "token_sha256": "a"},
                   {"owner": "one", "token_sha256": "a"},
                   {"owner": "two", "token_sha256": None}]
    assert base_relation(occurrences, "one") == "identical"
    assert base_relation(occurrences, "two") == "unknown"
    assert base_relation(occurrences[1:], "one") == "not_available"
