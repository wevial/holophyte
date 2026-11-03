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


def main_checks(target):
    pull = github._origin_pull(target)
    if pull is None:
        return {"state": "skipped", "detail": "no GitHub origin"}
    try:
        sha = _head(target, pull)
        runs, required = _reads(target, pull, sha)
    except InfraFailure as bad:
        return {"state": "unknown", "detail": str(bad)}
    failed = _failed_jobs(runs, required)
    if failed:
        return {"state": "failure", "sha": sha, "failed": failed}
    if not runs:
        return {"state": "none", "sha": sha}
    if any(run.get("status") != "completed" for run in runs):
        return {"state": "pending", "sha": sha}
    return {"state": "success", "sha": sha}


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
