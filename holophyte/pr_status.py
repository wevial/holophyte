"""Reading a pull request's state, out of `holophyte.pr` (KO-426).

`holophyte.pr` keeps `[merge] mode = "pr"`'s writers -- the route check,
the push, the open and the merge -- and the `gh`/API transport
(`graphql`, `rest`, `_call`). This module is the readers: `pull_status()`,
the reconcile's one read of a parked run's pull request; `pr_state()`,
the babysitter's read of its unresolved threads and the head's checks
folded by `fold_checks()`; and `parse_pr_url()`, the `PullRequest` a run's
`prUrl` names. The transport and the shapes the answers are folded into
(`PullRequest`, `Thread`, `Comment`, `PrState`) are imported back from
`holophyte.pr` at module top; `pr.py` reaches `parse_pr_url()` lazily, so
the module-top import runs one way.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from holophyte.gates import InfraFailure
from holophyte.pr import (
    Comment,
    PrState,
    PullRequest,
    Thread,
    _comment_url,
    _count,
    _short,
    graphql,
    rest,
)

# The shape of a pull request URL, `gh pr create`'s and the API's alike; the
# host is kept so an Enterprise PR is answered on its own API.
PR_URL_RE = re.compile(r"^https://([^/\s]+)/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$")
# What `statusCheckRollup.state` says, folded to the three answers the
# babysitter acts on. A PR with no checks at all (`null`) has nothing to wait
# for and reads as green -- as far as the rollup goes: `fold_checks()`
# reads the head's check runs and the branch's required contexts beside
# it, since seconds after a PR opens the rollup already says success while
# only the instant checks have reported and the rest are still queued.
CHECK_STATES = {None: "success", "SUCCESS": "success",
                "PENDING": "pending", "EXPECTED": "pending"}
# A check run's `conclusion` that is red; `neutral`, `skipped`, `success`
# and the rest are not.
RED_CONCLUSIONS = {"failure", "timed_out", "cancelled", "action_required",
                   "startup_failure"}
# The check-runs read's page size: past this many runs on one commit the
# babysitter reads the first page only.
CHECK_RUNS_PAGE = 100

# One page of threads per call; `$after` walks the rest, so a PR with more
# than `THREADS_PAGE` threads is read to the end before the babysitter decides
# it has nothing open.
THREADS_PAGE = 100
# One page of a thread's comments in the state query; a thread with more
# is read to its last page (`THREAD_COMMENTS_QUERY`) before it is judged,
# so the latest word in a long thread is in the brief.
COMMENTS_PAGE = 50
STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state merged headRefOid mergeable mergeCommit { oid } updatedAt
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      reviewThreads(first: %d, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved isOutdated path line
          comments(first: %d) {
            pageInfo { hasNextPage endCursor }
            nodes { author { login __typename } body url }
          }
        }
      }
    }
  }
}""" % (THREADS_PAGE, COMMENTS_PAGE)
THREAD_COMMENTS_QUERY = """
query($thread: ID!, $after: String) {
  node(id: $thread) {
    ... on PullRequestReviewThread {
      comments(first: %d, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { author { login __typename } body url }
      }
    }
  }
}""" % COMMENTS_PAGE


