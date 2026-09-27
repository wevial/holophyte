"""A seeded host daemon around one command, for console captures.

    python3 -m tests.console_fixture [--build COMMAND] COMMAND [ARG...]

Run from the repository root. `main()` points `HOLOPHYTE_HOME` at a fresh
temporary directory, runs `--build` (shlex-split, from the root) when
given, seeds the home (`seed()`), starts the real host daemon
(`factory.py --serve 127.0.0.1:0`) on it, and runs COMMAND from the root
with `CONSOLE_URL` naming the daemon and `CONSOLE_TOKEN` its machine
token. Then it stops the daemon, removes the home and exits with
COMMAND's code. A failed build, seed or daemon start exits 1 naming the
step, and COMMAND does not run.

`seed(home)` registers one native project, `demo`, whose store holds a
ticket in each console column and two ended runs, all written through the
store's own writers and dated relative to the seeding moment.
"""
from __future__ import annotations

import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
KEY = "DEMO"
NAME = "demo"
SEC = 1000
MIN = 60 * SEC
HOUR = 60 * MIN
START_SEC = 30
STOP_SEC = 10
SERVING = re.compile(r"^\[holo2\] serving (\S+):(\d+) ")
DRAFT = """\
# Show the merge ledger as a timeline

## Summary

A draft: the Shipped view could read as a day-by-day timeline.
"""
QUESTION = ("merge? The reviewer passed round 1 and the pre-merge verify is"
            " green; approve with --approve DEMO-4.")
FINDINGS = [{"path": "holophyte/serve.py", "line": 12, "severity": "p1",
             "criterion": "AC1", "message": "the route is unmatched"}]


def seed(home):
    """Seed `home` as a host with the `demo` project; answer the machine
    token. `HOLOPHYTE_HOME` names `home` while it runs, whatever it named
    before, so nothing here writes to another home. A relative `home` is
    resolved first: the daemon takes the token file against its home."""
    home = Path(home).resolve()
    with patch.dict(os.environ, {"HOLOPHYTE_HOME": str(home)}):
        path = _register(home)
        token = _serve_table(home)
        _seed_store(path)
    return token


def _register(home):
    """A git repository `home/demo` with a native board, registered as
    `project add` registers it; its path."""
    import holophyte.cli
    from holophyte.project import Project

    path = home / NAME
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    target = Project.locate(path, adopt=False)
    target.holo_dir.mkdir(parents=True, exist_ok=True)
    target.config_path.write_text(
        f'[board]\nkind = "native"\nkey = "{KEY}"\n'
        f'[serve]\nname = "{NAME}"\n')
    with open(os.devnull, "w") as quiet, patch.object(sys, "stdout", quiet):
        code = holophyte.cli.cli(["project", "add", str(path)])
    if code:
        raise RuntimeError(f"project add {path} exited {code}")
    return path


def _serve_table(home):
    """The host registry's `[serve]`, actions on behind a 0600 machine
    token file in `home`; the token."""
    token = secrets.token_hex(16)
    token_file = home / "machine.token"
    token_file.write_text(token + "\n")
    token_file.chmod(0o600)
    registry = home / "host.toml"
    registry.write_text(f'[serve]\nmachine_token_file = "{token_file}"\n'
                        "actions = true\n" + registry.read_text())
    return token


