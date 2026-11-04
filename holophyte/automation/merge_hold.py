"""The merge gate while main is red: only a fix that carries the red commit."""
import json
import subprocess

import store
import store.automations
from holophyte.automation.automation_config import (
    ACT,
    MAIN_WATCH,
    main_watch_config,
    unapproved,
)
from holophyte.automation.gate import inhibitor
from holophyte.automation.main_watch import REVISION
from holophyte.automation.policy import report_only, report_only_reason
from holophyte.config.project import Project
from holophyte.host.registry import Host, HostError, home
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github, pr_status


def stopped(watch, pull, conn):
    if watch.mode != ACT or unapproved(watch, REVISION) is not None:
        return True
    try:
        listed = report_only(Host.locate(home()))
    except HostError:
        return True
    return (report_only_reason(pull, listed) is not None
            or inhibitor(home(), conn, MAIN_WATCH) is not None)


def standing_hold(project, conn, run_id, pull):
    if conn is None or run_id is None:
        return None
    found = conn.execute("SELECT projectId FROM runs WHERE id = ?",
                         (run_id,)).fetchone()
    hold = (None if found is None
            else store.automations.open_merge_hold(conn, found[0]))
    if hold is None:
        return None
    try:
        watch = main_watch_config(Project.locate(project.path, adopt=False))
    except (ValueError, SystemExit):
        return None
    return None if stopped(watch, pull, conn) else hold


def _git(wt, *args):
    try:
        return subprocess.run(["git", *args], cwd=wt, capture_output=True,
                              text=True, timeout=github.PR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InfraFailure(f"git {' '.join(args)} timed out after"
                           f" {github.PR_TIMEOUT}s before the merge hold's"
                           " check") from None


def carries(wt, held, sha):
    if _git(wt, "cat-file", "-e", f"{held}^{{commit}}").returncode != 0:
        fetched = _git(wt, "fetch", github.REMOTE, github.BASE)
        if fetched.returncode != 0:
            raise InfraFailure(f"git fetch {github.REMOTE} {github.BASE}"
                               " failed before the merge hold's check:"
                               f" {(fetched.stderr or fetched.stdout).strip()}")
    ancestry = _git(wt, "merge-base", "--is-ancestor", held, sha)
    if ancestry.returncode not in (0, 1):
        raise InfraFailure(f"git merge-base --is-ancestor {held[:12]}"
                           f" {sha[:12]} failed before the merge hold's check:"
                           f" {(ancestry.stderr or ancestry.stdout).strip()}")
    return ancestry.returncode == 0


def passes(project, pull, sha, names):
    runs = pr_status._check_runs_of(project, pull, sha)
    if runs is None:
        raise InfraFailure(f"GitHub answered an unreadable check read at"
                           f" {sha[:12]}; the merge hold's check waits")
    latest = {}
    for run in sorted(runs, key=lambda run: run["id"]
                     if type(run.get("id")) is int else 0):
        latest[run.get("name")] = run
    return all(latest.get(name, {}).get("status") == "completed"
               and latest[name].get("conclusion") == "success"
               for name in names)


def held_merge(project, conn, run_id, wt, sha, pull):
    hold = standing_hold(project, conn, run_id, pull)
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
