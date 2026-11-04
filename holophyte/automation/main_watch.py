"""The main watch: a red required check on main's head, one firing per workflow run."""
import time

import store
import store.automations
from holophyte.automation.automation_config import (
    ACT,
    DRY_RUN,
    MAIN_WATCH,
    unapproved,
)
from holophyte.automation.gate import inhibitor
from holophyte.automation.policy import (
    HOLD_MERGES,
    RELEASE_HELD_MERGES,
    RERUN_FAILED_JOBS,
    refusal,
    report_only_reason,
)
from holophyte.host.registry import home
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github, pr_status
from holophyte.pr.check_holds import newest_runs

REVISION = 1
SUBJECT = "main"
HOLD_SUBJECT = "merges"
LEASE_MS = 5 * 60 * 1000
GREEN, RED_AGAIN, SUPERSEDED = "green", "red_again", "superseded"
COMPARE_COMMITS = 20


def failed_jobs_of_required_runs(checks):
    if checks["state"] != "failure":
        return {}
    eligible = {check["workflow_run_id"] for check in checks["failed"]
                if check["required"] and check["workflow_run_id"] is not None}
    runs = {}
    for check in checks["failed"]:
        if check["workflow_run_id"] in eligible:
            runs.setdefault(check["workflow_run_id"], []).append(
                {"name": check["name"], "job_id": check["job_id"]})
    return runs


def workflow_run_attempt(target, pull, run):
    answer = github.rest(target, pull, "GET",
                         f"repos/{pull.owner}/{pull.name}/actions/runs/{run}")
    attempt = answer.get("run_attempt") if isinstance(answer, dict) else None
    if type(attempt) is not int:
        raise InfraFailure(f"workflow run {run} answered no"
                           f" run_attempt: {attempt!r}")
    return attempt


def rerun_reading(firing, runs):
    jobs = firing.detail["jobs"]
    recorded = {job["job_id"] for job in jobs}
    newest = newest_runs(runs)
    answered = [(job["name"], newest.get(job["name"])) for job in jobs]
    red = [name for name, run in answered if run is not None
           and run["id"] not in recorded and run.get("status") == "completed"
           and run.get("conclusion") in pr_status.RED_CONCLUSIONS]
    if red:
        return RED_AGAIN, red
    if all(run is not None and run.get("conclusion") == "success"
           for _name, run in answered):
        return GREEN, [name for name, _run in answered]
    return None, []


def subject(commit):
    message = commit.get("commit", {}).get("message") or ""
    return message.partition("\n")[0]


def merged_since(target, pull, green, sha):
    try:
        answer = github.rest(
            target, pull, "GET", f"repos/{pull.owner}/{pull.name}/compare/"
            f"{green}...{sha}?per_page={COMPARE_COMMITS}")
    except InfraFailure as bad:
        return f"unreadable ({bad})"
    commits = answer.get("commits") if isinstance(answer, dict) else None
    if not isinstance(commits, list):
        return f"unreadable (the compare answered no commits: {answer!r})"
    return ", ".join(f"{str(commit.get('sha'))[:7]} {subject(commit)}"
                     for commit in commits[:COMPARE_COMMITS])


def result_of(target, pull, firing, head, green):
    sha = firing.detail["sha"]
    if head != sha:
        return SUPERSEDED, (f"main moved to {head[:12]} before the rerun at"
                            f" {sha[:12]} was read")
    runs = pr_status._check_runs_of(target, pull, sha)
    if runs is None:
        raise InfraFailure(f"GitHub answered an unreadable check read at {sha}")
    result, names = rerun_reading(firing, runs)
    named = ", ".join(names)
    if result == GREEN:
        return GREEN, f"suspected flake: {named} passed on rerun at {sha[:12]}"
    if result == RED_AGAIN:
        since = ("no green main seen yet" if green is None else
                 f"merged since {green[:12]}:"
                 f" {merged_since(target, pull, green, sha)}")
        return RED_AGAIN, f"red again at {sha[:12]}: {named}; {since}"
    return None


