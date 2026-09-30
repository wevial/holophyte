import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from time import monotonic
from typing import Any

import store
import store.read
from holophyte.board.projection import block_ticket, ledger
from holophyte.config.config_tables import merge_config
from holophyte.config.project import Project
from holophyte.loop.gates import MergeParked
from holophyte.redact import safe_print as print


@dataclass(frozen=True)
class Run:
    project: Project
    conn: Connection | None
    run_id: int | None
    provider: Any
    task_id: str
    issue_id: str
    task: str
    branch: str
    wt: Path
    budget_min: float
    started: float
    started_at: int | None = None
    sha: str | None = None
    rnd: int = 0
    pr_url: str | None = None
    merge_sha: str | None = None
    clock: Callable[[], float] = monotonic


def land(run: Run, verify: bool):
    from holophyte.loop.merge_gate import _merge
    from holophyte.pr.pullrequest import _landed_pr

    if run.pr_url is not None:
        return _landed_pr(run.conn, run.run_id, run.provider, run.task_id,
                          run.task, run.branch, run.pr_url, run.merge_sha,
                          run.started, run.budget_min, run.rnd)
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, task, branch, wt = run.task_id, run.task, run.branch, run.wt
    sha, started, budget_min, rnd = run.sha, run.started, run.budget_min, run.rnd
    merge_sha = _merge(project, conn, run_id, provider, task_id, task, branch,
                       wt, sha)
    # Still under the merge lock; a failure parks, since the merge has landed.
    _run_after(project, conn, run_id, provider, task_id, merge_sha,
               merge_config(project).after)
    actual_min = (run.clock() - started) / 60
    ledger(conn, run_id, task_id, "merge",
           f"MERGED to main (branch {branch} deleted). "
           f"Verify: {'passed' if verify else 'n/a'}.\n"
           f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
           f"rounds: {rnd}", provider)
    print(f"[holo2] merged: {task}")
    return merge_sha


AFTER_TAIL_LINES = 20


def _run_after(project, conn, run_id, provider, task_id, merge_sha, commands):
    for cmd in commands:
        done = subprocess.run(cmd, shell=True, cwd=project.path,
                              capture_output=True, text=True)
        print(f"[holo2] after: {cmd} -> exit {done.returncode}")
        if done.returncode == 0:
            continue
        tail = "\n".join((done.stdout + done.stderr).splitlines()
                         [-AFTER_TAIL_LINES:])
        why = (f"[merge] after command failed with exit {done.returncode}:"
               f" {cmd}\n{tail}")
        if conn is not None and run_id is not None:
            ticket_id = store.read.run_snapshot(conn, run_id).ticketId
            if not block_ticket(conn, ticket_id, provider, why):
                print(f"[holo2] {task_id} could not be moved to"
                      " blocked_on_operator; parking the run anyway")
            store.park(conn, run_id, "blocked_on_operator", why)
        print(f"[holo2] parked after merge {merge_sha[:12]}: {why}")
        ledger(conn, run_id, task_id, "note",
               f"MERGED to main at {merge_sha}, then {why}\nThe merge stands;"
               " the run waits in blocked_on_operator.", provider)
        raise MergeParked(f"merged at {merge_sha[:12]}; after command failed:"
                          f" {cmd}")
