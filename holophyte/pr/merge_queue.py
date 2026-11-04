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

QUEUE_READ_QUERY = """
query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) {
      configuration {
        mergeMethod mergingStrategy maximumEntriesToBuild
        minimumEntriesToMerge checkResponseTimeout
      }
      entries(first: 100) {
        nodes {
          position state enqueuedAt estimatedTimeToMerge jump solo
          pullRequest { number title url headRefOid author { login } }
        }
      }
    }
    pullRequests(baseRefName: $branch, first: 50,
                 orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number title url headRefOid author { login }
        timelineItems(last: 20, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT,
                      REMOVED_FROM_MERGE_QUEUE_EVENT, MERGED_EVENT]) {
          nodes {
            __typename
            ... on AddedToMergeQueueEvent { createdAt }
            ... on RemovedFromMergeQueueEvent { createdAt reason }
            ... on MergedEvent { createdAt }
          }
        }
      }
    }
  }
}"""
CONFIGURATION = (("mergeMethod", "merge_method"),
                 ("mergingStrategy", "merging_strategy"),
                 ("maximumEntriesToBuild", "maximum_entries_to_build"),
                 ("minimumEntriesToMerge", "minimum_entries_to_merge"),
                 ("checkResponseTimeout", "check_response_timeout"))
QUEUED = "QUEUED"
LEFT_WINDOW_MS = 2 * 60 * 60 * 1000
ADDED, REMOVED, MERGED = ("AddedToMergeQueueEvent",
                          "RemovedFromMergeQueueEvent", "MergedEvent")

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
    group, runs = merge_groups(target, pull, {pull.number: since}).get(
        pull.number, (None, ()))
    red = any(r.get("conclusion") in GROUP_RED for r in runs)
    return group if red else None


def merge_groups(target, pull, entered):
    sinces = {number: ms for number, since in entered.items()
              if (ms := pr_status._iso_ms(since)) is not None}
    if not sinces:
        return {}
    earliest = min(sinces.values())
    since = next(entered[n] for n, ms in sinces.items() if ms == earliest)
    runs = group_runs(target, pull, since, earliest)
    groups = {}
    for number, since_ms in sinces.items():
        prefix = f"gh-readonly-queue/{github.BASE}/pr-{number}-"
        ours = [r for r in runs
                if str(r.get("head_branch") or "").startswith(prefix)
                and (pr_status._iso_ms(r.get("created_at")) or 0) >= since_ms]
        if not ours:
            continue
        latest = max(ours, key=lambda r: pr_status._iso_ms(r["created_at"]))
        group = latest.get("head_sha")
        if isinstance(group, str) and group:
            groups[number] = (group, [r for r in ours
                                      if r.get("head_sha") == group])
    return groups


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


def queue_read(target, now_ms):
    pull = github._origin_pull(target)
    if pull is None:
        raise InfraFailure(f"origin {github.origin_url(target)!r} is not a"
                           " GitHub repository")
    data = github.graphql(target, pull, QUEUE_READ_QUERY,
                          {"owner": pull.owner, "name": pull.name,
                           "branch": github.BASE})
    repository = data.get("repository") or {}
    queue = repository.get("mergeQueue")
    if not isinstance(queue, dict):
        return {"configured": False, "configuration": None, "entries": [],
                "left": []}
    configuration = queue.get("configuration") or {}
    left = [row for node in _nodes(repository.get("pullRequests"))
            if (row := _left_row(node, now_ms)) is not None]
    return {"configured": True,
            "configuration": {key: configuration.get(field)
                              for field, key in CONFIGURATION},
            "entries": _queue_entries(target, pull,
                                      _nodes(queue.get("entries"))),
            "left": sorted(left, key=lambda row: row["at_ms"], reverse=True)}


def _nodes(connection):
    nodes = connection.get("nodes") if isinstance(connection, dict) else None
    return [node for node in nodes or () if isinstance(node, dict)]


def _pull_row(node):
    return {"number": node.get("number"), "title": node.get("title"),
            "author": (node.get("author") or {}).get("login"),
            "url": node.get("url"), "head_oid": node.get("headRefOid")}


def _queue_entries(target, pull, nodes):
    nodes = sorted(nodes, key=lambda node: node.get("position") or 0)
    building = {(node.get("pullRequest") or {}).get("number"):
                node.get("enqueuedAt") for node in nodes
                if node.get("state") != QUEUED}
    groups = merge_groups(target, pull, building)
    required = set()
    if groups:
        required = set(pr_status._required_contexts(
            pr_status.main_rules(target, pull)) or ())
    return [_queue_entry(target, pull, node, required, groups.get(
        (node.get("pullRequest") or {}).get("number"))) for node in nodes]


def _queue_entry(target, pull, node, required, found):
    group, workflow_runs = found or (None, ())
    runs = pr_status._check_runs_of(target, pull, group) if group else []
    if runs is None:
        raise InfraFailure(f"GitHub answered the check runs of merge group"
                           f" {group} incompletely")
    workflows = {run.get("check_suite_id"): run.get("name")
                 for run in workflow_runs}
    started = [ms for run in workflow_runs if (ms := pr_status._iso_ms(
        run.get("run_started_at") or run.get("created_at"))) is not None]
    checks = [_check_row(run, workflows, required) for run in runs]
    return {"position": node.get("position"), "state": node.get("state"),
            "enqueued_ms": pr_status._iso_ms(node.get("enqueuedAt")),
            "estimated_s": node.get("estimatedTimeToMerge"),
            "jump": bool(node.get("jump")), "solo": bool(node.get("solo")),
            "pr": _pull_row(node.get("pullRequest") or {}),
            "group_sha": group, "building_ms": min(started, default=None),
            "checks": {"done": sum(c["status"] == "completed" for c in checks),
                       "total": len(checks),
                       "failed": sum(c["conclusion"] in pr_status.RED_CONCLUSIONS
                                     for c in checks),
                       "runs": checks}}


def _check_row(run, workflows, required):
    return {"name": run.get("name"),
            "workflow": workflows.get((run.get("check_suite") or {}).get("id")),
            "status": run.get("status"), "conclusion": run.get("conclusion"),
            "started_ms": pr_status._iso_ms(run.get("started_at")),
            "completed_ms": pr_status._iso_ms(run.get("completed_at")),
            "url": run.get("html_url"), "required": run.get("name") in required}


def _left_row(node, now_ms):
    events = sorted(((ms, event) for event in _nodes(node.get("timelineItems"))
                     if (ms := pr_status._iso_ms(event.get("createdAt")))
                     is not None), key=lambda pair: pair[0])
    merged = [ms for (_, before), (ms, event) in zip(events, events[1:])
              if event.get("__typename") == MERGED
              and before.get("__typename") == ADDED]
    if merged:
        at_ms, outcome, reason = merged[-1], "merged", None
    elif events and events[-1][1].get("__typename") == REMOVED:
        at_ms, outcome, reason = (events[-1][0], "removed",
                                  events[-1][1].get("reason"))
    else:
        return None
    if at_ms < now_ms - LEFT_WINDOW_MS:
        return None
    return {"pr": _pull_row(node), "at_ms": at_ms, "outcome": outcome,
            "reason": reason}


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