def observe(target, conn, project, pull, checks, now, watch, listed, leased):
    held = None
    if watch.mode == ACT:
        held = (unapproved(watch, REVISION)
                or report_only_reason(pull, listed))
    hold_merges(conn, project, now, watch, held, pull, checks)
    for run, jobs in failed_jobs_of_required_runs(checks).items():
        sha = checks["sha"]
        names = ", ".join(job["name"] for job in jobs)
        jobs_at = f"the failed jobs of workflow run {run} ({names}) at {sha}"
        detail = {"sha": sha, "workflow_run_id": run, "jobs": jobs}
        cause = f"{pull.owner}/{pull.name}@{sha} run {run}"
        if watch.mode != ACT or held is not None:
            store.automations.reserve_firing(
                conn, project, MAIN_WATCH, REVISION, SUBJECT, cause,
                mode=DRY_RUN, reason=f"would rerun {jobs_at}" if held is None
                else f"would rerun {jobs_at}; dry run: {held}", detail=detail,
                now=now)
            continue
        detail.update(action=RERUN_FAILED_JOBS,
                      run_attempt=workflow_run_attempt(target, pull, run))
        firing = store.automations.reserve_firing(
            conn, project, MAIN_WATCH, REVISION, SUBJECT, cause, mode=ACT,
            reason=f"rerunning {jobs_at}",
            detail=detail, now=now,
            lease_until=int(time.time() * 1000) + LEASE_MS)
        if firing is not None:
            leased.append(firing)


def hold_merges(conn, project, now, watch, held, pull, checks):
    if checks["state"] != "failure":
        return
    names = list(dict.fromkeys(check["name"] for check in checks["failed"]
                               if check["required"]))
    if not names:
        return
    sha = checks["sha"]
    red = f"main is red at {sha[:12]} ({', '.join(names)})"
    cause = f"{pull.owner}/{pull.name}@{sha}"
    detail = {"action": HOLD_MERGES, "sha": sha, "checks": names}
    if watch.mode == ACT:
        held = held or refusal(MAIN_WATCH, HOLD_MERGES)
    if watch.mode != ACT or held is not None:
        store.automations.reserve_firing(
            conn, project, MAIN_WATCH, REVISION, HOLD_SUBJECT, cause,
            mode=DRY_RUN, reason=f"would hold merges: {red}" if held is None
            else f"would hold merges: {red}; dry run: {held}", detail=detail,
            now=now)
        return
    with store.transaction(conn):
        standing = store.automations.open_merge_hold(conn, project)
        if (standing is not None and standing.sha == sha
                and sorted(standing.checks) == sorted(names)):
            return
        firing = store.automations.reserve_firing(
            conn, project, MAIN_WATCH, REVISION, HOLD_SUBJECT, cause,
            mode=ACT, reason=f"holding merges: {red}", detail=detail, now=now,
            lease_until=int(time.time() * 1000) + LEASE_MS)
        if firing is None:
            firing = store.automations.keyed_firing(
                conn, project, MAIN_WATCH, REVISION, HOLD_SUBJECT, cause)
        store.automations.place_merge_hold(
            conn, project, firing, sha, names, f"held merges: {red}", now=now)


def left_reason(run):
    if run.lastRunId != run.runId:
        return (f"its newest run is {run.lastRunId}, not run {run.runId}"
                " the hold parked")
    if run.touched:
        return f"a person acted on run {run.runId} after the hold parked it"
    return None


def release_held_merges(conn, project, pull, sha, now, watch, listed):
    if watch.mode != ACT or (unapproved(watch, REVISION)
                             or report_only_reason(pull, listed)
                             or refusal(MAIN_WATCH, RELEASE_HELD_MERGES)):
        return None
    with store.transaction(conn):
        hold = store.automations.open_merge_hold(conn, project)
        if hold is None or inhibitor(home(), conn, MAIN_WATCH) is not None:
            return None
        detail = {"action": RELEASE_HELD_MERGES, "sha": sha, "hold": hold.id}
        firing = store.automations.reserve_firing(
            conn, project, MAIN_WATCH, REVISION, HOLD_SUBJECT,
            f"{pull.owner}/{pull.name}@{sha} hold {hold.id}", mode=ACT,
            reason=f"releasing the merge hold at {hold.sha[:12]}",
            detail=detail, now=now,
            lease_until=int(time.time() * 1000) + LEASE_MS)
        if firing is None:
            return None
        store.automations.end_merge_hold(conn, hold.id,
                                         f"main green at {sha[:12]}", now=now)
        note = (f"main is green at {sha[:12]}; main_watch firing {firing}"
                " released the hold")
        newest = {run.ticketId: run
                  for run in store.automations.held_runs(conn, hold.id)}
        released, left = [], []
        for run in newest.values():
            reason = left_reason(run)
            if reason is None:
                try:
                    store.babysit(conn, run.ticketId, note, now=now,
                                  source="automation", firing_id=firing)
                except store.ApproveRefused as refused:
                    reason = str(refused)
            if reason is None:
                released.append(run.identifier)
            else:
                left.append({"ticket": run.identifier, "reason": reason})
        store.automations.settle_firing(
            conn, firing, None, store.automations.CONFIRMED,
            f"released the merge hold at {hold.sha[:12]}: main is green at"
            f" {sha[:12]}, {len(released)} runs sent back to the babysitter",
            now=now, detail=detail | {"released": released, "left": left})
    return firing
