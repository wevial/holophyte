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
ticket in each console column, a run working and one in review, a question
park, a pull request parked on its checks and one ready for its merge, a
stranded failure, and three ended runs, one merged since local midnight,
all written through the store's own writers and dated relative to the
seeding moment.
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
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
KEY = "DEMO"
NAME = "demo"
# What the daemon calls this machine: a role, as captures are published.
HOST_LABEL = "writer-host"
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
FINDINGS = [{"path": "holophyte/serve/server.py", "line": 12, "severity": "p1",
             "criterion": "AC1", "message": "the route is unmatched"}]
PULL = "https://github.com/example/demo/pull/"
DEPENDS = "\n## Estimate & dependencies\n\nEstimate: 15 min · Depends on: {}\n"
W1 = dict(key="W1", criterion="A run that fails its verify is retried once",
          file="tests/test_recovery.py",
          command="python3 -m unittest tests.test_recovery",
          source="assert retried")


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
    `project add` registers it, its hostname shown as `HOST_LABEL`; its
    path."""
    import holophyte.cli.entry
    from holophyte.config.project import Project

    path = home / NAME
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    target = Project.locate(path, adopt=False)
    target.holo_dir.mkdir(parents=True, exist_ok=True)
    target.config_path.write_text(
        f'[board]\nkind = "native"\nkey = "{KEY}"\n'
        f'[serve]\nname = "{NAME}"\n'
        f'[report]\nhost_label = "{HOST_LABEL}"\n')
    with open(os.devnull, "w") as quiet, patch.object(sys, "stdout", quiet):
        code = holophyte.cli.entry.cli(["project", "add", str(path)])
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
    the operator, DEMO-5 merged after two rounds with a guardrail kept in the
    ledger, DEMO-6 failed, DEMO-7 in review after changes requested with a
    feature follow-up pending, DEMO-8 parked on pending checks, DEMO-9
    parked ready for its merge, DEMO-10 merged today, DEMO-11 failed and
    still in flight, DEMO-12 a story over six of them; and a supervisor
    beating now."""
    import store
    import store.board
    import store.tickets
    from holophyte.config.project import Project
    from store.follow_ups import record_follow_up, settle_ledger
    from tests.phase_fixture import advance_phase, finish_run
    from tests.test_store_board import body

    now = int(time.time() * 1000)
    conn = store.open(str(Project.locate(path).store_path))
    try:
        project = store.tickets.ensure_project(conn, f"native:{KEY}", path)

        def filed(text, column="ready", at=now - 3 * 24 * HOUR):
            return _ticket_id(conn, store.board.file_ticket(
                conn, project, KEY, text, column=column, now=at))

        def claimed(text, at):
            ticket = filed(text)
            store.tickets.transition(conn, ticket, "in_flight")
            run = store.claim(conn, project, ticket, now=at)
            store.set_phase(conn, run, "working", now=at + MIN)
            return ticket, run

        def parked_on_pr(text, number, kind, question, checks, at):
            ticket, run = claimed(body(text), at)
            advance_phase(conn, run, "merge_gate", now=at + 30 * MIN)
            store.park(conn, run, "awaiting_merge_approval",
                       pr_url=f"{PULL}{number}", park_kind=kind,
                       pr_seen=(now - 5 * MIN, 0, 0, None, checks,
                                "approved", text, "ready"),
                       now=at + 31 * MIN)
            store.tickets.transition(conn, ticket, "blocked_on_operator")
            store.set_question(conn, ticket, f"PR open\n{question}")

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
        guardrail = record_follow_up(
            conn, run, "abc1234def5678901234567890abcdef12345678", "guardrail",
            True, "Fail the export test when a column is renamed",
            "export-columns", path="holophyte/serve/server.py", line=12,
            now=now - 23 * HOUR)
        settle_ledger(conn, guardrail, now=now - 23 * HOUR)
        store.tickets.transition(conn, ticket, "merged")

        ticket, run = claimed(body("Cache the board read"), now - 8 * HOUR)
        store.release(conn, run, "failed", "verify failed: 2 tests red",
                      now=now - 7 * HOUR)
        store.tickets.transition(conn, ticket, "abandoned")

        _, run = claimed(body("Name each worker's route", depends="DEMO-5"),
                         now - 30 * MIN)
        store.record_review_round(
            conn, run, 1, "changes_requested", "reviewer-model",
            findings=FINDINGS, started_at=now - 15 * MIN,
            ended_at=now - 10 * MIN)
        record_follow_up(
            conn, run, "d" * 40, "feature", True,
            "Show the shadow implementer's diff size next to the primary's",
            "shadow-diff-size", path="holophyte/cli/shadow_report.py",
            line=88, now=now - 10 * MIN)
        advance_phase(conn, run, "reviewing", now=now - 5 * MIN)
        store.heartbeat(conn, run, now=now)

        parked_on_pr("Show the merge ledger as a timeline", 41, "ci",
                     "pending checks: unit", "pending", now - 2 * HOUR)
        parked_on_pr("Link each run to its pull request", 42, "pull_request",
                     "Waiting for a maintainer's merge", "success",
                     now - 3 * HOUR)

        midnight = datetime.fromtimestamp(now / SEC).replace(
            hour=0, minute=0, second=0, microsecond=0)
        ended = max(int(midnight.timestamp() * SEC), now - HOUR)
        ticket, run = claimed(body("Sort the runs list by end"),
                              ended - 2 * HOUR)
        store.set_pull_request(conn, run, f"{PULL}43")
        store.record_review_round(conn, run, 1, "pass", "reviewer-model",
                                  started_at=ended - HOUR,
                                  ended_at=ended - HOUR + 4 * MIN)
        finish_run(conn, run, "merged", now=ended,
                   merge_sha="0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c")
        store.tickets.transition(conn, ticket, "merged")

        _, run = claimed(body("Retry the board sync", depends="DEMO-10"),
                         now - 2 * HOUR)
        store.release(conn, run, "failed", "verify failed: 2 tests red",
                      now=now - 50 * MIN)

        _seed_story(conn, project, filed(
            body("Unattended runs recover themselves"), column="backlog"),
            now)

        store.record_supervisor_heartbeat(conn, os.getpid(), now - HOUR,
                                          now=now)
    finally:
        conn.close()


