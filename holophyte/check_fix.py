"""A red check's one fix turn per babysit (KO-640): the brief and the turn.

`holophyte.babysitter` parks a PR whose checks are not green; before it does,
a red head whose failed check runs are all GitHub Actions jobs gets one
implement turn with their log tails, verified, pushed and left for
`_review_fix()` to cover like a thread fix. Before that turn the failed
jobs are rerun once per babysit (KO-707): a flake that goes green costs no
fix turn."""
from dataclasses import dataclass

import store
from holophyte import pr
from holophyte.gates import InfraFailure
from holophyte.pr_head import _just_pushed_state
from holophyte.redact import safe_print as print
from holophyte.runs import heartbeat_while, warn_on_run
from holophyte.stop import stop_if_requested

# How much of a failed check's job log the check fix brief carries.
LOG_TAIL_LINES = 80


@dataclass
class CheckFix:
    """One babysit's spend on red checks: `reran` once their failed jobs
    were rerun, `fixed` once the one fix turn was taken."""

    reran: bool = False
    fixed: bool = False


def rerun_failed_jobs(target, pull, workflow_run_id):
    """Rerun the failed jobs of Actions workflow run `workflow_run_id`."""
    return pr.rest(target, pull, "POST",
                   f"repos/{pull.owner}/{pull.name}/actions/runs"
                   f"/{workflow_run_id}/rerun-failed-jobs")


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
                       pass_no, reviewed, check_fix):
    """`(sha, pushed_state)` after one fix turn for the red Actions checks
    of `state` at `run.sha`. The first time, their workflow runs' failed
    jobs are rerun and the PR settled instead: a state that is no longer
    red comes back as `pushed_state` at the same sha. Parks on the checks
    after this babysit's check fix, or when a red check is not an Actions
    job, whose log there is none to read; a turn that commits nothing
    parks too."""
    from holophyte.babysitter import _fix_threads
    _park_unless_fixable(run, pull, state, reviewed, check_fix)
    stop_if_requested(run.conn, run.run_id, "merge_gate")
    if not check_fix.reran and all(check.workflow_run_id
                                   for check in state.failed_checks):
        check_fix.reran = True
        state = _rerun_and_settle(run, beat_s, pull, state, reviewed)
        if not _red(state, run.sha):
            return run.sha, state
        _park_unless_fixable(run, pull, state, reviewed, check_fix)
    check_fix.fixed = True
    why, failed = _why(state), state.failed_checks
    logs = []
    with heartbeat_while(run.conn, run.run_id, beat_s):
        for check in failed:
            try:
                logs.append(pr.job_log(run.project, pull, check.job_id))
            except InfraFailure:
                logs.append(None)
    print(f"[holo2] checks failed on {pull.url}"
          f" ({', '.join(check.name for check in failed)}); one fix turn")
    sha = _fix_threads(run.project, run.conn, run.run_id, run.provider,
                       run.task_id, run.branch, run.wt, run.sha, beat_s, pull,
                       (), None, ticket, verify_cmd, contracts, run.budget_min,
                       pass_no, review_follows=True,
                       goal=check_fix_brief(pull, failed, logs, ticket),
                       no_commit_why=why, reviewed=reviewed)
    return sha, _just_pushed_state(run.project, run.conn, run.run_id,
                                   run.provider, run.task_id, run.branch, sha,
                                   beat_s, pull, reviewed)


def _why(state):
    return f"checks {state.checks} on the head commit"


def _park_unless_fixable(run, pull, state, reviewed, check_fix):
    """Park on the checks after the fix turn, or when a red check of
    `state` has no job log to hand a fix turn."""
    from holophyte.pullrequest import _park_on_pr
    failed = state.failed_checks
    if (check_fix.fixed or not failed
            or any(check.job_id is None for check in failed)):
        _park_on_pr(run.project, run.conn, run.run_id, run.provider,
                    run.task_id, run.branch, run.sha, pull, _why(state), (),
                    reviewed=reviewed)


def _rerun_and_settle(run, beat_s, pull, state, reviewed):
    """The PR settled after rerunning the failed jobs of each workflow run
    red in `state` once, waiting on the rerun as on any pending check;
    `state` itself when a rerun call fails."""
    from holophyte.babysitter import _settled_or_park
    failed = state.failed_checks
    runs = tuple(dict.fromkeys(check.workflow_run_id for check in failed))
    names = ", ".join(check.name for check in failed)
    listed = ", ".join(str(n) for n in runs)
    print(f"[holo2] checks failed on {pull.url} ({names}); rerunning the"
          f" failed jobs of workflow run(s) {listed}")
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, "check_rerun",
                           f"rerunning the failed jobs of {names} on"
                           f" {pull.url}: workflow run(s) {listed}")
    try:
        with heartbeat_while(run.conn, run.run_id, beat_s):
            for workflow_run_id in runs:
                rerun_failed_jobs(run.project, pull, workflow_run_id)
            # GitHub queues the rerun's jobs a moment after it answers.
            pr.SLEEP(pr.CHECK_POLL_S)
    except InfraFailure as refused:
        warn_on_run(run.conn, run.run_id,
                    f"check rerun of {names} failed: {refused}")
        return state
    return _settled_or_park(run.project, run.conn, run.run_id, beat_s, pull,
                            None, run.provider, run.task_id, run.branch,
                            run.sha, reviewed)


def _red(state, sha):
    """Whether `state` is still only red checks on `sha`, which the fix
    turn answers; threads, a conflict, a closed PR or another head are
    the pass's to handle."""
    return (state.checks == "failure" and not state.threads
            and not state.merged and not state.closed
            and state.mergeable != "CONFLICTING" and state.head_sha == sha)
