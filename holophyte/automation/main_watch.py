"""The main watch: a red required check on main's head, one firing per workflow run."""
import time

import store.automations
from holophyte.automation.automation_config import (
    ACT,
    DRY_RUN,
    MAIN_WATCH,
    unapproved,
)
from holophyte.automation.policy import RERUN_FAILED_JOBS, report_only_reason
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github
from holophyte.pr.check_holds import main_checks

REVISION = 1
SUBJECT = "main"
LEASE_MS = 5 * 60 * 1000


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


def observe(target, conn, project, now, watch, listed):
    pull = github._origin_pull(target)
    if pull is None:
        return []
    held = None
    if watch.mode == ACT:
        held = (unapproved(watch, REVISION)
                or report_only_reason(pull, listed))
    leased = []
    checks = main_checks(target)
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
    return leased