# The one read the loop's pull-request reconcile makes of a parked PR: is it
# still open, merged (as which commit, by whom) or closed unmerged, and --
# KO-362 -- whether anything happened on it since the babysitter last looked:
# `updatedAt` and the count of its review threads, which the reconcile
# holds against what the last babysit pass recorded (`runs.prSeenAt`,
# `runs.prSeenThreads`), and -- KO-368 -- the facts `/attention` shows
# beside them: the head commit's checks rollup and the review decision
# (`runs.prSeenChecks`, `runs.prSeenReview`). The thread bodies and the
# per-run checks are still the babysitter's own read. `mergeable` rides
# along too: GitHub's MERGEABLE / CONFLICTING / UNKNOWN, the answer a
# resumed pass merges `origin/main` on. `rateLimit` rides
# along at no cost: the remaining GraphQL budget on the token and when it
# resets, so the reconcile backs off before the babysitter's reads run it
# dry.
PULL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state merged mergeable mergeCommit { oid } mergedBy { login }
      updatedAt reviewThreads { totalCount }
      reviewDecision
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    }
  }
  rateLimit { remaining resetAt }
}"""


@dataclass(frozen=True)
class PullStatus:
    """A pull request as one `pull_status()` read saw it: open, merged as
    `merge_sha` by `merged_by`, or closed without merging; when it last
    changed (`updated_at`, GitHub's ISO 8601 timestamp) and how many
    review threads it carries (`threads`), None for either when GitHub
    did not say; the head commit's checks rollup as `checks` ("success",
    "pending" or "failure", None when the head carries no rollup -- a
    pull request with no checks -- or the answer had none) and GitHub's
    `reviewDecision` lower-cased as `review` ("approved",
    "changes_requested", "review_required", None when the repository
    requires no review or the answer had none); `mergeable` as GitHub
    spells it ("MERGEABLE", "CONFLICTING", "UNKNOWN", None when the
    answer had none); and the token's remaining
    GraphQL budget with the time it resets (`rate_remaining`,
    `rate_reset`), None when the answer carried no `rateLimit`."""

    merged: bool
    closed: bool
    merge_sha: str | None = None
    merged_by: str | None = None
    updated_at: str | None = None
    threads: int | None = None
    checks: str | None = None
    review: str | None = None
    mergeable: str | None = None
    rate_remaining: int | None = None
    rate_reset: str | None = None


def pull_status(target, pull):
    """One GraphQL read of the pull request's `state`, `merged`,
    `mergeable`, `mergeCommit`, `mergedBy`, `updatedAt`, review-thread
    count,
    `reviewDecision` and head checks rollup, with the token's `rateLimit`
    (`PULL_QUERY`): the loop's reconcile of a run parked on its PR asks
    this once per pass. GitHub answering without the pull request is
    `InfraFailure`, as every read here is; an answer without the
    activity, fact or budget fields is one without them (None), not an
    error."""
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
    threads = node.get("reviewThreads") or {}
    rate = data.get("rateLimit") or {}
    updated = node.get("updatedAt")
    decision = node.get("reviewDecision")
    return PullStatus(merged=bool(node.get("merged")),
                      closed=node.get("state") == "CLOSED",
                      merge_sha=merge.get("oid") if isinstance(merge, dict)
                      else None,
                      merged_by=by.get("login") if isinstance(by, dict)
                      else None,
                      updated_at=updated if isinstance(updated, str)
                      else None,
                      threads=_count(threads.get("totalCount")
                                     if isinstance(threads, dict) else None),
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


def _head_checks(node):
    """The pull request node's head `statusCheckRollup.state` as "success",
    "pending" or "failure"; None when the head carries no rollup (a pull
    request with no checks) or the answer did not include the commit.
    Absent is absent here, not "pending": the babysitter's `fold_checks()`
    reads a missing rollup as green only beside the check runs and the
    required contexts, which this one-field read does not have."""
    commits = node.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    head = nodes[0] if isinstance(nodes, list) and nodes else None
    commit = head.get("commit") if isinstance(head, dict) else None
    rollup = commit.get("statusCheckRollup") if isinstance(commit, dict) \
        else None
    state = rollup.get("state") if isinstance(rollup, dict) else None
    if not isinstance(state, str):
        return None
    return CHECK_STATES.get(state, "failure")


def parse_pr_url(url):
    """The `PullRequest` a PR URL names, or None for a URL of another shape:
    a parked run whose `prUrl` this cannot read has nothing to babysit."""
    m = PR_URL_RE.match((url or "").strip())
    if m is None:
        return None
    host, owner, name, number = m.groups()
    return PullRequest(host=host, owner=owner, name=name, number=int(number),
                       url=url.strip())


def pr_state(target, pull):
    """One read of the pull request: its unresolved review threads, the head
    commit's check rollup, its `mergeable` answer, and whether it is
    already merged or closed.

    One GraphQL query per page of threads (`THREADS_PAGE`), walked to the
    last page before anything is decided: a PR whose first page is all
    resolved and whose open thread is on the next must not read as quiet.
    The head, the checks and the merged/closed answer are the first
    page's, so the threads and the checks are the same moment's. A
    thread's author and body are its opening comment's and its `replies`
    the rest of the conversation, read to the last page of comments
    (`COMMENTS_PAGE` per read) so a long thread's latest word is not
    dropped; a thread with no comments (GitHub does not make one) is
    skipped. Resolved threads are not returned: the babysitter answers what
    is open.
    """
    first_page = node = _pull_request_page(target, pull, None)
    threads = []
    while True:
        page = node.get("reviewThreads") or {}
        for t in (page.get("nodes") or ()):
            if not isinstance(t, dict) or t.get("isResolved"):
                continue
            comments = _comments_of(target, pull, t)
            if not comments:
                continue
            first, *rest = comments
            threads.append(Thread(
                id=t.get("id") or "", path=t.get("path") or "",
                line=t.get("line"), author=first.author, body=first.body,
                url=_comment_url(t) or pull.url,
                outdated=bool(t.get("isOutdated")), replies=tuple(rest),
                author_kind=first.author_kind))
        info = page.get("pageInfo") or {}
        if not (info.get("hasNextPage") and info.get("endCursor")):
            break
        node = _pull_request_page(target, pull, info["endCursor"])
    runs, required = _check_reads(target, pull, first_page.get("headRefOid"))
    return _state_of(first_page, threads, runs, required)


def _check_reads(target, pull, sha):
    """The head's check runs and `main`'s required contexts, two REST reads;
    either one the babysitter cannot make or cannot read is None, which
    `fold_checks()` takes as pending: a check the babysitter cannot see is
    never a check that passed."""
    runs = required = None
    if sha:
        try:
            runs = _check_runs_of(target, pull, sha)
        except InfraFailure:
            runs = None
    try:
        answer = rest(target, pull, "GET",
                      f"repos/{pull.owner}/{pull.name}/rules/branches/main")
        required = _required_contexts(answer)
    except InfraFailure:
        required = None
    return runs, required


def _check_runs_of(target, pull, sha):
    """Every check run of commit `sha`, walked page by page (`CHECK_RUNS_PAGE`
    a page) until the answer's `total_count` is in hand, or None when the
    answer is not readable as check runs or a page the babysitter asked for
    did not come back: a head with more runs than one page holds must not
    be read as green on the page alone."""
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
    """The contexts every `required_status_checks` rule names, or None for
    an answer that is not the rules list or a rule the babysitter cannot
    read as one: a required check it cannot make out is not one that
    reported, so None folds to pending, never green."""
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
    """The head's checks as one of "success", "pending" or "failure".

    `rollup` is `statusCheckRollup.state`; `runs` the head commit's check
    runs (each with `name`, `status` and `conclusion`) and `required` the
    contexts `main`'s rules require -- either None when the babysitter could
    not read it. Red first: a red rollup, or any completed run with a red
    conclusion. Then pending: a pending rollup, a run still queued or in
    progress, a required context with no completed run, a read that did
    not come back, or check data that is not readable as runs. Green is
    what is left: every run completed without a
    red conclusion and every required context reported. No rules and no
    runs is green, as the rollup alone said.
    """
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
    """Every comment of thread `node`, oldest first: the page the state
    query carried, then each further page by the thread's id."""
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
    """The `Comment`s of one comments page, in the order GitHub gave. The
    author's `__typename` is read as `"user"` or `"bot"`; any other type,
    or no author (a deleted account), is `"unknown"`."""
    comments = []
    for c in (page.get("nodes") or ()):
        if not isinstance(c, dict):
            continue
        author = c.get("author") or {}
        comments.append(Comment(
            author=author.get("login") or "unknown", body=c.get("body") or "",
            author_kind=AUTHOR_KINDS.get(author.get("__typename"), "unknown")))
    return comments


