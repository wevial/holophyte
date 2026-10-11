import contextlib
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from holophyte.babysit.conversation_comments import (
    console_answers,
    conversation_threads,
    pull_comments,
)
from holophyte.babysit.thread_mentions import classify
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.pr.github import (
    Comment,
    PrState,
    PullRequest,
    Thread,
    _comment_url,
    _count,
    _short,
    acknowledged,
    graphql,
    rest,
)
from holophyte.pr.pr_activity import ACTIVITY_FIELDS, HEADER, activities
from holophyte.pr.pr_contexts import CONTEXTS_FIELDS, status_contexts_of

# The host is kept so an Enterprise PR is answered on its own API.
PR_URL_RE = re.compile(r"^https://([^/\s]+)/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$")
# Seconds after a PR opens the rollup already says success while the rest
# queue: `fold_checks()` reads the runs and required contexts beside it.
CHECK_STATES = {None: "success", "SUCCESS": "success",
                "PENDING": "pending", "EXPECTED": "pending"}
RED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required",
                   "startup_failure", "error"}
ACTIONS_APP = "github-actions"
WORKFLOW_RUN_RE = re.compile(r"/actions/runs/(\d+)/job/\d+")
CHECK_RUNS_PAGE = 100
# Refused on a plan that cannot have rules (private, GitHub Free): no rules.
PLAN_GATED = "Upgrade to GitHub Pro or make this repository public"

THREADS_PAGE = 100
COMMENTS_PAGE = 50
STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String,
      $contextsAfter: String, $commentsAfter: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(last: 1, itemTypes: [CLOSED_EVENT]) {
        nodes { ... on ClosedEvent { actor { login } } }
      }
      readyEvent: timelineItems(last: 1, itemTypes: [READY_FOR_REVIEW_EVENT]) {
        nodes { ... on ReadyForReviewEvent { createdAt } }
      }
      id isDraft state merged headRefOid mergeable mergeCommit { oid } updatedAt title
      reviewDecision
      commits(last: 1) { nodes { commit { statusCheckRollup { state %s } } } }
      comments(first: 100, after: $commentsAfter) {
        pageInfo { hasNextPage endCursor }
        nodes { id author { login __typename } body url viewerDidAuthor
                reactionGroups { content viewerHasReacted } }
      }
      reviewThreads(first: %d, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line
          comments(first: %d) {
            pageInfo { hasNextPage endCursor }
            nodes { id author { login __typename } body url
                    reactionGroups { content viewerHasReacted } }
          }
        }
      }
    }
  }
}""" % (CONTEXTS_FIELDS, THREADS_PAGE, COMMENTS_PAGE)
THREAD_COMMENTS_QUERY = """
query($thread: ID!, $after: String) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      comments(first: %d, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { id author { login __typename } body url
                reactionGroups { content viewerHasReacted } }
      }
    }
  }
}""" % COMMENTS_PAGE


# Creation/submission times, not updatedAt: edits are not new content.
PULL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      timelineItems(last: 1, itemTypes: [CLOSED_EVENT]) {
        nodes { ... on ClosedEvent { actor { login } } }
      }
      state merged mergeable mergeCommit { oid } mergedBy { login }
      updatedAt title
      threadCount: reviewThreads(first: %d) { totalCount nodes { isResolved } }
      reviewDecision
      %s
    }
  }
  viewer { login }
  rateLimit { remaining resetAt }
}""" % (THREADS_PAGE, ACTIVITY_FIELDS)


@dataclass(frozen=True)
class PullStatus:
    merged: bool
    closed: bool
    closed_by: str | None = None
    merge_sha: str | None = None
    merged_by: str | None = None
    updated_at: str | None = None
    threads: int | None = None
    open_threads: int | None = None
    open_threads_floor: bool | None = None
    checks: str | None = None
    review: str | None = None
    mergeable: str | None = None
    rate_remaining: int | None = None
    rate_reset: str | None = None
    activity: tuple = ()
    title: str | None = None


