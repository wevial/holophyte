"""Main's check runs, and the red checks holding an open pull request."""
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github, pr_status


def _reads(target, pull, ref):
    """`_check_reads()` without its suppression, so the error is kept."""
    runs = pr_status._check_runs_of(target, pull, ref)
    required = pr_status._required_contexts(pr_status.main_rules(target, pull))
    if runs is None or required is None:
        raise InfraFailure(f"GitHub answered an unreadable check read at {ref}")
    return runs, required + pr_status._protected_contexts(target, pull)


def _failed(runs, required):
    return [{"name": check.name, "required": check.name in required}
            for check in pr_status._failed_checks(runs)]


def _head(target, pull):
    answer = pr_status.rest(target, pull, "GET",
                            f"repos/{pull.owner}/{pull.name}/commits/main")
    sha = answer.get("sha") if isinstance(answer, dict) else None
    if not isinstance(sha, str) or not sha:
        raise InfraFailure("GitHub answered main's head without its sha")
    return sha


def _failed_jobs(runs, required):
    return [{"name": check.name, "required": check.name in required,
             "job_id": check.job_id, "workflow_run_id": check.workflow_run_id}
            for check in pr_status._failed_checks(runs)]


def newest_runs(runs):
    ordered = sorted((run for run in runs if type(run.get("id")) is int),
                     key=lambda run: run["id"])
    return {run.get("name"): run for run in ordered}


def _required_green(runs, required):
    newest = newest_runs(runs)
    return bool(required) and all(
        newest.get(name, {}).get("conclusion") == "success"
        for name in required)


def main_reading(target):
    pull = github._origin_pull(target)
    if pull is None:
        return {"state": "skipped", "detail": "no GitHub origin"}, False
    try:
        sha = _head(target, pull)
        runs, required = _reads(target, pull, sha)
    except InfraFailure as bad:
        return {"state": "unknown", "detail": str(bad)}, False
    green = _required_green(runs, required)
    failed = _failed_jobs(runs, required)
    if failed:
        return {"state": "failure", "sha": sha, "failed": failed}, green
    if not runs:
        return {"state": "none", "sha": sha}, green
    if any(run.get("status") != "completed" for run in runs):
        return {"state": "pending", "sha": sha}, green
    return {"state": "success", "sha": sha}, green or not required


def main_checks(target):
    return main_reading(target)[0]


def pull_checks(target, pull):
    answer = pr_status.rest(
        target, pull, "GET",
        f"repos/{pull.owner}/{pull.name}/pulls/{pull.number}")
    state = answer.get("state") if isinstance(answer, dict) else None
    head = answer.get("head") if isinstance(answer, dict) else None
    sha = head.get("sha") if isinstance(head, dict) else None
    if state == "closed":
        return []
    if state != "open":
        raise InfraFailure(f"GitHub answered {pull.url} without its state")
    if not isinstance(sha, str) or not sha:
        raise InfraFailure(f"GitHub answered {pull.url} without its head")
    runs, required = _reads(target, pull, sha)
    failed = _failed(runs, required)
    if any(check["required"] or not required for check in failed):
        return failed
    return []
