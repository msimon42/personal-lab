"""The election wheel and rebuilt sdist run without source or host packages."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import venv
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/election_model"


def _run(arguments, cwd, timeout=90):
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
        "WINDIR", "LANG", "LC_ALL", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}}
    result = subprocess.run(arguments, cwd=cwd, env=environment, capture_output=True,
                            text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _hashes(directory):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.glob("*.json")}


@pytest.fixture(scope="module")
def election_wheels(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-election")
    source = root / "source"
    original_hashes = _hashes(PACKAGE / "src/election_model/data")
    shutil.copytree(PACKAGE, source,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build", "dist"))
    direct, archives, unpacked, rebuilt = [root / name for name in ("direct", "archives", "unpacked", "rebuilt")]
    _run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(direct)], source)
    _run([sys.executable, "-m", "build", "--no-isolation", "--sdist", "--outdir", str(archives)], source)
    unpacked.mkdir()
    with tarfile.open(next(archives.glob("*.tar.gz"))) as archive:
        archive.extractall(unpacked, filter="data")
    _run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(rebuilt)],
         next(unpacked.iterdir()))
    wheels = {"wheel": next(direct.glob("*.whl")), "sdist": next(rebuilt.glob("*.whl"))}
    for wheel in wheels.values():
        with zipfile.ZipFile(wheel) as archive:
            installed_hashes = {Path(name).name: hashlib.sha256(archive.read(name)).hexdigest()
                                for name in archive.namelist()
                                if name.startswith("election_model/data/") and name.endswith(".json")}
            assert installed_hashes == original_hashes
            assert not any(name.startswith(("backend/", "personal_lab/", "toolkit_core/"))
                           for name in archive.namelist())
    yield wheels, original_hashes
    assert _hashes(PACKAGE / "src/election_model/data") == original_hashes


SMOKE = r'''
import builtins, hashlib, json, sys
from pathlib import Path
from datetime import date
from importlib.metadata import version

original_import = builtins.__import__
forbidden = {'backend', 'personal_lab', 'toolkit_core', 'mcp', 'fastapi', 'uvicorn'}
def guarded(name, *args, **kwargs):
    assert name.split('.')[0] not in forbidden, name
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded

import election_model
from election_model.contracts import BacktestRequest, ForecastRequest, ModelConfig
from election_model.service import ElectionService

assert Path(election_model.__file__).is_relative_to(Path(sys.prefix)), election_model.__file__
assert version('election-model') == '0.1.1'
assert Path(sys.argv[1]) not in [Path(value or '.').resolve() for value in sys.path]
data = Path(election_model.__file__).parent / 'data'
def hashes():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in data.glob('*.json')}
expected_hashes = json.loads(sys.argv[2])
assert hashes() == expected_hashes
service = ElectionService()
status = service.status()
assert status.historical_races == 749 and status.current_races == status.current_polls == 4
assert status.aggregator_races == 17
assert status.status == 'partial_race_coverage'
forecast = service.forecast(ForecastRequest(dataset_id='us-2026-11-01',
    config=ModelConfig(as_of=date(2026, 11, 1), draws=1000)))
assert forecast.status == 'partial' and forecast.modeled_race_count == 4
assert forecast.data_origin == 'bundled_public' and forecast.synthetic is False
assert len(forecast.forecasts) == 12
assert {row.scenario for row in forecast.forecasts} == {'baseline', 'historical', 'reversal'}
assert all(0 <= row.dem_win_probability <= 1 for row in forecast.forecasts)
backtest = service.backtest(BacktestRequest())
assert backtest.evaluated_race_count == 629 and backtest.history_count == 749
assert backtest.data_origin == 'bundled_public' and backtest.synthetic is False
assert hashes() == expected_hashes
assert not forbidden & {name.split('.')[0] for name in sys.modules}
print(json.dumps({'historical_races': status.historical_races, 'current_races': status.current_races,
    'scenario_rows': len(forecast.forecasts), 'evaluated_races': backtest.evaluated_race_count,
    'immutable_data_files': len(expected_hashes), 'host_or_game_imports': 0}))
'''


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_election_installed_artifact_without_checkout(tmp_path, election_wheels, artifact):
    wheels, expected_hashes = election_wheels
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    paths = json.loads(_run([str(python), "-I", "-c", "import sysconfig,json; print(json.dumps(sysconfig.get_paths()))"],
                            runtime))
    # Appending locked third-party distributions does not execute their editable
    # .pth files; the source checkout and its host packages remain unavailable.
    Path(paths["purelib"], "test-dependencies.pth").write_text(
        "import sys; sys.path.append(" + repr(sysconfig.get_path("purelib")) + ")\n", encoding="utf-8")
    _run([str(python), "-I", "-m", "installer", "--prefix", str(environment), str(wheels[artifact])], runtime)
    evidence = json.loads(_run([str(python), "-I", "-c", SMOKE, str(ROOT), json.dumps(expected_hashes)], runtime))
    assert evidence == {"historical_races": 749, "current_races": 4, "scenario_rows": 12,
                        "evaluated_races": 629, "immutable_data_files": len(expected_hashes),
                        "host_or_game_imports": 0}