def pull_status(target, pull):
    data = graphql(target, pull, PULL_QUERY,
                   {"owner": pull.owner, "name": pull.name,
                    "number": pull.number})
    node = ((data.get("repository") or {}).get("pullRequest")
            if isinstance(data, dict) else None)
    if not isinstance(node, dict):
        raise InfraFailure(f"GitHub answered without pull request"
                           f" {pull.url}: {_short(data)}")
    merge = node.get("mergeCommit") or {}
    by = node.get("mergedBy") or {}
    threads = node.get("threadCount") or node.get("reviewThreads") or {}
    rate = data.get("rateLimit") or {}
    updated = node.get("updatedAt")
    decision = node.get("reviewDecision")
    title = node.get("title")
    return PullStatus(title=title if isinstance(title, str) else None,
                      activity=activities(target, pull, node,
                      (data.get("viewer") or {}).get("login"), graphql, rate),
                      merged=bool(node.get("merged")),
                      closed=node.get("state") == "CLOSED",
                   closed_by=_closed_by(node),
                      merge_sha=merge.get("oid") if isinstance(merge, dict)
                      else None,
                      merged_by=by.get("login") if isinstance(by, dict)
                      else None,
                      updated_at=updated if isinstance(updated, str)
                      else None,
                      threads=_count(threads.get("totalCount")
                                     if isinstance(threads, dict) else None),
                      open_threads=_open_count(threads),
                      open_threads_floor=_counted_in_part(threads),
                      checks=_head_checks(node),
                      review=decision.lower() if isinstance(decision, str)
                      and decision else None,
                      mergeable=node.get("mergeable")
                      if isinstance(node.get("mergeable"), str)
                      else None,
                      rate_remaining=_count(rate.get("remaining")
                                            if isinstance(rate, dict)
                                            else None),
                      rate_reset=rate.get("resetAt")
                      if isinstance(rate, dict)
                      and isinstance(rate.get("resetAt"), str) else None)


def _open_count(threads):
    nodes = threads.get("nodes") if isinstance(threads, dict) else None
    flags = [t.get("isResolved") if isinstance(t, dict) else None
             for t in nodes] if isinstance(nodes, list) else [None]
    if not all(isinstance(flag, bool) for flag in flags):
        return None
    return flags.count(False)


def _counted_in_part(threads):
    total = threads.get("totalCount") if isinstance(threads, dict) else None
    nodes = threads.get("nodes") if isinstance(threads, dict) else None
    if not isinstance(nodes, list) or _count(total) is None:
        return None
    return total > len(nodes)


def _head_checks(node):
    """Absent is absent, not "pending"."""
    commits = node.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    head = nodes[-1] if isinstance(nodes, list) and nodes else None
    commit = head.get("commit") if isinstance(head, dict) else None
    rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) \
        else None
    state = rollup.get("state") if isinstance(rollup, dict) else None
    if not isinstance(state, str):
        return None
    return CHECK_STATES.get(state, "failure")


def parse_pr_url(url):
    m = PR_URL_RE.match((url or "").strip())
    if m is None:
        return None
    host, owner, name, number = m.groups()
    return PullRequest(host=host, owner=owner, name=name, number=int(number),
                       url=url.strip())


def pr_state(target, pull):
    first_page = node = _pull_request_page(target, pull, None)
    threads = []
    while True:
        page = node.get("reviewThreads") or {}
        for t in (page.get("nodes") or ()):
            if not isinstance(t, dict):
                continue
            comments = _comments_of(target, pull, t)
            if not comments:
                continue
            first, *rest = comments
            thread = Thread(
                id=t.get("id") or "", path=t.get("path") or "",
                line=t.get("line"), author=first.author, body=first.body,
                url=_comment_url(t) or pull.url,
                outdated=bool(t.get("isOutdated")), replies=tuple(rest),
                author_kind=first.author_kind, node_id=first.node_id,
                acknowledged=first.acknowledged)
            if not t.get("isResolved") or _reopened(target, thread):
                threads.append(thread)
        info = page.get("pageInfo") or {}
        if not (info.get("hasNextPage") and info.get("endCursor")):
            break
        node = _pull_request_page(target, pull, info["endCursor"])
    comments = pull_comments(target, pull, first_page, _pull_request_page)
    threads.extend(conversation_threads(target, pull, comments))
    every_run, required = _check_reads(target, pull,
                                       first_page.get("headRefOid"))
    runs = _started_since_ready(every_run, first_page)
    rollup_stale = runs is not None and len(runs) < len(every_run)
    if runs is not None:
        try:
            runs += status_contexts_of(target, pull, first_page, graphql)
        except InfraFailure:
            runs = None
    return replace(_state_of(first_page, threads, runs, required, pull.awaited,
                             rollup_stale),
                   console_answers=console_answers(comments))


