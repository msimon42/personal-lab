"""Operator diagnostics and trusted decisions over the same application contracts."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import threading


def _dump(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _common(parser, profile="personal", suppress=False):
    from .profiles import PROFILES
    parser.add_argument("--state-dir", type=Path, default=argparse.SUPPRESS if suppress else None)
    parser.add_argument("--profile", choices=sorted(PROFILES), default=argparse.SUPPRESS if suppress else profile)
    parser.add_argument("--config", type=Path, default=argparse.SUPPRESS if suppress else None)


def _state(args):
    from .migration import default_state_dir
    return args.state_dir or default_state_dir("personal")


def _administrative_service(store):
    """No worker/owner startup: only coordinated short database operations."""
    from backend.app.service import Service
    from backend.agent.release import release_info
    service = Service.__new__(Service)
    service.root, service.store = store.root, store
    service.command_lock = threading.RLock()
    service.agent_release = release_info()
    return service


def trusted_decide(root, question_id):
    from toolkit_core.storage import Store
    from .migration import validate_state_ready
    validate_state_ready(root)
    store = Store(root)
    try:
        service = _administrative_service(store)
        try:
            question = store.get("lab_eu5_question", question_id)
            choices = question["choices"]
            _dump({"question_id": question_id, "decision": question["decision"],
                   "alternatives": question["alternatives"], "choices": choices})
            choice = input("Enter the exact offered choice, or leave blank to cancel: ")
            if choice not in choices:
                return {"state": "cancelled", "question_id": question_id}
            from backend.app.eu5_operations import accept_trusted_decision
            return accept_trusted_decision(service, question_id, choice)
        except KeyError:
            question = store.get("agent_question", question_id)
            from backend.agent.workflow import AgentWorkflow
            from backend.agent.approvals import sign, signing_key
            from backend.agent.release import installation_status
            if installation_status()["status"] == "mismatch":
                raise ValueError("The installed CK3 instructions and toolkit must match before a trusted choice.")
            flow = AgentWorkflow(service)
            if question["purpose"] == "choose":
                proposal = flow.proposal(question["workspace_id"], question["proposal_id"])
                labels = {option["label"]: option["id"] for option in proposal["options"]}
                _dump({"question_id": question_id, "proposal_hash": question["proposal_hash"],
                       "title": proposal["title"], "options": proposal["options"]})
            elif question["purpose"] == "undo":
                labels = {"Yes, continue": "Yes, continue", "No, leave it unchanged": "No, leave it unchanged"}
                _dump({"question_id": question_id, "proposal_id": question["proposal_id"], "purpose": "undo", "choices": list(labels)})
            else:
                raise ValueError("Game launch and activation remain unavailable; this question cannot be authorized.")
            choice = input("Enter the exact offered choice, or leave blank to cancel: ")
            answer = {"question_id": question_id, "nonce": question["nonce"],
                "proposal_hash": question["proposal_hash"], "action": "accept" if choice in labels else "cancel",
                "choice": labels.get(choice)}
            if question["purpose"] == "undo" and choice == "No, leave it unchanged":
                answer["action"] = "decline"
            result = flow.answer(answer, sign(signing_key(store), answer))
            if question["purpose"] == "undo" and choice == "Yes, continue":
                result = flow.undo(question["workspace_id"], question["proposal_id"])
            return result
    finally:
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lab", description="Headless personal tools for external agents")
    commands = parser.add_subparsers(dest="command", required=True)
    mcp = commands.add_parser("mcp", help="Run the configured MCP stdio tool host")
    _common(mcp)
    doctor = commands.add_parser("doctor", help="Inspect local capabilities without launching a game")
    _common(doctor)
    contracts = commands.add_parser("contracts", help="Print concrete schemas for the selected profile")
    _common(contracts, "dev")
    call = commands.add_parser("call", help="Invoke one enabled typed operation with JSON arguments")
    _common(call)
    call.add_argument("operation")
    argument_source = call.add_mutually_exclusive_group()
    argument_source.add_argument("--arguments", help="JSON object validated by the operation's input contract")
    argument_source.add_argument("--arguments-file", type=Path,
                                 help="Read a bounded UTF-8 JSON argument object from an explicit terminal path")
    runs = commands.add_parser("runs", help="Inspect or cancel persistent runs")
    _common(runs)
    actions = runs.add_subparsers(dest="action", required=True)
    inspect_run = actions.add_parser("inspect")
    _common(inspect_run, suppress=True)
    inspect_run.add_argument("run_id")
    events = actions.add_parser("events")
    _common(events, suppress=True)
    events.add_argument("run_id")
    events.add_argument("--cursor", type=int, default=0)
    events.add_argument("--limit", type=int, default=50)
    cancel = actions.add_parser("cancel")
    _common(cancel, suppress=True)
    cancel.add_argument("run_id")
    fixtures = commands.add_parser("fixtures", help="Run real synthetic fixture contracts")
    fixtures.add_argument("--id")
    materialize = commands.add_parser("materialize", help="Materialize an untouched synthetic source tree")
    materialize.add_argument("fixture_id")
    materialize.add_argument("destination", type=Path)
    backup = commands.add_parser("backup", help="Consistently back up closed or coordinated state")
    _common(backup)
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="Restore a verified backup into an empty managed root")
    restore.add_argument("source", type=Path)
    restore.add_argument("destination", type=Path)
    migrate = commands.add_parser("migrate", help="Relocate closed state with a verified backup")
    migrate.add_argument("source", type=Path)
    migrate.add_argument("destination", type=Path)
    migrate.add_argument("--backup", type=Path, required=True)
    migrate.add_argument("--previous-source-root", type=Path,
                         help="Absent former root after a rename; map only verified artifact paths")
    recover = commands.add_parser("recover-migration")
    recover.add_argument("destination", type=Path)
    restore_recovery = commands.add_parser("recover-restore", help="Verify and finish an interrupted restore publication")
    restore_recovery.add_argument("destination", type=Path)
    install = commands.add_parser("install", help="Install the already built owned agent connection")
    _common(install)
    install.add_argument("--config-path", type=Path)
    install.add_argument("--skill-destination", type=Path)
    approve = commands.add_parser("approve", help="Review and authorize one immutable external-action plan")
    _common(approve)
    approve.add_argument("plan_id")
    decide = commands.add_parser("decide", help="Answer a prepared operator design question")
    _common(decide)
    decide.add_argument("question_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "fixtures":
            from backend.engine.fixtures import catalog, run_fixture
            chosen = [entry for entry in catalog() if args.id is None or entry["id"] == args.id]
            if not chosen:
                raise ValueError("Unknown fixture ID")
            results = [run_fixture(entry["id"]) for entry in chosen]
            _dump({"fixtures": [{"id": result["id"], "passed": result["passed"]} for result in results],
                   "game_runs": 0, "model_calls": 0})
            return 0 if all(result["passed"] for result in results) else 1
        if args.command == "materialize":
            from backend.engine.fixture_directories import materialize_fixture
            _dump(materialize_fixture(args.fixture_id, args.destination))
        elif args.command == "restore":
            from .migration import restore_state
            _dump(restore_state(args.source, args.destination))
        elif args.command == "migrate":
            from .migration import relocate_state
            _dump(relocate_state(args.source, args.destination, args.backup,
                                 previous_source_root=args.previous_source_root))
        elif args.command == "recover-migration":
            from .migration import recover_relocation
            _dump(recover_relocation(args.destination))
        elif args.command == "recover-restore":
            from .migration import recover_restore
            _dump(recover_restore(args.destination))
        elif args.command == "install":
            from .installation import install_connection
            _dump(install_connection(_state(args), config_path=args.config_path,
                                    skill_destination=args.skill_destination, profile=args.profile))
        elif args.command == "approve":
            from toolkit_core.authorization import PlanManager
            from toolkit_core.storage import Store
            from .migration import validate_state_ready
            validate_state_ready(_state(args))
            store = Store(_state(args))
            try:
                plans = PlanManager(store)
                record = plans.inspect(args.plan_id)
                _dump(record)
                expected = record["plan_digest"]
                answer = input("Type approve followed by the displayed plan digest to authorize: ")
                _dump(plans.approve(args.plan_id, expected) if answer == "approve " + expected else {"state": "cancelled"})
            finally:
                store.close()
        elif args.command == "decide":
            _dump(trusted_decide(_state(args), args.question_id))
        elif args.command == "backup":
            from toolkit_core.storage import Store
            from toolkit_core.safety import ManagedLock
            from .migration import validate_state_ready
            validate_state_ready(_state(args))
            with ManagedLock(_state(args) / "coordinator.lock"):
                store = Store(_state(args))
                try:
                    _dump({"path": str(store.backup(args.destination))})
                finally:
                    store.close()
        elif args.command == "runs" and args.action in {"inspect", "events"}:
            from .bootstrap import administrative_application
            app = administrative_application(_state(args), profile=args.profile)
            try:
                _dump(app.project_run(args.run_id) if args.action == "inspect" else
                      app.project_events(args.run_id, args.cursor, args.limit))
            finally:
                app.store.close()
        else:
            from .bootstrap import create_application
            with create_application(_state(args), profile=args.profile, config_path=args.config) as app:
                if args.command == "mcp":
                    from .mcp_server import run
                    run(app)
                elif args.command == "doctor":
                    _dump(asyncio.run(app.execute("lab_capabilities", {})))
                elif args.command == "contracts":
                    _dump(app.contracts())
                elif args.command == "call":
                    arguments_text = args.arguments
                    if args.arguments_file is not None:
                        from .bootstrap import MAX_INPUT_BYTES
                        with args.arguments_file.open("rb") as source:
                            raw = source.read(MAX_INPUT_BYTES + 1)
                        if len(raw) > MAX_INPUT_BYTES:
                            raise ValueError("Argument file exceeds the 262144-byte application input limit.")
                        arguments_text = raw.decode("utf-8")
                    arguments = json.loads(arguments_text if arguments_text is not None else "{}")
                    if not isinstance(arguments, dict):
                        raise ValueError("Operation arguments must be a JSON object.")
                    result = asyncio.run(app.execute(args.operation, arguments))
                    _dump(result)
                    return 2 if result.status in {"blocked", "error"} else 0
                elif args.command == "runs":
                    name = {"inspect": "lab_run_get", "events": "lab_run_events", "cancel": "lab_run_cancel"}[args.action]
                    values = {"run_id": args.run_id}
                    if args.action == "events":
                        values.update(cursor=args.cursor, limit=args.limit)
                    _dump(asyncio.run(app.execute(name, values)))
        return 0
    except (ValueError, KeyError, OSError, EOFError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