def _ticket_id(conn, identifier):
    (ticket,) = conn.execute(
        "SELECT id FROM tickets WHERE linearIdentifier = ?",
        (identifier,)).fetchone()
    return ticket


def _seed_story(conn, project, parent, now):
    """DEMO-1 made to depend on DEMO-7 and DEMO-11, then `parent`'s story
    over DEMO-5, DEMO-10, DEMO-7, DEMO-11, DEMO-2 and DEMO-1: approved, its
    witness W1 recorded green and one decision answered `replan`."""
    import store.board
    from store.stories import (
        answer_decision,
        approve_story,
        file_story,
        park_story,
        record_witness_result,
    )

    (revision,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                               (_ticket_id(conn, f"{KEY}-1"),)).fetchone()
    store.board.edit_ticket(conn, project, f"{KEY}-1",
                            DRAFT + DEPENDS.format("DEMO-7, DEMO-11"),
                            revision, now=now - 20 * MIN)
    roles = {"DEMO-5": "advances", "DEMO-10": "advances",
             "DEMO-7": "advances", "DEMO-11": "advances",
             "DEMO-2": "scaffolding", "DEMO-1": "completes"}
    file_story(conn, parent, [W1], [
        (_ticket_id(conn, name), role, () if role == "scaffolding" else ("W1",))
        for name, role in roles.items()], now=now - 15 * MIN)
    (revision,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                               (parent,)).fetchone()
    approve_story(conn, parent, revision, "maintainer", "Plan approved",
                  now=now - 14 * MIN)
    record_witness_result(conn, parent, "W1", "1" * 40, "green", "loop",
                          now=now - 12 * MIN)
    decision = park_story(conn, parent, "unmet", "DEMO-11 failed its verify;"
                          " replan around it or abandon the story?",
                          ["replan", "abandon"], "replan",
                          ticket_id=_ticket_id(conn, "DEMO-11"),
                          now=now - 10 * MIN)
    answer_decision(conn, decision, "replan", "maintainer",
                    "Replan around the board sync", now=now - 8 * MIN)


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
