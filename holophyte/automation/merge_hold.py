"""The merge gate while main is red: only a fix that carries the red commit."""
import json
import subprocess

import store
import store.automations
from holophyte.automation.automation_config import (
    ACT,
    MAIN_WATCH,
    main_watch_config,
)
from holophyte.automation.gate import inhibitor
from holophyte.host.registry import home
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github, pr_status


def standing_hold(project, conn, run_id):
    if conn is None or run_id is None:
        return None
    found = conn.execute("SELECT projectId FROM runs WHERE id = ?",
                         (run_id,)).fetchone()
    hold = (None if found is None
            else store.automations.open_merge_hold(conn, found[0]))
    if hold is None:
        return None
    try:
        watch = main_watch_config(project)
    except ValueError:
        return None
    if watch.mode != ACT or inhibitor(home(), conn, MAIN_WATCH) is not None:
        return None
    return hold


def _git(wt, *args):
    return subprocess.run(["git", *args], cwd=wt, capture_output=True,
                          text=True, timeout=github.PR_TIMEOUT)


def carries(wt, held, sha):
    if _git(wt, "cat-file", "-e", f"{held}^{{commit}}").returncode != 0:
        fetched = _git(wt, "fetch", github.REMOTE, github.BASE)
        if fetched.returncode != 0:
            raise InfraFailure(f"git fetch {github.REMOTE} {github.BASE}"
                               " failed before the merge hold's check:"
                               f" {(fetched.stderr or fetched.stdout).strip()}")
    return _git(wt, "merge-base", "--is-ancestor", held, sha).returncode == 0


def passes(project, pull, sha, names):
    runs = pr_status._check_runs_of(project, pull, sha)
    if runs is None:
        raise InfraFailure(f"GitHub answered an unreadable check read at"
                           f" {sha[:12]}; the merge hold's check waits")
    passed = {run.get("name") for run in runs
              if run.get("status") == "completed"
              and run.get("conclusion") == "success"}
    return set(names) <= passed


def held_merge(project, conn, run_id, wt, sha, pull):
    hold = standing_hold(project, conn, run_id)
    if hold is None:
        return None
    payload = json.dumps({"hold": hold.id})
    if carries(wt, hold.sha, sha) and passes(project, pull, sha, hold.checks):
        store.record_event(conn, run_id, "main_hold_passed",
                           f"the candidate at {sha[:12]} carries main's red"
                           f" commit {hold.sha[:12]} and passes"
                           f" {', '.join(hold.checks)}", level="detail",
                           payload=payload)
        return None
    store.record_event(conn, run_id, "main_hold_parked",
                       f"the candidate at {sha[:12]} waits on the merge hold"
                       f" at {hold.sha[:12]}", level="detail", payload=payload)
    return (f"main is red at {hold.sha[:12]} ({', '.join(hold.checks)});"
            " merges wait for it to go green")
