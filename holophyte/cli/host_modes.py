import subprocess
import sys
from pathlib import Path

from holophyte.cli.board_verbs import require_board
from holophyte.cli.operator import report
from holophyte.cli.status import host_status_report, status_report
from holophyte.cli.store_import import dry_run
from holophyte.host.registry import watched_line
from holophyte.host.supervisor import supervise, supervisor_liveness_line
from holophyte.host.supervisor_lock import SupervisorHeld, supervisor_running
from holophyte.serve.server import serve

# By path, so the supervisor runs the checkout of the loop that spawned it.
FACTORY_PATH = Path(__file__).resolve().parents[2] / "factory.py"
SPAWN = subprocess.Popen


def _host_mode(parser, args):
    from holophyte.host.registry import Host, HostError
    if args.serve is not None:
        from holophyte.serve.serve_host import serve_host
        return serve_host(Host.locate(), args.serve or None)
    if args.supervise:
        from holophyte.host.sweep_host import supervise_host
        return supervise_host(Host.locate(), once=args.once)
    if not args.status:
        parser.error("the following arguments are required: project (only "
                     "--status, --serve and --supervise have a host form)")
    try:
        return host_status_report(Host.locate(), as_json=args.json)
    except HostError as bad:
        raise SystemExit(str(bad)) from None


def _read_only_mode(args, target):
    if args.report:
        return lambda: report(target)
    if args.status:
        return lambda: status_report(target, as_json=args.json)
    if args.import_store is not None:
        return lambda: dry_run(target, args.import_store)
    if args.serve is not None:
        return lambda: serve(target, args.serve)
    return None


def _supervise_project(target, board):
    watched = watched_line(target)
    if watched is not None:
        raise SystemExit(watched)
    try:
        return supervise(target, require_board(target, board))
    except SupervisorHeld as held:
        raise SystemExit(
            f"{held}\n{supervisor_liveness_line(target)}") from None


def start_supervisor(target, out=None):
    """Two loops starting at once both spawn; the second exits at the lock."""
    out = sys.stdout if out is None else out
    watched = watched_line(target)
    if watched is not None:
        print(watched, file=out, flush=True)
        return None
    pid = supervisor_running(target)
    if pid is not None:
        print(f"[holo2] supervisor pid {pid} is watching {target.path}",
              file=out, flush=True)
        return None
    target.holo_dir.mkdir(parents=True, exist_ok=True)
    with open(target.holo_dir / "supervisor.log", "ab") as log:
        child = SPAWN(
            [sys.executable, "-u", str(FACTORY_PATH), "--supervise",
             str(target.path)],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log)
    print(f"[holo2] started a supervisor for {target.path} as pid {child.pid}",
          file=out, flush=True)
    return child.pid