def _reopened(target, thread):
    """Resolving after an answer does not end the conversation."""
    if HEADER.match(thread.comments[-1].body):
        return False
    merge = merge_config(target)
    return classify(thread, merge.mention_handle,
                    merge.mention_accounts).classification == "MENTIONED"


def _check_reads(target, pull, sha):
    """Unreadable REST data stays pending."""
    runs = required = None
    if sha:
        with contextlib.suppress(InfraFailure):
            runs = _check_runs_of(target, pull, sha)
    with contextlib.suppress(InfraFailure):
        required = _required_contexts(main_rules(target, pull))
    if required is not None:
        required += _protected_contexts(target, pull)
    return runs, required


def main_rules(target, pull):
    try:
        return rest(target, pull, "GET",
                    f"repos/{pull.owner}/{pull.name}/rules/branches/main")
    except InfraFailure as e:
        if PLAN_GATED in str(e):
            return []
        raise


def _protected_contexts(target, pull):
    """Read off the branch, which a token that may read the repository may read."""
    try:
        branch = rest(target, pull, "GET",
                      f"repos/{pull.owner}/{pull.name}/branches/main")
    except InfraFailure:
        return []
    for key in ("protection", "required_status_checks"):
        branch = branch.get(key) if isinstance(branch, dict) else None
    contexts = branch.get("contexts") if isinstance(branch, dict) else None
    return [c for c in contexts if isinstance(c, str) and c] \
        if isinstance(contexts, list) else []


def _check_runs_of(target, pull, sha):
    base = (f"repos/{pull.owner}/{pull.name}/commits/{sha}"
            f"/check-runs?per_page={CHECK_RUNS_PAGE}")
    runs, page = [], 1
    while True:
        path = base if page == 1 else f"{base}&page={page}"
        answer = rest(target, pull, "GET", path)
        if not isinstance(answer, dict):
            return None
        batch, total = answer.get("check_runs"), answer.get("total_count")
        if not isinstance(batch, list) or not all(isinstance(r, dict)
                                                  for r in batch):
            return None
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(batch) if page == 1 else None
        if total is None:
            return None
        runs.extend(batch)
        if len(runs) >= total:
            return runs
        if not batch:
            return None  # Promised more than it gave: incomplete.
        page += 1


def _required_contexts(rules):
    """A required check it cannot make out folds to pending, never green."""
    if not isinstance(rules, list):
        return None
    contexts = []
    for rule in rules:
        if not isinstance(rule, dict):
            return None
        if rule.get("type") != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            return None
        checks = parameters.get("required_status_checks")
        if not isinstance(checks, list):
            return None
        for check in checks:
            context = check.get("context") if isinstance(check, dict) else None
            if not isinstance(context, str) or not context:
                return None
            contexts.append(context)
    return contexts


def fold_checks(rollup, runs, required):
    state = CHECK_STATES.get(rollup, "failure")
    if state == "failure":
        return state
    if runs is None or required is None:
        return "pending"
    if not isinstance(runs, list):
        return "pending"
    completed = set()
    for run in runs:
        if not isinstance(run, dict):
            state = "pending"  # Not a run the babysitter can read: not green.
            continue
        if run.get("status") != "completed":
            state = "pending"
            continue
        if run.get("conclusion") in RED_CONCLUSIONS:
            return "failure"
        completed.add(run.get("name"))
    if any(context not in completed for context in required):
        return "pending"
    return state


def _comments_of(target, pull, node):
    page = node.get("comments") or {}
    comments = _comment_nodes(page)
    info = page.get("pageInfo") or {}
    while info.get("hasNextPage") and info.get("endCursor"):
        data = graphql(target, pull, THREAD_COMMENTS_QUERY,
                       {"thread": node.get("id") or "",
                        "after": info["endCursor"]})
        page = ((data.get("node") or {}).get("comments")
                if isinstance(data, dict) else None) or {}
        comments.extend(_comment_nodes(page))
        info = page.get("pageInfo") or {}
    return comments


AUTHOR_KINDS = {"User": "user", "Bot": "bot"}


def _comment_nodes(page):
    comments = []
    for c in (page.get("nodes") or ()):
        if not isinstance(c, dict):
            continue
        author = c.get("author") or {}
        comments.append(Comment(
            author=author.get("login") or "unknown", body=c.get("body") or "",
            author_kind=AUTHOR_KINDS.get(author.get("__typename"), "unknown"),
            node_id=c.get("id") or "", acknowledged=acknowledged(c)))
    return comments


