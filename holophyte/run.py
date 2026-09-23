"""The immutable identity and candidate state carried by a claimed run."""
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from time import monotonic
from typing import Any

import store
import store.read
from holophyte.board import block_ticket, ledger
from holophyte.config_tables import merge_config
from holophyte.gates import MergeParked
from holophyte.project import Project
from holophyte.redact import safe_print as print


@dataclass(frozen=True)
class Run:
    """Claim identity plus a candidate snapshot; advance with dataclasses.replace.

    `started_at` is the store's wall-clock timestamp; `started` and `clock`
    retain the monotonic elapsed-time calculation, including storeless callers.
    A PR pass returns its merge SHA without moving the local main checkout.
    """
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
    """The merge, the target's `[merge] after` commands and the merged ledger
    line; returns the merge commit's sha. PR runs retain their separate ledger
    format and never run local after-merge commands."""
    from holophyte.merge_gate import _merge
    from holophyte.pullrequest import _landed_pr

    if run.pr_url is not None:
        return _landed_pr(run.conn, run.run_id, run.provider, run.task_id,
                          run.task, run.branch, run.pr_url, run.merge_sha,
                          run.started, run.budget_min, run.rnd)
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, task, branch, wt = run.task_id, run.task, run.branch, run.wt
    sha, started, budget_min, rnd = run.sha, run.started, run.budget_min, run.rnd
    merge_sha = _merge(project, conn, run_id, provider, task_id, task, branch,
                       wt, sha)
    # Still under the merge lock, so the checkout the commands see is the
    # main this merge left and no sibling's merge moves it under them. A
    # failure parks the run rather than failing it: the merge has landed,
    # and a failed run would send the loop back to redo work main holds.
    _run_after(project, conn, run_id, provider, task_id, merge_sha,
               merge_config(project).after)
    # Nothing tells Linear the ticket is done here any more. The merge makes
    # the ticket `merged` in the store, and `main()` projects that status onto
    # the board through `mirror_push()` once the run has been released — one
    # writer of the workflow state instead of a call from the middle of a run
    # that has not finished ending yet.
    # One greppable line of timing data per merged ticket: the estimate stays
    # write-only otherwise, and a future burndown script reads this format.
    actual_min = (run.clock() - started) / 60
    ledger(conn, run_id, task_id, "merge",
           f"MERGED to main (branch {branch} deleted). "
           f"Verify: {'passed' if verify else 'n/a'}.\n"
           f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
           f"rounds: {rnd}", provider)
    # The task's own commit of FINDINGS.md is `main()`'s, not this frame's:
    # the run's close-out entry exists only once the run has been released,
    # which happens after this returns.
    print(f"[holo2] merged: {task}")
    # The merge commit itself, for the close-out to stamp on the run: truthy,
    # so every caller that read this as "did it merge" still does.
    return merge_sha


# How much of a failed `[merge] after` command's output the park's note and
# ledger carry: the last lines, where a build tool says what went wrong.
AFTER_TAIL_LINES = 20


def _run_after(project, conn, run_id, provider, task_id, merge_sha, commands):
    """`[merge] after` (KO-347): run `commands` in order in the main checkout
    once the merge commit exists, each printed with its exit code. The first
    nonzero exit stops the list and parks the run `blocked_on_operator` with
    the command and the tail of its output as the note and the ticket's
    question; `MergeParked` then unwinds the run without marking it merged.
    Nothing here touches the merge commit: main keeps it either way.
    """
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
