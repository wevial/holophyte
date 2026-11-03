"""The main watch: a red required check on main's head, one firing per workflow run."""
import store.automations
from holophyte.automation.automation_config import DRY_RUN, MAIN_WATCH
from holophyte.pr import github
from holophyte.pr.check_holds import main_checks

REVISION = 1
SUBJECT = "main"


def failed_required_jobs_by_run(checks):
    if checks["state"] != "failure":
        return {}
    runs = {}
    for check in checks["failed"]:
        if check["required"] and check["workflow_run_id"] is not None:
            runs.setdefault(check["workflow_run_id"], []).append(
                {"name": check["name"], "job_id": check["job_id"]})
    return runs


def observe(target, conn, project, now):
    """The ids of the firings this read reserved."""
    pull = github._origin_pull(target)
    if pull is None:
        return []
    checks = main_checks(target)
    reserved = []
    for run, jobs in failed_required_jobs_by_run(checks).items():
        sha = checks["sha"]
        names = ", ".join(job["name"] for job in jobs)
        found = store.automations.reserve_firing(
            conn, project, MAIN_WATCH, REVISION, SUBJECT,
            f"{pull.owner}/{pull.name}@{sha} run {run}", mode=DRY_RUN,
            reason=(f"would rerun the failed jobs of workflow run {run}"
                    f" ({names}) at {sha}"),
            detail={"sha": sha, "workflow_run_id": run, "jobs": jobs},
            now=now)
        if found is not None:
            reserved.append(found)
    return reserved