def _pull_request_page(target, pull, after, comments_after=None):
    data = graphql(target, pull, STATE_QUERY,
                   {"owner": pull.owner, "name": pull.name,
                    "number": pull.number, "after": after,
                    **({"commentsAfter": comments_after} if comments_after else {})})
    node = ((data.get("repository") or {}).get("pullRequest")
            if isinstance(data, dict) else None)
    if not isinstance(node, dict):
        raise InfraFailure(f"GitHub answered without pull request"
                           f" {pull.url}: {_short(data)}")
    return node


def _iso_ms(text):
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _ready_ms(node):
    events = node.get("readyEvent")
    events = events.get("nodes") if isinstance(events, dict) else None
    last = events[-1] if isinstance(events, list) and events else None
    return _iso_ms(last.get("createdAt")) if isinstance(last, dict) else None


def _started_before(run, ready):
    started = _iso_ms(run.get("started_at")) if isinstance(run, dict) else None
    return started is not None and started < ready


def _started_since_ready(runs, node):
    ready = _ready_ms(node)
    if runs is None or ready is None:
        return runs
    return [run for run in runs if not _started_before(run, ready)]


def _state_of(node, threads, runs, required, awaited=(), rollup_stale=False):
    commits = ((node.get("commits") or {}).get("nodes") or ())
    rollup = None
    if commits and isinstance(commits[-1], dict) and not rollup_stale:
        rollup = ((commits[-1].get("commit") or {})
                  .get("statusCheckRollup") or {}).get("state")
    checks = fold_checks(rollup, runs, None if required is None
                         else required + list(awaited))
    merge = node.get("mergeCommit") or {}
    mergeable = node.get("mergeable")
    decision = node.get("reviewDecision")
    return PrState(threads=tuple(threads), checks=checks,
                   head_sha=node.get("headRefOid"),
                   merged=bool(node.get("merged")),
                   merge_sha=merge.get("oid") if isinstance(merge, dict)
                   else None,
                   closed=node.get("state") == "CLOSED",
                   closed_by=_closed_by(node),
                   mergeable=mergeable
                   if isinstance(mergeable, str) and mergeable
                   else "UNKNOWN",
                   updated_at=_iso_ms(node.get("updatedAt")),
                   review=decision.lower() if isinstance(decision, str)
                   and decision else None,
                   pending_contexts=tuple(r["name"] for r in (runs or [])
                                          if isinstance(r, dict)
                                          and r.get("name")
                                          and r.get("status") != "completed"),
                   failed_checks=_failed_checks(runs),
                   missing_checks=_missing_checks(runs, required),
                   awaiting=_awaiting(runs, awaited),
                   draft=node.get("isDraft") is True,
                   node_id=node.get("id") or "")


@dataclass(frozen=True)
class FailedCheck:
    name: str
    conclusion: str
    url: str
    job_id: int | None = None
    workflow_run_id: int | None = None


def _failed_checks(runs):
    return tuple(FailedCheck(name=r.get("name") or "",
                             conclusion=r["conclusion"],
                             url=r.get("html_url") or "", job_id=_job_id(r),
                             workflow_run_id=_workflow_run_id(r))
                 for r in (runs or ()) if isinstance(r, dict)
                 and r.get("conclusion") in RED_CONCLUSIONS)


def _missing_checks(runs, required):
    """A check the babysitter cannot see is not one it can call absent."""
    if runs is None or required is None:
        return ()
    reported = {r.get("name") for r in runs if isinstance(r, dict)
                and r.get("conclusion") != "expected"}
    return tuple(c for c in dict.fromkeys(required) if c not in reported)


def _awaiting(runs, awaited):
    completed = {r.get("name") for r in (runs or ()) if isinstance(r, dict)
                 and r.get("status") == "completed"}
    return tuple(name for name in awaited
                 if runs is None or name not in completed)


def _job_id(run):
    app = run.get("app")
    if not isinstance(app, dict) or app.get("slug") != ACTIONS_APP:
        return None
    job = run.get("id")
    return job if isinstance(job, int) and not isinstance(job, bool) else None


def _workflow_run_id(run):
    match = WORKFLOW_RUN_RE.search(run.get("html_url") or "")
    return int(match[1]) if match else None


def _closed_by(node):
    events = (node.get("timelineItems") or {}).get("nodes") or []
    return ((events[-1].get("actor") or {}).get("login")
            if events else None)
