"""A red check's one fix turn per babysit (KO-640): the brief and the turn.

`holophyte.babysitter` parks a PR whose checks are not green; before it does,
a red head whose failed check runs are all GitHub Actions jobs gets one
implement turn with their log tails, verified, pushed and left for
`_review_fix()` to cover like a thread fix."""
from holophyte import pr
from holophyte.gates import InfraFailure
from holophyte.pr_head import _just_pushed_state
from holophyte.redact import safe_print as print
from holophyte.runs import heartbeat_while
from holophyte.stop import stop_if_requested

# How much of a failed check's job log the check fix brief carries.
LOG_TAIL_LINES = 80


def check_fix_brief(pull, failed, logs, ticket):
    """Fix goal for red checks: each `FailedCheck` of `failed` with its
    conclusion, link and the tail of its job log (`logs`, in the same
    order; None for a log that could not be read)."""
    listing = "\n\n".join(
        f"CHECK {check.name} -- {check.conclusion}: {check.url}\n"
        + ("log unavailable" if log is None
           else "\n".join(log.splitlines()[-LOG_TAIL_LINES:]))
        for check, log in zip(failed, logs))
    return (
        f"Checks on pull request {pull.url} failed on the head commit. The"
        " ticket you are held to, acceptance criteria included:\n\n"
        f"{ticket}\n\nFailed checks, each with the last {LOG_TAIL_LINES}"
        f" lines of its job log:\n\n{listing}\n\n"
        "Fix the failures on this branch and commit; keep the ticket's"
        " verify commands passing. This is one implementer fix turn.")


def fix_checks_or_park(run, beat_s, pull, state, ticket, verify_cmd, contracts,
                       pass_no, reviewed, check_fixed):
    """`(sha, pushed_state)` after one fix turn for the red Actions checks
    of `state` at `run.sha`. Parks on the checks instead after this
    babysit's check fix, or when a red check is not an Actions job, whose
    log there is none to read; a turn that commits nothing parks too."""
    from holophyte.babysitter import _fix_threads
    from holophyte.pullrequest import _park_on_pr
    why = f"checks {state.checks} on the head commit"
    failed = state.failed_checks
    if (check_fixed or not failed
            or any(check.job_id is None for check in failed)):
        _park_on_pr(run.target, run.conn, run.run_id, run.provider,
                    run.task_id, run.branch, run.sha, pull, why, (),
                    reviewed=reviewed)
    stop_if_requested(run.conn, run.run_id, "merge_gate")
    logs = []
    with heartbeat_while(run.conn, run.run_id, beat_s):
        for check in failed:
            try:
                logs.append(pr.job_log(run.target, pull, check.job_id))
            except InfraFailure:
                logs.append(None)
    print(f"[holo2] checks failed on {pull.url}"
          f" ({', '.join(check.name for check in failed)}); one fix turn")
    sha = _fix_threads(run.target, run.conn, run.run_id, run.provider,
                       run.task_id, run.branch, run.wt, run.sha, beat_s, pull,
                       (), None, ticket, verify_cmd, contracts, run.budget_min,
                       pass_no, review_follows=True,
                       goal=check_fix_brief(pull, failed, logs, ticket),
                       no_commit_why=why, reviewed=reviewed)
    return sha, _just_pushed_state(run.target, run.conn, run.run_id,
                                   run.provider, run.task_id, run.branch, sha,
                                   beat_s, pull, reviewed)
