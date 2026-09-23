"""Landing a pull request through `main`'s merge queue (KO-712).

When the rules for `main` hold a `merge_queue` rule, the REST merge is
refused or bypasses the queue, so two pull requests green against an older
`main` could land one after the other and leave it red. The babysitter adds
the pull request to the queue instead and waits there the way it waits for
checks: the queue's merge commit is the run's merge sha, and a removal or a
wait past `[merge] check_wait_sec` is `QueueLeft`, which parks the run --
unless the merge group the queue last tested it on went red (KO-714): that
is `QueueRemoved`, which gets the babysit's one check fix turn.
"""
from time import monotonic
from urllib.parse import quote

import store
from holophyte import pr, pr_status
from holophyte.config_tables import merge_config
from holophyte.gates import InfraFailure
from holophyte.redact import safe_print as print
from holophyte.stop import stop_if_requested

ENQUEUE_MUTATION = """
mutation($pull: ID!, $sha: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $pull, expectedHeadOid: $sha}) {
    mergeQueueEntry { enqueuedAt }
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


# A merge group's workflow run with one of these conclusions failed it.
GROUP_RED = ("failure", "cancelled")
# Workflow runs per page of the Actions runs read, GitHub's most.
RUNS_PAGE = 100


class QueueLeft(Exception):
    """The pull request left the queue unmerged, or outstayed the wait;
    `since` is GitHub's `enqueuedAt` for a removal, None otherwise."""

    def __init__(self, why, since=None):
        super().__init__(why)
        self.since = since


class QueueRemoved(Exception):
    """The queue removed the pull request with the Actions checks `failed`
    (`pr_status.FailedCheck`s) red on its merge-group commit `group`."""

    def __init__(self, failed, group):
        super().__init__(f"checks {', '.join(c.name for c in failed)} failed"
                         f" on merge group {group}")
        self.failed, self.group = failed, group


def red_merge_group(target, pull, since):
    """The merge-group commit the queue last tested the pull request on if
    a workflow run there failed or was cancelled, else None. It is read off
    the Actions runs of event `merge_group` on a `gh-readonly-queue/<base>/
    pr-<number>-` branch created since `since`, the enqueue's `enqueuedAt`:
    their head sha is the merge-group commit. `mergeQueueEntry.headCommit`
    is not read: GitHub names it only "the head commit for this entry"."""
    since_ms = pr_status._iso_ms(since)
    if since_ms is None:
        return None
    prefix = f"gh-readonly-queue/{pr.BASE}/pr-{pull.number}-"
    ours = [r for r in group_runs(target, pull, since, since_ms)
            if str(r.get("head_branch") or "").startswith(prefix)
            and (pr_status._iso_ms(r.get("created_at")) or 0) >= since_ms]
    if not ours:
        return None
    latest = max(ours, key=lambda r: pr_status._iso_ms(r["created_at"]))
    group = latest.get("head_sha")
    red = any(r.get("head_sha") == group and r.get("conclusion") in GROUP_RED
              for r in ours)
    return group if red and isinstance(group, str) and group else None


def group_runs(target, pull, since, since_ms):
    """The repository's `merge_group` workflow runs created since `since`,
    paged newest first until a page runs short or reaches back past it: the
    other pull requests' queue runs can fill any number of pages first."""
    base = (f"repos/{pull.repo}/actions/runs?event=merge_group&created="
            f"{quote('>=' + since, safe='')}&per_page={RUNS_PAGE}")
    runs, page = [], 1
    while True:
        answer = pr.rest(target, pull, "GET", f"{base}&page={page}")
        batch = answer.get("workflow_runs") if isinstance(answer, dict) else None
        if not isinstance(batch, list):
            return runs
        runs.extend(r for r in batch if isinstance(r, dict))
        if len(batch) < RUNS_PAGE or any(
                (pr_status._iso_ms(r.get("created_at")) or 0) < since_ms
                for r in batch if isinstance(r, dict)):
            return runs
        page += 1


def red_group(target, conn, run_id, pull, left):
    """`QueueRemoved` for a removal `left` whose merge group went red with
    red checks there, every one an Actions job; None when it parks
    instead: no red merge group found, no red check on it, or one without
    a job log."""
    if left.since is None:
        return None
    try:
        group = red_merge_group(target, pull, left.since)
        runs = group and pr_status._check_runs_of(target, pull, group)
    except InfraFailure:
        return None
    failed = pr_status._failed_checks(runs)
    if not failed or any(check.job_id is None for check in failed):
        return None
    names = ", ".join(check.name for check in failed)
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "merge_queue",
                           f"{pull.url} was removed from the merge queue:"
                           f" {names} failed on merge group {group[:12]}")
    return QueueRemoved(failed, group)


def merge_queue_required(target, pull):
    """Whether the rules answer for `main` holds a `merge_queue` rule."""
    rules = pr.rest(target, pull, "GET",
                    f"repos/{pull.repo}/rules/branches/{pr.BASE}")
    return isinstance(rules, list) and any(
        isinstance(rule, dict) and rule.get("type") == "merge_queue"
        for rule in rules)


def enqueue_pull_request(target, pull, sha):
    """Add the pull request to the queue pinned to head `sha`; GitHub's
    `enqueuedAt` for it, None unnamed. GitHub's refusal (an `errors`
    answer) is `MergeRefused`, as a REST merge's is."""
    node = pr.rest(target, pull, "GET",
                   f"repos/{pull.repo}/pulls/{pull.number}")["node_id"]
    try:
        data = pr.graphql(target, pull, ENQUEUE_MUTATION,
                          {"pull": node, "sha": sha})
    except InfraFailure as e:
        if str(e).startswith("GitHub GraphQL refused"):
            raise pr.MergeRefused(str(e)) from None
        raise
    entry = (data.get("enqueuePullRequest") or {}).get("mergeQueueEntry")
    return (entry or {}).get("enqueuedAt")


def land_through_queue(target, conn, run_id, pull, sha):
    """Enqueue at `sha`, then read the queue every `pr.CHECK_POLL_S` until
    the queue merges it; return the queue's merge commit."""
    since = enqueue_pull_request(target, pull, sha)
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
                            f" unmerged (state {node.get('state')})", since)
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
