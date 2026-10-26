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


def main_checks(target):
    pull = github._origin_pull(target)
    if pull is None:
        return {"state": "skipped", "detail": "no GitHub origin"}
    try:
        runs, required = _reads(target, pull, "main")
    except InfraFailure as bad:
        return {"state": "unknown", "detail": str(bad)}
    failed = _failed(runs, required)
    if failed:
        return {"state": "failure", "failed": failed}
    if not runs:
        return {"state": "none"}
    if any(run.get("status") != "completed" for run in runs):
        return {"state": "pending"}
    return {"state": "success"}


def pull_checks(target, pull):
    try:
        answer = pr_status.rest(
            target, pull, "GET",
            f"repos/{pull.owner}/{pull.name}/pulls/{pull.number}")
        state = answer.get("state") if isinstance(answer, dict) else None
        head = answer.get("head") if isinstance(answer, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        if state != "open":
            return {"state": state, "held": False, "failed": []}
        if not isinstance(sha, str) or not sha:
            raise InfraFailure(f"GitHub answered {pull.url} without a head")
        runs, required = _reads(target, pull, sha)
    except InfraFailure as bad:
        return {"state": "unknown", "held": False, "failed": [],
                "detail": str(bad)}
    failed = _failed(runs, required)
    blocking = [check for check in failed
                if check["required"] or not required]
    return {"state": state, "head": sha, "held": bool(blocking),
            "failed": failed}
