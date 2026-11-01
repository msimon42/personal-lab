"""Mac isolation qualification, separate from mod behavior or repair approval.

Preparing a probe never launches CK3. The operator must obtain explicit consent
before start_probe. Output creation and a clean log are not gameplay evidence.
"""
from __future__ import annotations

import json
import ctypes
import errno
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time

from backend.app.store import Conflict, canonical, digest, uid
from backend.engine.safety import hash_source_file, reject_link_components

PLAN_FORMAT = "ck3-isolation-plan-2"
ACTIVE_STATES = {"starting", "running", "stopping", "unknown"}


def policy_for(profile: Path, normal_data: Path):
    profile, normal_data = profile.absolute(), normal_data.absolute()
    if profile == normal_data or profile.is_relative_to(normal_data) or normal_data.is_relative_to(profile):
        raise ValueError("Test data and normal game data must be separate.")
    # The candidate userdir option is not trusted for isolation. The OS policy
    # denies ALL original writes even if CK3 ignores that option entirely.
    return '\n'.join(['(version 1)', '(allow default)', '(deny file-write*)',
        f'(allow file-write* (subpath {json.dumps(str(profile))}))',
        f'(deny file-read* (subpath {json.dumps(str(normal_data))}))', '(deny network*)', ''])


def normal_inventory(root):
    root = reject_link_components(Path(root))
    if not root.is_dir():
        raise ValueError("The normal game-data directory is missing or unavailable.")
    result = []
    def traversal_failed(error):
        raise error
    for current, dirs, files in os.walk(root, followlinks=False, onerror=traversal_failed):
        for name in dirs + files:
            path = Path(current) / name
            if path.is_symlink():
                raise ValueError("Normal game data contains a link; its isolation must be resolved before testing.")
        for name in sorted(files):
            path = Path(current) / name
            info = path.stat()
            result.append([path.relative_to(root).as_posix(), info.st_size, info.st_mtime_ns, info.st_ino])
    return sorted(result)


def prepare_probe(store, discovery):
    if discovery.get("target_platform", discovery.get("platform")) != "macos" or sys.platform != "darwin":
        raise ValueError("This test setup currently requires its separate Mac qualification. Windows was not tested.")
    installation = discovery["installation"]
    executable = reject_link_components(Path(installation["executable"]))
    options = discovery.get("options", {})
    normal = options.get("user_data_path") or discovery.get("user_data")
    if not normal:
        raise ValueError("The normal game-data folder must be identified before making a test setup.")
    normal = reject_link_components(Path(normal))
    before = normal_inventory(normal)
    # Validate separation before creating anything under the managed root.
    for original in (normal, Path(installation["content_root"]), executable.parent):
        original = reject_link_components(original.absolute())
        if store.root == original or store.root.is_relative_to(original) or original.is_relative_to(store.root):
            raise ValueError("Managed test data and original source roots must be separate.")
    checksum = hash_source_file(executable.parent, executable.name, native_paths=True)
    identifier = uid("isolation")
    prefix = "agent/tests/" + identifier
    initial = {"dlc_load.json": b'{"enabled_mods":[],"disabled_dlcs":[]}\n',
               "tmp/.keep": b"", "cache/.keep": b""}
    for path, raw in initial.items():
        store.fs.write_bytes(prefix + "/profile/" + path, raw, replace=False)
    profile = store.fs.resolve(prefix + "/profile")
    policy = policy_for(profile, normal).encode()
    store.fs.write_bytes(prefix + "/isolation.sb", policy, replace=False)
    plan = {"format": PLAN_FORMAT, "id": identifier, "workspace_id": discovery["workspace_id"], "game_id": "ck3",
        "target_platform": "macos",
        "executable": str(executable), "executable_sha256": checksum["sha256"],
        "content_root": installation["content_root"], "normal_data": str(normal), "prefix": prefix,
        "profile": str(profile), "policy_sha256": digest(policy), "normal_before": before,
        "initial_files": {path: digest(raw) for path, raw in initial.items()},
        "argv": [str(executable), "-userdir=" + str(profile), "-gdpr-compliant"],
        "cwd": str(executable.parents[3]),
        "argument_status": "Candidate inspected in the installed binary, not executed or qualified.",
        "gameplay_outcome": "not_run", "mods_enabled": [], "network": "denied",
        "purpose": "Establish a separate game-data setup before testing any mod change.",
        "observation_scope": "OS isolation and process lifecycle only; no game initialization, loading or gameplay qualification."}
    raw_plan = canonical(plan)
    store.fs.write_bytes(prefix + "/plan.json", raw_plan, replace=False)
    return store.put("agent_isolation", {**plan, "plan_sha256": digest(raw_plan), "state": "prepared",
                                          "game_launched": False, "process_started": False})


