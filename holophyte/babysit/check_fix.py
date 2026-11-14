import json
import shlex
from dataclasses import dataclass, field, replace
from pathlib import PurePosixPath
from time import monotonic

import store
from holophyte.babysit import maintainer_notes
from holophyte.babysit.check_failures import (
    LISTED_MAX,
    Failure,
    failure_lines,
    unittest_ids,
)
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.files import git
from holophyte.loop.gates import InfraFailure, run_verify, verify_timed_out
from holophyte.loop.runs import heartbeat_while, warn_on_run
from holophyte.loop.stop import stop_if_requested
from holophyte.pr import github, pr_status
from holophyte.pr.pr_head import _just_pushed_state
from holophyte.redact import safe_print as print

LOG_TAIL_LINES = 80
LOCAL_RUN_SEC = 300
LOCAL_TAIL_LINES = 40


@dataclass
class CheckFix:
    reran: bool = False
    awaited: tuple = ()
    handed: list = field(default_factory=list)
    noted: set = field(default_factory=set)

    def note_optional(self, conn, run_id, state):
        if not state.optional_failures or state.head_sha in self.noted:
            return
        self.noted.add(state.head_sha)
        if conn is not None and run_id is not None:
            store.record_event(
                conn, run_id, "optional_checks_failed",
                f"optional checks failed on {state.head_sha[:12]}, not"
                " blocking the merge: " + ", ".join(
                    check.name for check in state.optional_failures))


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
    logs = None
    if group is None and not check_fix.reran and not check_fix.handed and all(
            check.workflow_run_id for check in state.failed_checks):
        pull, state, logs = _rerun_unless_reproduced(
            run, beat_s, pull, state, reviewed, check_fix, approval)
        if logs is None and not _red(state, run.sha):
            return run.sha, state
        if logs is None:
            _park_unless_fixable(run, pull, state, reviewed)
    why, failed = _why(state, group), state.failed_checks
    if logs is None:
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


def _rerun_unless_reproduced(run, beat_s, pull, state, reviewed, check_fix,
                             approval):
    check_fix.reran = True
    failed = state.failed_checks
    logs = _job_logs(run, beat_s, pull, failed)
    named = list(dict.fromkeys(
        found for log in logs for found in unittest_ids(log)))
    reproduced, reason, out = _local_run(run, beat_s, named)
    if reproduced:
        _record_skip(run, pull, failed, named, reproduced, out)
        return pull, state, logs
    check_fix.awaited = tuple(dict.fromkeys(check.name for check in failed))
    pull = replace(pull, awaited=check_fix.awaited)
    return pull, _rerun_and_settle(run, beat_s, pull, state, reviewed,
                                   approval, reason), None


def _local_run(run, beat_s, named):
    if not named:
        return [], "no unittest test id in the failed job logs", ""
    head = git(run.wt, "rev-parse", "HEAD")[1].strip()
    if head != run.sha:
        return [], (f"the worktree is at {head[:12] or 'no commit'}, not the"
                    f" pull request head {run.sha[:12]}"), ""
    files = _module_files(run.wt, dict.fromkeys(module for module, _ in named))
    if isinstance(files, str):
        return [], files, ""
    command = " && ".join(
        "python3 -m unittest discover -s "
        f"{shlex.quote(str(path.parent))} -p {shlex.quote(path.name)}"
        + "".join(f" -k {shlex.quote(name)}"
                  for module, name in named if files[module] == path)
        for path in dict.fromkeys(files.values()))
    with heartbeat_while(run.conn, run.run_id, beat_s):
        ok, out = run_verify(command, run.wt, timeout=LOCAL_RUN_SEC,
                             conn=run.conn, run_id=run.run_id,
                             project=run.project, rerun=False)
    if ok:
        return [], f"passed at {run.sha[:12]}", out
    if verify_timed_out(out):
        return [], f"timed out after {LOCAL_RUN_SEC:g} s", out
    local = {(module.rsplit(".", 1)[-1], name)
             for module, name in unittest_ids(out)}
    reproduced = [(module, name) for module, name in named
                  if (files[module].stem, name) in local]
    return reproduced, ("" if reproduced else
                        "the local run failed without reproducing a named"
                        " test"), out


def _module_files(wt, modules):
    tracked = [PurePosixPath(path)
               for path in git(wt, "ls-files")[1].splitlines()]
    files = {}
    for module in modules:
        dotted = PurePosixPath(module.replace(".", "/") + ".py")
        found = [dotted] if dotted in tracked else [
            path for path in tracked
            if path.name == f"{module.rsplit('.', 1)[-1]}.py"]
        if len(found) != 1:
            return (f"module {module} not found in the worktree" if not found
                    else f"module {module} found {len(found)} times in the"
                    " worktree")
        files[module] = found[0]
    return files


def _record_skip(run, pull, failed, named, reproduced, out):
    names = ", ".join(check.name for check in failed)
    ids = ", ".join(f"{module}.{name}" for module, name in reproduced)
    print(f"[holo2] checks failed on {pull.url} ({names}); {ids} failed"
          f" locally at {run.sha[:12]}, so no rerun")
    if run.conn is not None and run.run_id is not None:
        tail = "\n".join(str(out).splitlines()[-LOCAL_TAIL_LINES:])
        store.record_event(
            run.conn, run.run_id, "check_rerun_skipped",
            f"not rerunning {names} on {pull.url}: {ids} failed locally at"
            f" {run.sha[:12]}", level="detail", payload=json.dumps({
                "ids": [f"{module}.{name}" for module, name in named],
                "reproduced": [f"{module}.{name}"
                               for module, name in reproduced],
                "output": tail}))


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


def _rerun_and_settle(run, beat_s, pull, state, reviewed, approval, local):
    failed = state.failed_checks
    runs = tuple(dict.fromkeys(check.workflow_run_id for check in failed))
    names = ", ".join(check.name for check in failed)
    listed = ", ".join(str(n) for n in runs)
    print(f"[holo2] checks failed on {pull.url} ({names}); rerunning the"
          f" failed jobs of workflow run(s) {listed}")
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, "check_rerun",
                           f"rerunning the failed jobs of {names} on"
                           f" {pull.url}: workflow run(s) {listed};"
                           f" local run: {local}")
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


def reads_green(project, conn, run_id, beat_s, pull, sha):
    try:
        with heartbeat_while(conn, run_id, beat_s):
            state = pr_status.pr_state(project, pull)
    except InfraFailure:
        return False
    state = maintainer_notes.pending_state(conn, run_id, state, pull.url)
    return _open(state, sha) and state.checks == "success"


def _red(state, sha):
    return state.checks == "failure" and _open(state, sha)


def _open(state, sha):
    return (not state.threads and not state.merged and not state.closed
            and state.mergeable != "CONFLICTING" and state.head_sha == sha)
