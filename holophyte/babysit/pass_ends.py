"""Where a babysit pass ends besides its loop: a merge, a person's merge, a cap."""
from dataclasses import replace

from holophyte.babysit.check_fix import fix_checks_or_park
from holophyte.config.config_tables import merge_config
from holophyte.loop.runs import heartbeat_while, set_phase
from holophyte.pr import merge_queue, pr_status
from holophyte.pr.missing_checks import Retrigger
from holophyte.pr.pr_head import _pr_terminal


def _at_the_cap(run, beat_s, pull, pushed_state, reviewed, refresh, refreshed):
    from holophyte.babysit.babysitter import _settled_or_park
    from holophyte.pr.pullrequest import _park_on_pr
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, branch, merge = run.task_id, run.branch, merge_config(run.project)
    retrigger = Retrigger(run, beat_s, pull, run.sha, reviewed)
    state = _settled_or_park(
        project, conn, run_id, beat_s, pull, pushed_state, provider,
        task_id, branch, run.sha, reviewed, refresh, retrigger)
    sha, reviewed = retrigger.sha, retrigger.reviewed
    done = _pr_terminal(project, conn, run_id, provider, task_id, branch, sha,
                        pull, state, reviewed)
    if done is not None:
        return replace(run, sha=sha, merge_sha=done)
    cap = (f"pr_main_refreshes = {merge.pr_main_refreshes} main refreshes made"
           if refreshed else f"pr_rounds = {merge.pr_rounds} passes made")
    _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                f"[merge] {cap}; the babysitter stops here", state.threads,
                reviewed=reviewed)


def _merge_or_fix_checks(run, beat_s, pull, state, reviewed, verified, ticket,
                         verify_cmd, contracts, pass_no, check_fix):
    try:
        return merge_queue.verified_merge(
            run.project, run.conn, run.run_id, run.provider, run.task_id,
            run.issue_id, run.branch, run.wt, run.sha, beat_s, pull, reviewed,
            verified, verify_cmd, contracts, ticket, run.budget_min,
            merge_config(run.project).approve == "auto"), run.sha, None
    except merge_queue.QueueRemoved as removed:
        set_phase(run.conn, run.run_id, "merge_gate", "the merge queue removed"
                  " the pull request; fixing its checks")
        return (None, *fix_checks_or_park(
            run, beat_s, pull, replace(
                state, checks="failure", failed_checks=removed.failed),
            ticket, verify_cmd, contracts, pass_no, reviewed,
            check_fix, removed.group))


def _merged_meanwhile(run, beat_s, pull, reviewed):
    with heartbeat_while(run.conn, run.run_id, beat_s):
        state = pr_status.pr_state(run.project, pull)
    return _pr_terminal(run.project, run.conn, run.run_id, run.provider,
                        run.task_id, run.branch, run.sha, pull, state, reviewed)