def _seed_store(path):
    """DEMO-1 in backlog, DEMO-2 ready, DEMO-3 worked now, DEMO-4 asking
    the operator, DEMO-5 merged after two rounds, DEMO-6 failed; and a
    supervisor beating now."""
    import store
    import store.board
    import store.tickets
    from holophyte.project import Project
    from tests.phase_fixture import advance_phase, finish_run
    from tests.test_store_board import body

    now = int(time.time() * 1000)
    conn = store.open(str(Project.locate(path).store_path))
    try:
        project = store.tickets.ensure_project(conn, f"native:{KEY}", path)

        def filed(text, column="ready", at=now - 3 * 24 * HOUR):
            identifier = store.board.file_ticket(conn, project, KEY, text,
                                                 column=column, now=at)
            (ticket,) = conn.execute(
                "SELECT id FROM tickets WHERE linearIdentifier = ?",
                (identifier,)).fetchone()
            return ticket

        def claimed(text, at):
            ticket = filed(text)
            store.tickets.transition(conn, ticket, "in_flight")
            run = store.claim(conn, project, ticket, now=at)
            store.set_phase(conn, run, "working", now=at + MIN)
            return ticket, run

        filed(DRAFT, column="backlog")
        filed(body("Stream the orders list as CSV"))

        _, run = claimed(body("Page the shipped ledger"), now - 40 * MIN)
        store.heartbeat(conn, run, now=now)

        ticket, run = claimed(body("Retry a flaky verify once"), now - 3 * HOUR)
        store.record_review_round(conn, run, 1, "pass", "reviewer-model",
                                  started_at=now - 2 * HOUR,
                                  ended_at=now - 2 * HOUR + 5 * MIN)
        advance_phase(conn, run, "reviewing", now=now - 2 * HOUR)
        store.park(conn, run, "awaiting_merge_approval",
                   now=now - 2 * HOUR + 6 * MIN)
        store.tickets.transition(conn, ticket, "blocked_on_operator")
        store.set_question(conn, ticket, QUESTION, park_kind="question")

        ticket, run = claimed(body("Add export endpoint"), now - 26 * HOUR)
        store.record_review_round(
            conn, run, 1, "changes_requested", "reviewer-model",
            findings=FINDINGS, started_at=now - 25 * HOUR,
            ended_at=now - 25 * HOUR + 4 * MIN)
        store.record_review_round(conn, run, 2, "pass", "reviewer-model",
                                  started_at=now - 24 * HOUR,
                                  ended_at=now - 24 * HOUR + 3 * MIN)
        finish_run(conn, run, "merged", now=now - 23 * HOUR,
                   merge_sha="abc1234def5678901234567890abcdef12345678")
        store.tickets.transition(conn, ticket, "merged")

        ticket, run = claimed(body("Cache the board read"), now - 8 * HOUR)
        store.release(conn, run, "failed", "verify failed: 2 tests red",
                      now=now - 7 * HOUR)
        store.tickets.transition(conn, ticket, "abandoned")

        store.record_supervisor_heartbeat(conn, os.getpid(), now - HOUR,
                                          now=now)
    finally:
        conn.close()


def parse(argv):
    """`(build, command)` from the command line; SystemExit(2) on none."""
    build = None
    if argv[:1] == ["--build"] and len(argv) > 1:
        build, argv = argv[1], argv[2:]
    elif argv[:1] and argv[0].startswith("--build="):
        build, argv = argv[0][len("--build="):], argv[1:]
    if not argv:
        print("usage: python3 -m tests.console_fixture [--build COMMAND]"
              " COMMAND [ARG...]", file=sys.stderr)
        raise SystemExit(2)
    return build, argv


def failed(step):
    print(f"[console_fixture] {step}; the command was not run",
          file=sys.stderr)
    return 1


def start_daemon(env):
    """The host daemon on an ephemeral loopback port: `(process, port)`,
    or `(process, lines)` with the output it gave when it never bound."""
    daemon = subprocess.Popen(
        [sys.executable, "factory.py", "--serve", "127.0.0.1:0"], cwd=REPO,
        env={**env, "PYTHONUNBUFFERED": "1"}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    lines = queue.Queue()

    def drain():
        for line in daemon.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=drain, daemon=True).start()
    said = []
    deadline = time.monotonic() + START_SEC
    while (left := deadline - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=left)
        except queue.Empty:
            break
        if line is None:
            break
        said.append(line)
        match = SERVING.match(line)
        if match:
            return daemon, int(match.group(2))
    return daemon, said


def stop_daemon(daemon):
    if daemon.poll() is None:
        daemon.send_signal(signal.SIGTERM)
        try:
            daemon.wait(STOP_SEC)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


def main(argv=None):
    build, command = parse(sys.argv[1:] if argv is None else argv)
    home = Path(tempfile.mkdtemp(prefix="holophyte-console-"))
    os.environ["HOLOPHYTE_HOME"] = str(home)
    daemon = None
    try:
        if build is not None:
            try:
                code = subprocess.run(shlex.split(build), cwd=REPO).returncode
            except (OSError, ValueError) as bad:
                code = bad
            if code:
                return failed(f"--build {build} failed: {code}")
        try:
            token = seed(home)
        except Exception as bad:  # noqa: BLE001 - any seeding failure is named
            return failed(f"seeding {home} failed: {bad!r}")
        try:
            daemon, bound = start_daemon(dict(os.environ))
        except OSError as bad:
            return failed(f"the host daemon did not start: {bad}")
        if not isinstance(bound, int):
            sys.stderr.write("".join(bound))
            return failed("the host daemon did not start (factory.py --serve"
                          " 127.0.0.1:0)")
        env = {**os.environ, "CONSOLE_URL": f"http://127.0.0.1:{bound}",
               "CONSOLE_TOKEN": token}
        try:
            return subprocess.run(command, cwd=REPO, env=env).returncode
        except OSError as bad:
            print(f"[console_fixture] {command[0]}: {bad}", file=sys.stderr)
            return 127
    finally:
        if daemon is not None:
            stop_daemon(daemon)
        shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
