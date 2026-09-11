"""`[merge] mode = "pr"`: the loop's one GitHub surface.

Design note 7. Instead of the `--no-ff` merge into main, an approved,
verified candidate is pushed to `origin` and opened as a pull request whose
body is the ticket body plus the run's FINDINGS entry, so the repository's
own review bots and CI see the change before it lands. The loop then
babysits the PR (`holophyte.loop`): `pr_state()` reads its unresolved
review threads and its checks, `reply_thread()` and `resolve_thread()`
answer the threads the adjudicator verdicted, `merge_pull_request()` lands
it through the merge API once it is green and quiet -- never a local push
of `main`.

Everything that talks to GitHub is in this file, so the surface is one seam
to port: `check_pr_route()` is the startup preflight, `push_branch()` and
`create_pull_request()` the two calls the merge path makes, `pr_body()` the
body they carry, and the babysitter's calls go through `graphql()` and
`rest()`. The route is `gh` on PATH, authenticated, or -- with no `gh` --
the REST and GraphQL APIs with a token read from `TOKEN_VARS` in the
environment. The token is read there and only there: never written to the
config, the store or a log line.

Failures on the route are `InfraFailure`: a push refused, a PR create that
did not answer, a `gh` gone missing since startup say nothing about the
ticket, so they spend none of its strikes, and every one of them leaves the
branch and worktree as they were. A merge GitHub refuses is `MergeRefused`:
the PR's own state (a protection rule, a conflict), for the operator.
"""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import store.read
from holophyte import config
from holophyte.findings import run_entry
from holophyte.gates import InfraFailure

# The remote the mode pushes to and the branch the PR targets. `main` is the
# factory's only integration point, and `origin` is the remote the design
# note names; neither is a knob until a second repository asks for one.
REMOTE = "origin"
BASE = "main"
# Where a token is looked for when `gh` is not on PATH, in this order. Both
# are names `gh` itself honours, so an operator sets one thing.
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
# `gh` is the route's program; the REST endpoint is what a token talks to.
GH = "gh"
API = "https://api.github.com"
# Wall-clock cap on each call that leaves the machine. A push of one branch or
# one API call that has not answered in this long is a route that is down, and
# the run must end as an infra failure rather than hold its lease forever.
PR_TIMEOUT = 120
# How the babysitter waits for pending checks: one `pr_state()` read every
# `CHECK_POLL_S` seconds, for at most `CHECK_WAIT_S` before the run parks
# with the checks still pending. `SLEEP` is the seam a test replaces.
CHECK_POLL_S = 30
CHECK_WAIT_S = 1800
SLEEP = time.sleep
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
# The reviewer the babysitter stamps a pass with when no thread named one: the
# pass judged the checks alone.
NO_AUTHOR = "ci"

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
      state merged headRefOid mergeCommit { oid }
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
REPLY_MUTATION = """
mutation($thread: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
      input: {pullRequestReviewThreadId: $thread, body: $body}) {
    comment { url }
  }
}"""
RESOLVE_MUTATION = """
mutation($thread: ID!) {
  resolveReviewThread(input: {threadId: $thread}) { thread { isResolved } }
}"""


