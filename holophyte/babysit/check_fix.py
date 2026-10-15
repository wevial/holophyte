from dataclasses import dataclass, field, replace
from time import monotonic

import store
from holophyte.babysit.check_failures import LISTED_MAX, Failure, failure_lines
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import heartbeat_while, warn_on_run
from holophyte.loop.stop import stop_if_requested
from holophyte.pr import github, pr_status
from holophyte.pr.pr_head import _just_pushed_state
from holophyte.redact import safe_print as print

LOG_TAIL_LINES = 80


@dataclass
class CheckFix:
    reran: bool = False
    awaited: tuple = ()
    handed: list = field(default_factory=list)


def rerun_failed_jobs(target, pull, workflow_run_id):
    return github.rest(target, pull, "POST",
                   f"repos/{pull.owner}/{pull.name}/actions/runs"
                   f"/{workflow_run_id}/rerun-failed-jobs")


def log_tail(log):
    lines = log.splitlines()
    errors = [n for n, line in enumerate(lines) if "##[error]" in line]
    end = errors[-1] + 1 if errors else len(lines)
    return "\n".join(lines[max(0, end - LOG_TAIL_LINES):end])


def check_fix_brief(pull, failed, logs, ticket, group=None, rounds=(1, 1)):
    listing = "\n\n".join(
        f"CHECK {check.name} -- {check.conclusion}: {check.url}\n"
        + ("log unavailable" if log is None else log_tail(log))
        for check, log in zip(failed, logs))
    failing = "\n\n".join(
        f"{check.name}:\n" + "\n".join(
            f"- {line}" for line in failure_lines(log)[:LISTED_MAX])
        for check, log in zip(failed, logs) if failure_lines(log))
    number, cap = rounds
    return (
        f"Checks on pull request {pull.url} failed on "
        + ("the head commit" if group is None else
           f"the merge group {group} for the pull request, the commit the"
           " merge queue built from main and the pull request")
        + ". The ticket you are held to, acceptance criteria included:\n\n"
        f"{ticket}\n\n"
        + (f"This is check fix round {number} of {cap}: these failures are new"
           " since the commit of the earlier check fix round.\n\n"
           if number > 1 else "")
        + (f"Failing tests and errors, read from each whole job log:\n\n"
           f"{failing}\n\n" if failing else "")
        + f"Failed checks, each with the last {LOG_TAIL_LINES} lines of its"
        f" job log up to its last error:\n\n{listing}\n\n"
        "Fix the failures on this branch and commit; keep the ticket's"
        " verify commands passing. This is one implementer fix turn.")


def fix_checks_or_park(run, beat_s, pull, state, ticket, verify_cmd, contracts,
                       pass_no, reviewed, check_fix, group=None, approval=None):
    from holophyte.babysit.babysitter import _fix_threads
    _park_unless_fixable(run, pull, state, reviewed, group)
    stop_if_requested(run.conn, run.run_id, "merge_gate")
    if group is None and not check_fix.reran and not check_fix.handed and all(
            check.workflow_run_id for check in state.failed_checks):
        check_fix.reran = True
        check_fix.awaited = tuple(dict.fromkeys(
            check.name for check in state.failed_checks))
        pull = replace(pull, awaited=check_fix.awaited)
        state = _rerun_and_settle(run, beat_s, pull, state, reviewed, approval)
        if not _red(state, run.sha):
            return run.sha, state
        _park_unless_fixable(run, pull, state, reviewed)
    why, failed = _why(state, group), state.failed_checks
    logs = _job_logs(run, beat_s, pull, failed)
    failure = Failure.read([check.name for check in failed], logs)
    cap = merge_config(run.project).check_fix_rounds
    number = _next_round(check_fix.handed, failure, cap)
    if isinstance(number, str):
        _park_on_checks(run, pull, f"{why}: {number}", reviewed)
    check_fix.handed.append(failure)
    _record_round(run, pull, failed, failure, (number, cap), group)
    print(f"[holo2] checks failed on {pull.url}"
          f" ({', '.join(check.name for check in failed)}); check fix round"
          f" {number} of {cap}")
    sha = _fix_threads(run.project, run.conn, run.run_id, run.provider,
                       run.task_id, run.branch, run.wt, run.sha, beat_s, pull,
                       (), None, ticket, verify_cmd, contracts, run.budget_min,
                       pass_no, review_follows=True,
                       goal=check_fix_brief(pull, failed, logs, ticket, group,
                                            (number, cap)),
                       no_commit_why=why, reviewed=reviewed,
                       notes=state.notes)
    return sha, _just_pushed_state(run.project, run.conn, run.run_id,
                                   run.provider, run.task_id, run.branch, sha,
                                   beat_s, pull, reviewed)


