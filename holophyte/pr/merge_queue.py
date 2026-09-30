"""Landing a pull request through `main`'s merge queue, which a REST merge skips."""
from time import monotonic
from urllib.parse import quote

import store
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.loop.stop import stop_if_requested
from holophyte.pr import github, pr_status
from holophyte.redact import safe_print as print

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


GROUP_RED = ("failure", "cancelled")
# GitHub's largest page.
RUNS_PAGE = 100


class QueueLeft(Exception):
    def __init__(self, why, since=None):
        super().__init__(why)
        self.since = since


class QueueRemoved(Exception):
    def __init__(self, failed, group):
        super().__init__(f"checks {', '.join(c.name for c in failed)} failed"
                         f" on merge group {group}")
        self.failed, self.group = failed, group


def red_merge_group(target, pull, since):
    """`mergeQueueEntry.headCommit` is not read: GitHub leaves it undefined."""
    since_ms = pr_status._iso_ms(since)
    if since_ms is None:
        return None
    prefix = f"gh-readonly-queue/{github.BASE}/pr-{pull.number}-"
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
    """Other pull requests' queue runs can fill any number of pages first."""
    base = (f"repos/{pull.repo}/actions/runs?event=merge_group&created="
            f"{quote('>=' + since, safe='')}&per_page={RUNS_PAGE}")
    runs, page = [], 1
    while True:
        answer = github.rest(target, pull, "GET", f"{base}&page={page}")
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
    rules = pr_status.main_rules(target, pull)
    return isinstance(rules, list) and any(
        isinstance(rule, dict) and rule.get("type") == "merge_queue"
        for rule in rules)


def enqueue_pull_request(target, pull, sha):
    node = github.rest(target, pull, "GET",
                   f"repos/{pull.repo}/pulls/{pull.number}")["node_id"]
    try:
        data = github.graphql(target, pull, ENQUEUE_MUTATION,
                          {"pull": node, "sha": sha})
    except InfraFailure as e:
        if str(e).startswith("GitHub GraphQL refused"):
            raise github.MergeRefused(str(e)) from None
        raise
    entry = (data.get("enqueuePullRequest") or {}).get("mergeQueueEntry")
    return (entry or {}).get("enqueuedAt")


def land_through_queue(target, conn, run_id, pull, sha):
    since = enqueue_pull_request(target, pull, sha)
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "merge_queue",
                           f"added {pull.url} to the merge queue at {sha[:12]}")
    wait_s = merge_config(target).check_wait_sec
    deadline = monotonic() + wait_s
    while True:
        node = github.graphql(target, pull, QUEUE_QUERY,
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
        print(f"[holo2] {pull.url} is {where}; waiting {github.CHECK_POLL_S}s")
        stop_if_requested(conn, run_id, "merge_gate")
        github.SLEEP(min(github.CHECK_POLL_S, remaining))


def verified_merge(project, conn, run_id, provider, task_id, issue_id, branch,
                   wt, sha, beat_s, pull, reviewed, verified, verify_cmd,
                   contracts, ticket, budget_min, retry_conflicts):
    from holophyte.loop.merge_gate import _merge_gate
    from holophyte.pr.pullrequest import _merge_pr
    if sha != verified:
        _merge_gate(project, conn, run_id, provider, task_id, issue_id, branch,
                    wt, beat_s, sha, verify_cmd, contracts, ticket, budget_min,
                    sync_main=False)
    return _merge_pr(project, conn, run_id, provider, task_id, branch, wt, sha,
                     beat_s, pull, reviewed=reviewed, retry_conflicts=retry_conflicts)