def _require_current_binding(record):
    if record.get("relocation_historical") or record.get("execution_eligible") is False:
        raise Conflict("This relocated probe is historical. Prepare and approve a fresh plan for this instance.")


def _preconditions(store, record):
    _require_current_binding(record)
    if record.get("format") != PLAN_FORMAT or not record.get("plan_sha256"):
        raise ValueError("This legacy probe needs a fresh reviewed preparation before launching.")
    raw = store.fs.read_bytes(record["prefix"] + "/plan.json")
    if digest(raw) != record["plan_sha256"]:
        raise Conflict("The reviewed launch plan changed; prepare a fresh probe.")
    plan = json.loads(raw)
    if any(record.get(key) != value for key, value in plan.items()):
        raise Conflict("The saved launch target or arguments differ from the reviewed plan.")
    executable = Path(plan["executable"])
    if hash_source_file(executable.parent, executable.name, native_paths=True)["sha256"] != plan["executable_sha256"]:
        raise Conflict("The game executable changed. Prepare a fresh test.")
    if normal_inventory(plan["normal_data"]) != plan["normal_before"]:
        raise Conflict("Normal game data changed after the test was prepared.")
    if digest(store.fs.read_bytes(plan["prefix"] + "/isolation.sb")) != plan["policy_sha256"]:
        raise Conflict("The test isolation settings changed.")
    profile = reject_link_components(Path(plan["profile"]))
    expected = dict(plan["initial_files"])
    actual = {}
    for path in profile.rglob("*"):
        reject_link_components(path)
        if path.is_file():
            relative = path.relative_to(profile).as_posix()
            actual[relative] = digest(store.fs.read_bytes(plan["prefix"] + "/profile/" + relative))
        if len(actual) > 128:
            raise Conflict("The private profile changed after preparation.")
    # The native positive control may have already been run by the operator.
    if actual.get("boundary-allowed") == digest(b"probe"):
        actual.pop("boundary-allowed")
    if actual != expected:
        raise Conflict("The private profile changed; only the reviewed empty-mod setup may launch.")
    return plan


def check_os_boundary(store, record):
    """Negative control writes only disposable managed locations, never originals."""
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("The Mac process isolation tool is unavailable.")
    _require_current_binding(record)
    policy = store.fs.resolve(record["prefix"] + "/isolation.sb")
    if digest(store.fs.read_bytes(record["prefix"] + "/isolation.sb")) != record["policy_sha256"]:
        raise ValueError("The test isolation settings changed.")
    allowed = Path(record["profile"]) / "boundary-allowed"
    forbidden = store.root / record["prefix"] / "boundary-forbidden"
    program = ('from pathlib import Path\n'
        f'Path({str(allowed)!r}).write_text("probe")\n'
        'try:\n'
        f' Path({str(forbidden)!r}).write_text("must be denied")\n'
        'except PermissionError:\n print("write boundary enforced")\n'
        'else:\n raise SystemExit("write escaped test profile")\n')
    result = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(policy), sys.executable, "-c", program],
        cwd=record["profile"], capture_output=True, timeout=30, check=False)
    if result.returncode != 0 or result.stdout.strip() != b"write boundary enforced" or forbidden.exists():
        raise ValueError("The process write boundary was not established. CK3 was not launched.")
    return {"executed": True, "kind": "os_write_boundary", "game_launched": False,
            "negative_control": "A write outside the private profile was denied.",
            "positive_control": "A write inside the private profile succeeded."}


