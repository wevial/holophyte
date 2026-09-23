"""Landing a pull request through `main`'s merge queue (KO-712).

When the rules for `main` hold a `merge_queue` rule, the REST merge is
refused or bypasses the queue, so two pull requests green against an older
`main` could land one after the other and leave it red. The babysitter adds
the pull request to the queue instead and waits there the way it waits for
checks: the queue's merge commit is the run's merge sha, and a removal or a
wait past `[merge] check_wait_sec` is `QueueLeft`, which parks the run --
unless the removal's merge-group commit has red Actions checks (KO-714):
that is `QueueRemoved`, which gets the babysit's one check fix turn.
"""
from time import monotonic

import store
from holophyte import pr, pr_status
from holophyte.config_tables import merge_config
from holophyte.gates import InfraFailure
from holophyte.redact import safe_print as print
from holophyte.stop import stop_if_requested

ENQUEUE_MUTATION = """
mutation($pull: ID!, $sha: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $pull, expectedHeadOid: $sha}) {
    mergeQueueEntry { position }
  }
}"""
QUEUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state merged mergeCommit { oid } isInMergeQueue
      mergeQueueEntry { headCommit { oid } }
    }
  }
}"""


class QueueLeft(Exception):
    """The pull request left the queue unmerged, or outstayed the wait;
    `group` is the last merge-group commit named before a removal."""

    def __init__(self, why, group=None):
        super().__init__(why)
        self.group = group


class QueueRemoved(Exception):
    """The queue removed the pull request with the Actions checks `failed`
    (`pr_status.FailedCheck`s) red on its merge-group commit `group`."""

    def __init__(self, failed, group):
        super().__init__(f"checks {', '.join(c.name for c in failed)} failed"
                         f" on merge group {group}")
        self.failed, self.group = failed, group


def red_group(target, conn, run_id, pull, left):
    """`QueueRemoved` for a removal `left` whose merge-group commit has red
    checks, every one an Actions job; None when it parks instead: no
    readable merge-group commit, no red check, or one without a job log."""
    if left.group is None:
        return None
    try:
        runs = pr_status._check_runs_of(target, pull, left.group)
    except InfraFailure:
        return None
    failed = pr_status._failed_checks(runs)
    if not failed or any(check.job_id is None for check in failed):
        return None
    names = ", ".join(check.name for check in failed)
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "merge_queue",
                           f"{pull.url} was removed from the merge queue:"
                           f" {names} failed on merge group {left.group[:12]}")
    return QueueRemoved(failed, left.group)


def merge_queue_required(target, pull):
    """Whether the rules answer for `main` holds a `merge_queue` rule."""
    rules = pr.rest(target, pull, "GET",
                    f"repos/{pull.repo}/rules/branches/{pr.BASE}")
    return isinstance(rules, list) and any(
        isinstance(rule, dict) and rule.get("type") == "merge_queue"
        for rule in rules)


def enqueue_pull_request(target, pull, sha):
    """Add the pull request to the queue pinned to head `sha`. GitHub's
    refusal (an `errors` answer) is `MergeRefused`, as a REST merge's is."""
    node = pr.rest(target, pull, "GET",
                   f"repos/{pull.repo}/pulls/{pull.number}")["node_id"]
    try:
        pr.graphql(target, pull, ENQUEUE_MUTATION, {"pull": node, "sha": sha})
    except InfraFailure as e:
        if str(e).startswith("GitHub GraphQL refused"):
            raise pr.MergeRefused(str(e)) from None
        raise


def land_through_queue(target, conn, run_id, pull, sha):
    """Enqueue at `sha`, then read the queue every `pr.CHECK_POLL_S` until
    the queue merges it; return the queue's merge commit."""
    enqueue_pull_request(target, pull, sha)
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "merge_queue",
                           f"added {pull.url} to the merge queue at {sha[:12]}")
    wait_s = merge_config(target).check_wait_sec
    deadline = monotonic() + wait_s
    group = None  # The removal's entry is gone; keep the last one seen.
    while True:
        node = pr.graphql(target, pull, QUEUE_QUERY,
                          {"owner": pull.owner, "name": pull.name,
                           "number": pull.number}
                          )["repository"]["pullRequest"]
        entry = (node.get("mergeQueueEntry") or {}).get("headCommit") or {}
        group = entry.get("oid") or group
        # A merge can read before GitHub names its commit; read it again.
        oid = (node.get("mergeCommit") or {}).get("oid")
        if node.get("merged") and oid:
            return oid
        if not node.get("merged") and not node.get("isInMergeQueue"):
            raise QueueLeft("the pull request was removed from the merge queue"
                            f" unmerged (state {node.get('state')})", group)
        where = ("merged without a named merge commit" if node.get("merged")
                 else "still in the merge queue")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise QueueLeft(f"the pull request was {where} after"
                            f" [merge] check_wait_sec = {wait_s}s")
        print(f"[holo2] {pull.url} is {where}; waiting {pr.CHECK_POLL_S}s")
        stop_if_requested(conn, run_id, "merge_gate")
        pr.SLEEP(min(pr.CHECK_POLL_S, remaining))


def verified_merge(project, conn, run_id, provider, task_id, issue_id, branch,
                   wt, sha, beat_s, pull, reviewed, verified, verify_cmd,
                   contracts, ticket, budget_min, retry_conflicts):
    """Gate a changed candidate before attempting the PR merge."""
    from holophyte.merge_gate import _merge_gate
    from holophyte.pullrequest import _merge_pr
    if sha != verified:
        _merge_gate(project, conn, run_id, provider, task_id, issue_id, branch,
                    wt, beat_s, sha, verify_cmd, contracts, ticket, budget_min,
                    sync_main=False)
    return _merge_pr(project, conn, run_id, provider, task_id, branch, wt, sha,
                     beat_s, pull, reviewed=reviewed, retry_conflicts=retry_conflicts)