def _pull_request_page(target, pull, after):
    """The `pullRequest` node of one `STATE_QUERY` read, its threads the
    page after cursor `after` (None for the first)."""
    data = graphql(target, pull, STATE_QUERY,
                   {"owner": pull.owner, "name": pull.name,
                    "number": pull.number, "after": after})
    node = ((data.get("repository") or {}).get("pullRequest")
            if isinstance(data, dict) else None)
    if not isinstance(node, dict):
        raise InfraFailure(f"GitHub answered without pull request"
                           f" {pull.url}: {_short(data)}")
    return node


def _iso_ms(text):
    """`text`, GitHub's ISO 8601 `updatedAt`, as epoch milliseconds; None
    for anything else. A naive timestamp is read as UTC."""
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _state_of(node, threads, runs, required):
    """`PrState` from the first page's node, every page's threads, and the
    two check reads (`_check_reads()`) folded beside the rollup."""
    commits = ((node.get("commits") or {}).get("nodes") or ())
    rollup = None
    if commits and isinstance(commits[-1], dict):
        rollup = ((commits[-1].get("commit") or {})
                  .get("statusCheckRollup") or {}).get("state")
    checks = fold_checks(rollup, runs, required)
    merge = node.get("mergeCommit") or {}
    mergeable = node.get("mergeable")
    return PrState(threads=tuple(threads), checks=checks,
                   head_sha=node.get("headRefOid"),
                   merged=bool(node.get("merged")),
                   merge_sha=merge.get("oid") if isinstance(merge, dict)
                   else None,
                   closed=node.get("state") == "CLOSED",
                   mergeable=mergeable
                   if isinstance(mergeable, str) and mergeable
                   else "UNKNOWN",
                   updated_at=_iso_ms(node.get("updatedAt")))