def start_probe(store, identifier):
    """Operator entry point, called only after explicit approval of this probe."""
    with store.atomic():
        record = store.get("agent_isolation", identifier)
        _require_current_binding(record)
        if record["state"] != "prepared":
            raise ValueError("This isolation probe was already started or changed.")
        _preconditions(store, record)
        active = store.connection.execute(
            "SELECT id FROM records WHERE kind='agent_isolation' AND id<>? "
            "AND json_extract(payload,'$.state') IN ('starting','running','stopping','unknown') "
            "AND json_extract(payload,'$.executable')=? LIMIT 1", (identifier, record["executable"])).fetchone()
        if active:
            raise Conflict("Another probe for this executable needs an observed terminal state first.")
        running = subprocess.run(["/bin/ps", "-axo", "comm="], capture_output=True, check=True).stdout.decode()
        if any(line.strip() == record["executable"] for line in running.splitlines()):
            raise ValueError("CK3 is already running. It was not interrupted and a second game was not launched.")
        boundary = check_os_boundary(store, record)
        _preconditions(store, record)
        # Persist intent before spawning. A crash here is ambiguous and cannot
        # authorize an automatic retry or a signal against a guessed process.
        record = store.put("agent_isolation", {**record, "state": "starting", "launch_attempted": True,
            "game_launched": None, "started_at": time.time(), "boundary_check": boundary},
            expected_revision=record["revision"])
    log_path = record["prefix"] + "/profile/process.log"
    env = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "DISPLAY", "SSH_AUTH_SOCK"}}
    env.update(TMPDIR=record["profile"] + "/tmp", XDG_CACHE_HOME=record["profile"] + "/cache")
    process = None
    try:
        store.fs.write_bytes(log_path, b"", replace=False)
        with store.fs._parent(log_path) as (parent, name):
            descriptor = os.open(name, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(descriptor)
                raise ValueError("The probe log must be an unshared regular file.")
        with os.fdopen(descriptor, "ab") as log:
            process = subprocess.Popen(["/usr/bin/sandbox-exec", "-f", str(store.fs.resolve(record["prefix"] + "/isolation.sb")),
                *record["argv"]], cwd=record["cwd"], env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        # Record the handle before querying it, so observation failures retain
        # the PID without mistaking a wrapper spawn for game initialization.
        record = store.put("agent_isolation", {**record, "pid": process.pid, "process_started": True},
                           expected_revision=record["revision"])
        identity = process_identity(process.pid)
        record = store.put("agent_isolation", {**record, "process_identity": identity},
                           expected_revision=record["revision"])
        process.poll()  # Reap an immediately exited child, when this caller owns it.
    except Exception:
        if process is None:
            # A completed pre-spawn failure (including Popen's child-exec error)
            # is authoritative non-start. Crashes and post-spawn failures remain
            # ambiguous and cannot authorize an automatic retry.
            with store.lock:
                current = store.get("agent_isolation", identifier)
                store.put("agent_isolation", {**current, "state": "failed_to_start", "game_launched": False,
                    "process_started": False, "finished_at": time.time()}, expected_revision=current["revision"])
        raise
    return _reconcile(store, record)


class _BSDInfo(ctypes.Structure):
    # macOS sys/proc_info.h: PROC_PIDTBSDINFO, including microsecond start time.
    _fields_ = ([(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
        + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        + [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
        + [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)])


def process_identity(pid):
    """An OS creation identity, or confirmed absence; failures remain unknown."""
    if type(pid) is not int or pid <= 0:
        raise ValueError("A positive process identifier is required.")
    if sys.platform != "darwin":
        raise OSError("Mac process identity observation is unavailable on this host.")
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    query = libproc.proc_pidinfo
    query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    query.restype = ctypes.c_int
    info = _BSDInfo()
    result = query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    if result != ctypes.sizeof(info):
        error = ctypes.get_errno()
        if result == 0 and error == errno.ESRCH:
            return None
        raise OSError(error, "The process creation identity could not be observed.")
    return {"pid": info.pid, "pgid": info.pgid, "started": [info.start_sec, info.start_usec],
            "zombie": info.status == 5}


def _group_members(group):
    result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="], capture_output=True,
                            text=True, check=True, timeout=5)
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) != 3:
            raise ValueError("The process-group observation was incomplete.")
        pid, pgid = int(fields[0]), int(fields[1])
        if pgid == group:
            members.append({"pid": pid, "zombie": fields[2].startswith("Z")})
    return members


def _same_process(expected, actual):
    return (isinstance(expected, dict) and isinstance(actual, dict) and
            all(expected.get(key) == actual.get(key) for key in ("pid", "pgid", "started")) and
            actual.get("pgid") == actual.get("pid"))


def _observe(record):
    pid = record.get("pid")
    if type(pid) is not int or pid <= 0:
        return {"state": "unknown", "reason": "The launch has no recorded process identity; do not retry or guess a process."}
    try:
        identity = process_identity(pid)
        members = _group_members(pid)
        live = [member["pid"] for member in members if not member["zombie"]]
        if identity is not None and not _same_process(record.get("process_identity"), identity):
            return {"state": "unknown", "reason": "The PID or process-group identity changed; it is not safe to signal.",
                    "live_group_members": live}
        if identity is None:
            if live:
                return {"state": "unknown", "reason": "The leader exited but its process group still has live members.",
                        "live_group_members": live}
            return {"state": "exited", "reason": "The process and every live member of its group are absent."}
        if not identity["zombie"] and pid not in live:
            return {"state": "unknown", "reason": "The leader and group observations disagree; retry observation."}
        if not live:
            return {"state": "exited", "reason": "No live process-group member remains; zombies are not running."}
        return {"state": "running", "identity": identity, "live_group_members": live}
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {"state": "unknown", "reason": "Process observation failed: " + type(exc).__name__}


def _reconcile(store, record):
    observation = _observe(record)
    state = observation["state"]
    if state == "running" and record.get("stop_requested_at"):
        state = "stopping"
    if state == "exited" and record.get("stop_requested_at"):
        state = "stopped"
    update = {**record, "state": state, "process_observation": {**observation, "observed_at": time.time()}}
    if state in {"exited", "stopped"}:
        update.setdefault("finished_at", time.time())
    return store.put("agent_isolation", update, expected_revision=record["revision"])


def probe_status(store, identifier):
    with store.lock:
        record = store.get("agent_isolation", identifier)
        _require_current_binding(record)
        if record["state"] in ACTIVE_STATES:
            record = _reconcile(store, record)
    profile = Path(record["profile"])
    created = [path.relative_to(profile).as_posix() for path in profile.rglob("*") if path.is_file()]
    try:
        unchanged = normal_inventory(record["normal_data"]) == record["normal_before"]
        inventory_error = None
    except (OSError, ValueError) as exc:
        unchanged, inventory_error = None, type(exc).__name__
    return {"id": identifier, "state": record["state"], "game_launched": record["game_launched"],
        "created_files": created, "normal_data_metadata_unchanged": unchanged,
        "normal_data_inventory_error": inventory_error, "process_started": record.get("process_started", False),
        "process_observation": record.get("process_observation"), "game_initialization": "not_observed",
        "gameplay_outcome": "not_run", "qualification": "requires_direct_observation",
        "message": "Files created in the private profile do not establish that a mod feature works."}


def stop_probe(store, identifier, *, timeout=1.0):
    if type(timeout) not in {int, float} or not 0 <= timeout <= 5:
        raise ValueError("The stop observation timeout must be between zero and five seconds.")
    with store.lock:
        record = store.get("agent_isolation", identifier)
        _require_current_binding(record)
        if record["state"] not in ACTIVE_STATES:
            return record
        record = _reconcile(store, record)
        if record["state"] not in {"running", "stopping"}:
            return record
        record = store.put("agent_isolation", {**record, "state": "stopping",
            "stop_requested_at": record.get("stop_requested_at", time.time())}, expected_revision=record["revision"])
        # Recheck immediately before signaling; never kill a reused PID/group.
        if _observe(record)["state"] != "running":
            return _reconcile(store, record)
        try:
            os.killpg(record["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return store.put("agent_isolation", {**record, "state": "unknown",
                "process_observation": {"state": "unknown", "reason": "Stopping failed: " + type(exc).__name__}},
                expected_revision=record["revision"])
    deadline = time.monotonic() + timeout
    while True:
        with store.lock:
            record = _reconcile(store, store.get("agent_isolation", identifier))
        if record["state"] != "stopping" or time.monotonic() >= deadline:
            return record
        time.sleep(min(.05, max(0, deadline - time.monotonic())))