def _job_logs(run, beat_s, pull, failed):
    logs = []
    with heartbeat_while(run.conn, run.run_id, beat_s):
        for check in failed:
            try:
                logs.append(github.job_log(run.project, pull, check.job_id))
            except InfraFailure:
                logs.append(None)
    return logs


def _next_round(handed, failure, cap):
    repeated = failure.repeated_round(handed)
    if repeated is not None:
        first = (failure.lines or sorted(failure.names))[0]
        return (f"the failure check fix round {repeated} was handed repeats"
                f" ({first}); no further check fix round")
    if len(handed) >= cap:
        return ("a new failure, and the check fix rounds are used up"
                f" ([merge] check_fix_rounds = {cap})")
    return len(handed) + 1


def _record_round(run, pull, failed, failure, rounds, group):
    names = ", ".join(check.name for check in failed)
    where = ("the head commit" if group is None
             else f"the merge group {group[:12]}")
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, "check_fix_round",
                           f"check fix round {rounds[0]} of {rounds[1]} on"
                           f" {pull.url}: {names}; failing: "
                           + ("; ".join(failure.lines[:5]) or "no failure line"
                              " read from the job logs"))
    ledger(run.conn, run.run_id, run.task_id, "round",
           f"Check fix round {rounds[0]} of {rounds[1]} on {pull.url}:"
           f" {names} failed on {where}", run.provider)


def _why(state, group=None):
    if group is None:
        return f"checks {state.checks} on the head commit"
    return (f"the merge queue removed the pull request: checks"
            f" {', '.join(check.name for check in state.failed_checks)}"
            f" failed on the merge group {group[:12]}")


def _park_unless_fixable(run, pull, state, reviewed, group=None):
    failed = state.failed_checks
    if not failed or any(check.job_id is None for check in failed):
        _park_on_checks(run, pull, _why(state, group), reviewed)


def _park_on_checks(run, pull, why, reviewed):
    from holophyte.pr.pullrequest import _park_on_pr
    _park_on_pr(run.project, run.conn, run.run_id, run.provider, run.task_id,
                run.branch, run.sha, pull, why, (), reviewed=reviewed)


def _rerun_and_settle(run, beat_s, pull, state, reviewed, approval=None):
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
    except InfraFailure as refused:
        warn_on_run(run.conn, run.run_id,
                    f"check rerun of {names} failed: {refused}")
        return state
    return _rerun_result(run, beat_s, pull, reviewed, names,
                         {check.job_id for check in failed}, approval)


def _rerun_result(run, beat_s, pull, reviewed, names, rerun_jobs,
                  approval=None):
    """GitHub adds a rerun's jobs late: a red still in `rerun_jobs` predates it."""
    from holophyte.babysit.babysitter import _settled_or_park
    from holophyte.pr.pullrequest import _park_on_pr
    wait = merge_config(run.project).check_wait_sec
    deadline = monotonic() + wait
    while True:
        with heartbeat_while(run.conn, run.run_id, beat_s):
            state = pr_status.pr_state(run.project, pull)
        if not _unreported(state, run.sha, rerun_jobs):
            state = _settled_or_park(run.project, run.conn, run.run_id, beat_s,
                                     pull, state, run.provider, run.task_id,
                                     run.branch, run.sha, reviewed,
                                     deadline=deadline, approval=approval)
            if not _unreported(state, run.sha, rerun_jobs):
                return state
        remaining = deadline - monotonic()
        if remaining <= 0:
            _park_on_pr(run.project, run.conn, run.run_id, run.provider,
                        run.task_id, run.branch, run.sha, pull,
                        f"the rerun of {names} did not report within"
                        f" {wait}s on the pull request", (), reviewed=reviewed)
        stop_if_requested(run.conn, run.run_id, "merge_gate")
        with heartbeat_while(run.conn, run.run_id, beat_s):
            github.SLEEP(min(github.CHECK_POLL_S, remaining))


def _unreported(state, sha, rerun_jobs):
    stale = {check.job_id for check in state.failed_checks} & rerun_jobs
    return _open(state, sha) and bool(
        state.awaiting or (state.checks == "failure" and stale))


def _red(state, sha):
    return state.checks == "failure" and _open(state, sha)


def _open(state, sha):
    return (not state.threads and not state.merged and not state.closed
            and state.mergeable != "CONFLICTING" and state.head_sha == sha)
