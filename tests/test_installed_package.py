"""All workspace wheels and sdists execute independently of the source checkout."""
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

from backend.agent.release import release_info

ROOT = Path(__file__).resolve().parents[1]


def _run(arguments, cwd, timeout=90):
    environment = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "HOME", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
        "WINDIR", "LANG", "LC_ALL", "USERPROFILE", "APPDATA", "LOCALAPPDATA"}}
    environment["CODEX_HOME"] = str(cwd / "codex")
    result = subprocess.run(arguments, cwd=cwd, env=environment, capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def installed_wheels(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-artifacts")
    source = root / "source"
    source.mkdir()
    for name in ("backend", "schemas", "skills", "packages", "apps"):
        shutil.copytree(ROOT / name, source / name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", "build", "dist"))
    for name in ("pyproject.toml", "setup.py", "MANIFEST.in", "README.md"):
        shutil.copy2(ROOT / name, source / name)
    stale = source / "build/lib/backend/obsolete_shell.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("raise RuntimeError('deleted web shell must not ship')\n")
    wheels = {"wheel": [], "sdist": []}
    for index, package in enumerate((source / "packages/toolkit_core", source, source / "apps/toolkit")):
        direct, archives, unpacked, rebuilt = [root / (name + str(index)) for name in ("direct", "archives", "unpacked", "rebuilt")]
        _run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(direct)], package)
        _run([sys.executable, "-m", "build", "--no-isolation", "--sdist", "--outdir", str(archives)], package)
        unpacked.mkdir()
        with tarfile.open(next(archives.glob("*.tar.gz"))) as archive:
            archive.extractall(unpacked, filter="data")
        _run([sys.executable, "-m", "build", "--no-isolation", "--wheel", "--outdir", str(rebuilt)], next(unpacked.iterdir()))
        wheels["wheel"].append(next(direct.glob("*.whl")))
        wheels["sdist"].append(next(rebuilt.glob("*.whl")))
    originals = {path.name: path.read_bytes() for path in (ROOT / "schemas/ck3").glob("*.json")}
    for artifacts in wheels.values():
        with zipfile.ZipFile(artifacts[1]) as archive:
            assert "backend/obsolete_shell.py" not in archive.namelist()
            assert not {"backend/app/main.py", "backend/agent/connection.py", "backend/agent/server.py"} & set(archive.namelist())
            assert {Path(name).name: archive.read(name) for name in archive.namelist()
                    if name.startswith("backend/engine/data/ck3/") and name.endswith(".json")} == originals
    yield wheels, hashlib.sha256(originals["_manifest.json"]).hexdigest(), release_info()["fingerprint"]
    assert {path.name: path.read_bytes() for path in (ROOT / "schemas/ck3").glob("*.json")} == originals


def _environment(tmp_path, wheels):
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    paths = json.loads(_run([str(python), "-I", "-c", "import sysconfig,json; print(json.dumps(sysconfig.get_paths()))"], runtime))
    # Reuse locked third-party distributions, without processing editable .pth
    # files or exposing source packages through their editable finders.
    Path(paths["purelib"], "test-dependencies.pth").write_text(
        "import sys; sys.path.append(" + repr(sysconfig.get_path("purelib")) + ")\n", encoding="utf-8")
    for wheel in wheels:
        _run([str(python), "-I", "-m", "installer", "--prefix", str(environment), str(wheel)], runtime)
    return python, runtime, Path(paths["scripts"]) / ("lab.exe" if os.name == "nt" else "lab")


SMOKE = r'''
import asyncio, base64, hashlib, json, sys, time, uuid
from pathlib import Path
import backend, personal_lab, toolkit_core
from backend.agent.release import release_info, skill_source
from backend.engine.ck3 import schema_manifest
from backend.app.service import Service
from mcp.client import Client
from mcp import StdioServerParameters

root=Path.cwd()
for package in (backend, personal_lab, toolkit_core):
    assert Path(package.__file__).is_relative_to(Path(sys.prefix)), package.__file__
assert Path(sys.argv[1]) not in [Path(p or '.').resolve() for p in sys.path]
manifest=schema_manifest()
assert manifest['manifest_sha256']==sys.argv[2] and len(manifest['schemas'])==24
assert manifest['conformed_rules']==0 and manifest['originals_changed']==[]
assert skill_source().is_relative_to(Path(sys.prefix))
assert release_info()['fingerprint']==sys.argv[3]
sources=root/'originals'
for name,raw in [('one',b'call_ally_interaction={effect={first=yes}}\r\n'),('two',b'call_ally_interaction={effect={second=yes}}\n')]:
    directory=sources/name/'common/character_interactions'; directory.mkdir(parents=True)
    (directory/'actions.txt').write_bytes(raw)
    (sources/name/'descriptor.mod').write_text('name="'+name+'"\n')
def hashes():
    return {str(p.relative_to(sources)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources.rglob('*') if p.is_file()}
before=hashes()
managed=root/'ck3'
service=Service(managed,initialize_default=False)
workspace=service.create_workspace({'game_id':'ck3','target_platform':'windows' if sys.platform=='win32' else 'macos','mode':'game','name':'Synthetic installed sources','sources':[{'owner':n,'path':str(sources/n)} for n in ['one','two']]})
service.close()

async def session_work(profile,state,workflow):
    parameters=StdioServerParameters(command=sys.executable,args=['-m','personal_lab','mcp','--profile',profile,'--state-dir',str(state)],cwd=str(root))
    async with Client(parameters) as client:
        tools={t.name:t for t in (await client.list_tools()).tools}
        async def call(name,**arguments):
            if 'idempotency_key' in tools[name].input_schema.get('required',[]):
                arguments.setdefault('idempotency_key',uuid.uuid4().hex)
            response=await client.call_tool(name,arguments)
            assert not response.is_error, response
            envelope=response.structured_content
            if envelope['data'] is None and envelope['artifacts']:
                data=b''; offset=0
                while True:
                    part=await call('lab_artifact_read',artifact_id=envelope['artifacts'][0]['id'],offset=offset)
                    data+=base64.b64decode(part['bytes_base64'])
                    if part['next_offset'] is None: break
                    offset=part['next_offset']
                return json.loads(data)
            return envelope['data']
        async def finish(job):
            identifier=job.get('run_id',job.get('job_id'))
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                record=await call('lab_run_get',run_id=identifier)
                if record['state'] in {'succeeded','failed','cancelled','interrupted'}:
                    assert record['state']=='succeeded',record
                    return record['result']
                await asyncio.sleep(.02)
            raise AssertionError('installed run did not finish')
        await workflow(call,finish,tools)

async def ck3(call,finish,tools):
    assert 'ck3_test_selected_changes' not in tools and 'ck3_enable_selected_changes' not in tools
    scanned=await finish(await call('ck3_scan_playset',workspace_id=workspace['id']))
    indexed=await finish(await call('ck3_examine_changes',workspace_id=workspace['id'],scan_id=scanned['scan_id']))
    args={'workspace_id':workspace['id'],'index_id':indexed['index_id']}
    declarations=await call('ck3_get_definition_contributions',**args,target='game/character_interaction/call_ally_interaction',limit=1)
    assert declarations['occurrence_count']==2 and len(declarations['occurrences'])==1
    last=await call('ck3_get_definition_contributions',**declarations['occurrences_continuation'])
    assert len(last['occurrences'])==1 and last['occurrences_continuation'] is None
    found=await call('ck3_search_sources',**args,query='actions.txt',kind='path')
    assert found['total']==2
    texts=[(await call('ck3_read_source',**args,source_id=row['source_id']))['text'] for row in found['results']]
    assert any('first=yes' in t for t in texts) and any('second=yes' in t for t in texts)
    report=await call('ck3_get_investigation_report',**args)
    assert report['summary']['total_concerns']>0 and report['summary']['gameplay_compatibility']=='not_established'

async def eu5(call,finish,tools):
    playsets=await call('eu5_playsets_list',installation_id='fixture:F03')
    started=await call('eu5_scan_start',installation_id='fixture:F03',playset_id=playsets['items'][0]['id'])
    scanned=await finish(started)
    findings=await call('eu5_findings_list',snapshot_id=scanned['scan_id'])
    assert findings['total']>0
    assert (await call('lab_capabilities'))['capabilities']['deployment']['available'] is False

asyncio.run(session_work('ck3-local',managed,ck3))
asyncio.run(session_work('dev',root/'eu5',eu5))
assert hashes()==before
assert not (managed/'agent/engine.json').exists()
assert not (managed/'managed/active-release.json').exists()
print(json.dumps({'source_files_unchanged':len(before),'schema_count':len(manifest['schemas']),'game_runs':0}))
'''


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_installed_workspace_runs_eu5_and_ck3_without_checkout(tmp_path, installed_wheels, artifact):
    wheels, manifest_hash, fingerprint = installed_wheels
    python, runtime, lab = _environment(tmp_path, wheels[artifact])
    assert lab.is_file() and "mcp" in _run([str(lab), "--help"], runtime)
    evidence = json.loads(_run([str(python), "-I", "-c", SMOKE, str(ROOT), manifest_hash, fingerprint], runtime))
    assert evidence == {"source_files_unchanged": 4, "schema_count": 24, "game_runs": 0}


def test_core_only_installed_without_mod_engine(tmp_path, installed_wheels):
    wheels, _, _ = installed_wheels
    python, runtime, _ = _environment(tmp_path, [wheels["wheel"][0], wheels["wheel"][2]])
    result = json.loads(_run([str(python), "-I", "-c", r'''
import asyncio,importlib.util,json,sys
from pathlib import Path
assert importlib.util.find_spec('backend') is None
from mcp.client import Client
from mcp import StdioServerParameters
async def run():
    async with Client(StdioServerParameters(command=sys.executable,args=['-m','personal_lab','mcp','--profile','core-only','--state-dir',str(Path.cwd()/'state')])) as client:
        tools=(await client.list_tools()).tools
        assert len(tools)==5 and all(t.name.startswith('lab_') for t in tools)
        response=await client.call_tool('lab_capabilities',{})
        assert not response.is_error
        return response.structured_content['data']['profile']
print(json.dumps({'profile':asyncio.run(run())}))
'''], runtime))
    assert result == {"profile": "core-only"}
