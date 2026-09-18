"""Guard the babysitter's candidate against a foreign pull request head."""
import store
import store.read
from holophyte import pr, pr_status
from holophyte.runs import heartbeat_while


def _just_pushed_state(target, conn, run_id, provider, task_id, branch,
                       sha, beat_s, pull, reviewed):
    """Let the PR view catch up with our push before calling its head foreign."""
    with heartbeat_while(conn, run_id, beat_s):
        state = pr_status.pr_state(target, pull)
        reads = 1
        while (state.head_sha != sha and not state.merged and not state.closed
               and reads < 4):
            pr.SLEEP(5)
            state = pr_status.pr_state(target, pull)
            reads += 1
    if (reads > 1 and state.head_sha == sha
            and conn is not None and run_id is not None):
        store.record_event(conn, run_id, "pull_request",
                           f"pull request head settled to {sha} after"
                           f" {reads} reads")
    if state.head_sha != sha:
        _pr_terminal(target, conn, run_id, provider, task_id, branch, sha,
                     pull, state, reviewed, head_reads=reads)
    return state


def _pr_terminal(target, conn, run_id, provider, task_id, branch, sha,
                 pull, state, reviewed, head_reads=None):
    """Handle a terminal PR or park a head that differs from the candidate."""
    from holophyte.pullrequest import _park_on_pr
    if state.merged:
        print(f"[holo2] {pull.url} is already merged as"
              f" {(state.merge_sha or '?')[:12]}")
        return state.merge_sha
    if state.closed:
        from holophyte.board import release_lease_label
        from holophyte.gates import MergeParked
        from holophyte.reconcile import _reject_pr
        if conn is not None and run_id is not None:
            _reject_pr(conn, run_id, pull, state.closed_by, branch, sha)
            ticket_id = store.read.run_snapshot(conn, run_id).ticketId
            release_lease_label(target, conn, ticket_id, provider, run_id)
        raise MergeParked(f"rejected: {pull.url} closed by"
                          f" {state.closed_by or 'unknown'}")
    if state.head_sha and state.head_sha != sha:
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha,
                    pull,
                    f"the pull request's head is {state.head_sha[:12]},"
                    f" not the candidate {sha[:12]} this run pushed;"
                    " someone else pushed to the branch, and the"
                    " babysitter does not judge or merge their commit"
                    + (f" ({head_reads} reads over 15 s)" if head_reads else ""),
                    state.threads, reviewed=reviewed)
    return None
