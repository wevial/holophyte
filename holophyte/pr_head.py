"""Guard the babysitter's candidate against a foreign pull request head."""
from dataclasses import replace

import store
import store.read
from holophyte import pr, pr_status
from holophyte.files import git
from holophyte.gates import InfraFailure
from holophyte.project import worktree_path
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
    # KO-491: the park suffix counts re-reads; the settled event counts all reads.
    if state.head_sha != sha:
        _pr_terminal(target, conn, run_id, provider, task_id, branch, sha,
                     pull, state, reviewed, head_rereads=reads - 1)
    return (replace(state, head_sha=sha)
            if not state.merged and not state.closed else state)


def _remote_head(target, branch):
    code, out = git(worktree_path(target, branch), "ls-remote", pr.REMOTE,
                    f"refs/heads/{branch}")
    if code or not out.strip():
        raise InfraFailure(f"cannot read remote head for {branch}: {out.strip()}")
    return out.split()[0]


def _stale_head(target, conn, run_id, branch, sha, state):
    remote = _remote_head(target, branch)
    ancestor, _ = git(worktree_path(target, branch), "merge-base",
                      "--is-ancestor", remote, sha)
    if remote != sha and ancestor != 0:
        return remote
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"pull request API head {state.head_sha} was stale"
                           f" against remote {remote}; continuing with pushed {sha}")
    return None


def _pr_terminal(target, conn, run_id, provider, task_id, branch, sha,
                 pull, state, reviewed, head_rereads=None):
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
        remote = _stale_head(target, conn, run_id, branch, sha, state)
        if remote is None:
            return None
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha,
                    pull,
                    f"the remote branch head is {remote[:12]},"
                    f" not the candidate {sha[:12]} this run pushed;"
                    " someone else pushed to the branch, and the"
                    " babysitter does not judge or merge their commit"
                    + (f" ({head_rereads} reads over 15 s)"
                       if head_rereads else ""),
                    state.threads, reviewed=reviewed)
    return None