# The one read the loop's pull-request reconcile makes of a parked PR: is it
# still open, merged (as which commit, by whom) or closed unmerged, and --
# KO-362 -- whether anything happened on it since the babysitter last looked:
# `updatedAt` and the count of its review threads, which the reconcile
# holds against what the last babysit pass recorded (`runs.prSeenAt`,
# `runs.prSeenThreads`), and -- KO-368 -- the facts `/attention` shows
# beside them: the head commit's checks rollup and the review decision
# (`runs.prSeenChecks`, `runs.prSeenReview`). The thread bodies and the
# per-run checks are still the babysitter's own read. `rateLimit` rides
# along at no cost: the remaining GraphQL budget on the token and when it
# resets, so the reconcile backs off before the babysitter's reads run it
# dry.
PULL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state merged mergeCommit { oid } mergedBy { login }
      updatedAt reviewThreads { totalCount }
      reviewDecision
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    }
  }
  rateLimit { remaining resetAt }
}"""


class MergeRefused(Exception):
    """GitHub would not merge the pull request: its answer, verbatim."""


@dataclass(frozen=True)
class PullRequest:
    """A pull request by its URL: the host its API answers on, the
    repository, and the number."""

    host: str
    owner: str
    name: str
    number: int
    url: str

    @property
    def repo(self):
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Comment:
    """One comment in a thread: who wrote it, what kind of account GitHub
    says that is (`author_kind`: `"user"` for a person, `"bot"` for an
    App, `"unknown"` for a deleted account or an answer without a type),
    and what it says."""

    author: str
    body: str
    author_kind: str = "unknown"


@dataclass(frozen=True)
class Thread:
    """One unresolved review thread: where it is, who opened it, what it
    says (the opening comment's body), where to read it, and the
    follow-ups (`replies`, oldest first) -- the conversation as it stands,
    since a thread's latest comment may turn a finding into a question or
    a rejection that the opening comment alone does not show."""

    id: str
    path: str
    line: int | None
    author: str
    body: str
    url: str
    outdated: bool = False
    replies: tuple = ()  # `Comment`s after the opening one
    # The opening comment's `Comment.author_kind`. The babysitter answers a
    # bot's thread and leaves a person's to the operator; `"unknown"` --
    # the default, and a deleted account -- is treated as a person's.
    author_kind: str = "unknown"


@dataclass(frozen=True)
class PrState:
    """The pull request as one `pr_state()` read saw it."""

    threads: tuple  # unresolved `Thread`s, oldest first
    checks: str  # "success", "pending" or "failure"
    head_sha: str | None
    merged: bool = False
    merge_sha: str | None = None
    closed: bool = False


def origin_url(target):
    """The URL of the target's `origin`, or None when there is no such remote."""
    r = subprocess.run(["git", "remote", "get-url", REMOTE], cwd=target.path,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def token_from_env(environ=None):
    """The first non-empty `TOKEN_VARS` value, or None. The value goes to the
    request header and nowhere else."""
    environ = os.environ if environ is None else environ
    for name in TOKEN_VARS:
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


def check_pr_route(target):
    """Startup: `[merge] mode = "pr"` has an `origin` and a way to open a PR.

    Called from `check_agent_commands()` before anything is claimed, so a
    target with no `origin` remote, or a host with neither an authenticated
    `gh` nor a token in the environment, is an error message rather than an
    approved run failing at the gate with its lease held. `gh auth status`
    is a probe of the credential, not a write; with no `gh` on PATH the
    token is only looked for, never sent.
    """
    if origin_url(target) is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: [merge] mode = \"pr\" pushes the task "
            f"branch to `{REMOTE}`, and {target.path} has no `{REMOTE}` remote "
            f"-- add one (`git remote add {REMOTE} URL`) or set [merge] mode "
            "to \"local\"")
    if shutil.which(GH) is not None:
        try:
            probe = subprocess.run([GH, "auth", "status"], cwd=target.path,
                                   capture_output=True, text=True,
                                   timeout=PR_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise SystemExit(
                f"[holo2] {target.config_path}: [merge] mode = \"pr\" opens pull "
                f"requests through `{GH}`, and `{GH} auth status` did not answer "
                f"in {PR_TIMEOUT}s") from None
        if probe.returncode:
            detail = (probe.stderr or probe.stdout).strip().splitlines()
            reason = detail[-1] if detail else f"exit {probe.returncode}"
            raise SystemExit(
                f"[holo2] {target.config_path}: [merge] mode = \"pr\" opens pull "
                f"requests through `{GH}`, and `{GH} auth status` failed: "
                f"{reason} -- run `{GH} auth login` or set [merge] mode to "
                "\"local\"")
        return
    if token_from_env() is None:
        names = " or ".join(TOKEN_VARS)
        raise SystemExit(
            f"[holo2] {target.config_path}: [merge] mode = \"pr\" opens pull "
            f"requests through `{GH}` or the GitHub API, and there is no "
            f"executable {GH!r} on PATH and no {names} in the environment -- "
            f"install `{GH}`, export a token, or set [merge] mode to \"local\"")


def pr_title(task_id, task):
    """`KO-n: TITLE`, the ticket the way the ledger and the branch name it."""
    return f"{task_id}: {task}"


def pr_body(conn, run_id, body, now):
    """The PR body: the ticket body, then the run's FINDINGS entry.

    The entry is `findings.run_entry` over the run as it stands at this
    moment -- not yet ended, so `now` is the stamp and the phase it is about
    to be parked in is the head -- rendered by the same function the
    close-out renders the window with, so the PR shows what FINDINGS.md will.
    A direct call with no store carries the ticket body alone.
    """
    body = (body or "").strip()
    if conn is None or run_id is None:
        return body
    detail = store.read.run_detail(conn, run_id)
    if detail is None:
        return body
    row = store.read.EndedRun(
        id=detail.id, linearIdentifier=detail.linearIdentifier,
        startedAt=detail.startedAt, endedAt=now, timeBoxMs=detail.timeBoxMs,
        reviewRoundCount=len(store.read.rounds_of(conn, run_id)),
        outcome="awaiting_merge_approval",
        outcomeReason=f"pull request open for {detail.branch}",
        branch=detail.branch, host=detail.host, mergeSha=None)
    entry = run_entry(row)
    return f"{body}\n\n{entry}" if body else entry


# The longest title a written reply may carry; GitHub truncates past 256, and
# a title longer than this is a paragraph, not a title (KO-336).
PR_TITLE_MAX = 120


def parse_pr_text(reply):
    """`(title, body)` from a written reply, or None when it has no
    `TITLE:` line, an empty title, or a title over `PR_TITLE_MAX`.

    The turn is asked for a line `TITLE: ...` followed by the body in
    Markdown. The first line starting with `TITLE:` is the title -- an agent
    that opens with a sentence of prose before it is still read -- and what
    lies after that line, stripped, is the body; an empty body is allowed,
    since a title alone is a PR the operator can still read. Whatever the
    reply printed before the title line is dropped: it is the turn's
    narration, not the description.
    """
    if not reply:
        return None
    lines = reply.splitlines()
    for n, line in enumerate(lines):
        if line.lstrip().startswith("TITLE:"):
            title = line.lstrip()[len("TITLE:"):].strip()
            if not title or len(title) > PR_TITLE_MAX:
                return None
            return title, "\n".join(lines[n + 1:]).strip()
    return None


def pr_body_written(body, task_id, issue_url):
    """The written body with one line `Linear: KO-n` appended, the issue's
    URL beside it when the provider carried one. No FINDINGS entry: the
    written form is the repository's description, not the factory's."""
    body = (body or "").strip()
    link = f"Linear: {task_id}"
    if issue_url:
        link = f"{link} ({issue_url})"
    return f"{body}\n\n{link}" if body else link


def push_branch(target, branch):
    """`git push origin BRANCH` from the target checkout; a refusal is an
    `InfraFailure` naming the remote's answer, with the branch untouched."""
    try:
        r = subprocess.run(["git", "push", REMOTE, branch], cwd=target.path,
                           capture_output=True, text=True, timeout=PR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InfraFailure(f"git push {REMOTE} {branch} did not answer in "
                           f"{PR_TIMEOUT}s; branch preserved") from None
    if r.returncode:
        detail = " ".join((r.stderr or r.stdout).split())[-500:]
        raise InfraFailure(f"git push {REMOTE} {branch} failed: {detail};"
                           " branch preserved, no pull request opened")


def create_pull_request(target, branch, title, body):
    """Open the PR for `branch` against `BASE`; return its URL.

    `gh pr create` when `gh` is on PATH, the REST API with the environment's
    token otherwise -- the same order `check_pr_route()` settled at startup.
    Neither answering is an `InfraFailure`: the branch is pushed and stays.
    """
    if shutil.which(GH) is not None:
        return _create_with_gh(target, branch, title, body)
    token = token_from_env()
    if token is None:
        raise InfraFailure(f"no {GH!r} on PATH and no "
                           f"{' or '.join(TOKEN_VARS)} in the environment;"
                           f" branch {branch} pushed and preserved, no pull"
                           " request opened")
    return _create_with_api(target, branch, title, body, token)


def _create_with_gh(target, branch, title, body):
    """`gh pr create`, pinned with `--repo` to the repository `origin` names,
    the body on stdin so no length or quoting limit bites; the URL is what
    `gh` prints on success.

    Without `--repo`, `gh` opens the PR in its own default repository
    (`gh repo set-default`), which need not be the one the branch was just
    pushed to -- a PR against the wrong repository, or a create refused
    after a successful push (review round 1). The `origin` URL is what `gh`
    is given, verbatim: `gh` reads OWNER/REPO and the host off an https or
    ssh URL itself, so a GitHub Enterprise `origin` pins as well as a
    github.com one.
    """
    repo = origin_url(target)
    if repo is None:
        raise InfraFailure(f"no `{REMOTE}` remote to open the pull request"
                           f" in; branch {branch} preserved")
    argv = [GH, "pr", "create", "--repo", repo, "--base", BASE,
            "--head", branch, "--title", title, "--body-file", "-"]
    try:
        r = subprocess.run(argv, cwd=target.path, input=body,
                           capture_output=True, text=True, timeout=PR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InfraFailure(f"{GH} pr create did not answer in {PR_TIMEOUT}s;"
                           f" branch {branch} pushed and preserved") from None
    url = _url_in(r.stdout) if r.returncode == 0 else None
    if url is None:
        detail = " ".join((r.stderr or r.stdout).split())[-500:]
        detail = detail or "no URL in its output"
        raise InfraFailure(f"{GH} pr create failed: {detail}; branch {branch}"
                           " pushed and preserved")
    return url


def _create_with_api(target, branch, title, body, token):
    """`POST /repos/OWNER/REPO/pulls` with the token as a bearer; the
    response's `html_url` is the PR."""
    repo = repo_of(origin_url(target))
    if repo is None:
        raise InfraFailure(f"cannot read OWNER/REPO off the {REMOTE} URL;"
                           f" branch {branch} pushed and preserved")
    payload = json.dumps({"title": title, "body": body, "head": branch,
                          "base": BASE}).encode()
    request = urllib.request.Request(
        f"{API}/repos/{repo}/pulls", data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "holophyte"})
    try:
        with urllib.request.urlopen(request, timeout=PR_TIMEOUT) as response:
            answer = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # The response body names the refusal (a branch already in a PR, a
        # token without `repo`); the header the token rode in is not in it.
        detail = " ".join(e.read().decode("utf-8", "replace").split())[-500:]
        raise InfraFailure(f"GitHub refused the pull request ({e.code}):"
                           f" {detail}; branch {branch} pushed and"
                           " preserved") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise InfraFailure(f"GitHub did not answer the pull request:"
                           f" {e}; branch {branch} pushed and"
                           " preserved") from None
    url = answer.get("html_url") if isinstance(answer, dict) else None
    if not url:
        raise InfraFailure("GitHub answered the pull request without a URL;"
                           f" branch {branch} pushed and preserved")
    return url


def repo_of(url):
    """`OWNER/REPO` off a GitHub remote URL (https, ssh, or `git@` form), or
    None when the URL is not one this can read."""
    if not url:
        return None
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _url_in(text):
    """The last `https://` token in `text`, or None: `gh pr create` prints
    the PR URL as its last line, after any progress it wrote."""
    urls = re.findall(r"https://\S+", text or "")
    return urls[-1] if urls else None


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
    requires no review or the answer had none); and the token's remaining
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
    rate_remaining: int | None = None
    rate_reset: str | None = None


def pull_status(target, pull):
    """One GraphQL read of the pull request's `state`, `merged`,
    `mergeCommit`, `mergedBy`, `updatedAt`, review-thread count,
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


def _count(value):
    """`value` as a non-negative int, or None for anything GitHub did not
    answer as one."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


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
    commit's check rollup, and whether it is already merged or closed.

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


def _comment_url(node):
    """The opening comment's URL off a thread node, or None."""
    nodes = (node.get("comments") or {}).get("nodes") or ()
    first = next((c for c in nodes if isinstance(c, dict)), None)
    return first.get("url") if first else None


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
    return PrState(threads=tuple(threads), checks=checks,
                   head_sha=node.get("headRefOid"),
                   merged=bool(node.get("merged")),
                   merge_sha=merge.get("oid") if isinstance(merge, dict)
                   else None,
                   closed=node.get("state") == "CLOSED")


def reply_thread(target, pull, thread_id, body):
    """Post `body` as a reply on review thread `thread_id`."""
    graphql(target, pull, REPLY_MUTATION, {"thread": thread_id, "body": body})


def resolve_thread(target, pull, thread_id):
    """Mark review thread `thread_id` resolved."""
    graphql(target, pull, RESOLVE_MUTATION, {"thread": thread_id})


def merge_pull_request(target, pull, sha):
    """Merge the pull request through the merge API, pinned to head `sha`;
    return the sha of the commit that landed on `main`. The method is the
    target's `[merge] pr_merge_method`: `"merge"` by default -- a merge
    commit, like the loop's `--no-ff` merge, so the branch's history lands
    as it was reviewed -- or `"squash"` or `"rebase"` where the
    repository's ruleset allows nothing else; for those the sha answered is
    the new commit on `main`, not a merge commit. `sha` is the candidate
    whose checks and threads the babysitter judged: the API's `sha` field
    makes GitHub refuse (409) if the head has moved since, so a push that
    raced the pass never lands on its verdict.
    GitHub declining -- a protection rule, a conflict, a check that turned
    red, the head moved -- is `MergeRefused` with its reason; the route not
    answering is `InfraFailure` as everywhere else."""
    method = config.merge_config(target).pr_merge_method
    try:
        answer = rest(target, pull, "PUT",
                      f"repos/{pull.repo}/pulls/{pull.number}/merge",
                      {"merge_method": method, "sha": sha})
    except InfraFailure as e:
        # A 405 (not mergeable) or 409 (head moved) is the PR refusing, not
        # the route; `_call` folds every non-2xx into the same exception,
        # so the status is read off its text.
        if re.search(r"\b(405|409|422)\b", str(e)):
            raise MergeRefused(str(e)) from None
        raise
    sha = answer.get("sha") if isinstance(answer, dict) else None
    if not sha or not answer.get("merged"):
        raise MergeRefused(f"GitHub did not merge {pull.url}:"
                           f" {_short(answer)}")
    return sha


def graphql(target, pull, query, variables):
    """One GraphQL call on the PR's host; the `data` object. Errors in the
    answer (`errors`) are `InfraFailure` naming the first."""
    answer = _call(target, pull.host, "POST", "graphql",
                   {"query": query, "variables": variables})
    if not isinstance(answer, dict):
        raise InfraFailure(f"GitHub GraphQL answered {_short(answer)}")
    if answer.get("errors"):
        first = answer["errors"][0]
        message = (first.get("message") if isinstance(first, dict)
                   else str(first))
        raise InfraFailure(f"GitHub GraphQL refused: {message}")
    return answer.get("data") or {}


def rest(target, pull, method, path, payload=None):
    """One REST call on the PR's host, `path` relative to the API root."""
    return _call(target, pull.host, method, path, payload)


def _call(target, host, method, path, payload):
    """The one call shape both APIs go through: `gh api` when `gh` is on
    PATH, the token and `urllib` otherwise; the decoded JSON answer, or
    `InfraFailure` for a route that refused or did not answer."""
    if shutil.which(GH) is not None:
        return _call_with_gh(target, host, method, path, payload)
    token = token_from_env()
    if token is None:
        raise InfraFailure(f"no {GH!r} on PATH and no "
                           f"{' or '.join(TOKEN_VARS)} in the environment")
    return _call_with_api(host, method, path, payload, token)


def _call_with_gh(target, host, method, path, payload):
    argv = [GH, "api", "--hostname", host, "--method", method, path]
    body = None
    if payload is not None:
        argv += ["--input", "-"]
        body = json.dumps(payload)
    try:
        r = subprocess.run(argv, cwd=target.path, input=body or "",
                           capture_output=True, text=True, timeout=PR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InfraFailure(f"{GH} api {path} did not answer in"
                           f" {PR_TIMEOUT}s") from None
    if r.returncode:
        detail = " ".join((r.stderr or r.stdout).split())[-500:]
        raise InfraFailure(f"{GH} api {path} failed: {detail or 'no output'}")
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except ValueError:
        raise InfraFailure(f"{GH} api {path} answered something that is not"
                           f" JSON: {_short(r.stdout)}") from None


def _call_with_api(host, method, path, payload, token):
    base = API if host == "github.com" else f"https://{host}/api/v3"
    if path == "graphql":
        url = (f"{API}/graphql" if host == "github.com"
               else f"https://{host}/api/graphql")
    else:
        url = f"{base}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "holophyte"})
    try:
        with urllib.request.urlopen(request, timeout=PR_TIMEOUT) as response:
            text = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = " ".join(e.read().decode("utf-8", "replace").split())[-500:]
        raise InfraFailure(f"GitHub refused {method} {path} ({e.code}):"
                           f" {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise InfraFailure(f"GitHub did not answer {method} {path}:"
                           f" {e}") from None
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        raise InfraFailure(f"GitHub answered {method} {path} with something"
                           f" that is not JSON: {_short(text)}") from None


def _short(value):
    """A value as one short line, for a message about an answer."""
    text = value if isinstance(value, str) else json.dumps(value)
    return " ".join(text.split())[:300]
