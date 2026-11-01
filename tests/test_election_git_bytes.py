"""Election evidence survives Git's Windows-style text conversion byte-for-byte."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")


@pytest.mark.skipif(GIT is None, reason="Git is required to verify checkout byte preservation.")
def test_election_attributes_preserve_evidence_with_autocrlf_enabled(tmp_path):
    repository, checkout = tmp_path / "repository", tmp_path / "checkout"
    templates, hooks = tmp_path / "empty-templates", tmp_path / "empty-hooks"
    for directory in (repository, checkout, templates, hooks):
        directory.mkdir()
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
        "WINDIR", "LANG", "LC_ALL"}}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")

    def git(*arguments):
        result = subprocess.run([GIT, "-c", f"core.hooksPath={hooks}", *arguments], cwd=repository,
                                env=environment, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        return result.stdout

    git("init", "--quiet", f"--template={templates}")
    git("config", "core.autocrlf", "true")
    git("config", "core.safecrlf", "false")
    (repository / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
    csv = b"cycle,margin\n2026,1.5\n"
    document = b'{"cycle": 2026,\n "margin": 1.5}\n'
    evidence = {
        "data/elections/fixture-lf.csv": csv,
        "data/elections/nested/fixture-crlf.csv": csv.replace(b"\n", b"\r\n"),
        "packages/election_model/src/election_model/data/fixture-lf.json": document,
        "packages/election_model/src/election_model/data/nested/fixture-crlf.json":
            document.replace(b"\n", b"\r\n"),
    }
    for relative, original in evidence.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)
    control = b"ordinary text\nshould undergo conversion\n"
    (repository / "control.txt").write_bytes(control)
    (repository / "control-crlf.txt").write_bytes(control.replace(b"\n", b"\r\n"))
    git("add", "--", ".gitattributes", "data/elections", "packages/election_model/src/election_model/data",
        "control.txt", "control-crlf.txt")

    # Verify both index storage and a fresh checkout; no commit or real index is used.
    for relative, original in evidence.items():
        assert git("show", f":{relative}") == original
    assert git("show", ":control-crlf.txt") == control
    git("checkout-index", "--all", "--force", f"--prefix={checkout.as_posix()}/")
    for relative, original in evidence.items():
        assert (checkout / relative).read_bytes() == original
    converted = control.replace(b"\n", b"\r\n")
    assert (checkout / "control.txt").read_bytes() == converted != control
    assert (checkout / "control-crlf.txt").read_bytes() == converted
