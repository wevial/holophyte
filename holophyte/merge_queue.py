"""Landing a pull request through `main`'s merge queue (KO-712).

When the rules for `main` hold a `merge_queue` rule, the REST merge is
refused or bypasses the queue, so two pull requests green against an older
`main` could land one after the other and leave it red. The babysitter adds
the pull request to the queue instead and waits there the way it waits for
checks: the queue's merge commit is the run's merge sha, and a removal or a
wait past `[merge] check_wait_sec` is `QueueLeft`, which parks the run.
"""
from time import monotonic

import store
from holophyte import pr
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
    }
  }
}"""


class QueueLeft(Exception):
    """The pull request left the queue unmerged, or outstayed the wait."""


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
    while True:
        node = pr.graphql(target, pull, QUEUE_QUERY,
                          {"owner": pull.owner, "name": pull.name,
                           "number": pull.number}
                          )["repository"]["pullRequest"]
        # A merge can read before GitHub names its commit; read it again.
        oid = (node.get("mergeCommit") or {}).get("oid")
        if node.get("merged") and oid:
            return oid
        if not node.get("merged") and not node.get("isInMergeQueue"):
            raise QueueLeft("the pull request was removed from the merge queue"
                            f" unmerged (state {node.get('state')})")
        where = ("merged without a named merge commit" if node.get("merged")
                 else "still in the merge queue")
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise QueueLeft(f"the pull request was {where} after"
                            f" [merge] check_wait_sec = {wait_s}s")
        print(f"[holo2] {pull.url} is {where}; waiting {pr.CHECK_POLL_S}s")
        stop_if_requested(conn, run_id, "merge_gate")
        pr.SLEEP(min(pr.CHECK_POLL_S, remaining))
