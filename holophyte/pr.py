"""`[merge] mode = "pr"`: the loop's one GitHub surface.
Design note 7. Instead of the `--no-ff` merge into main, an approved,
verified candidate is pushed to `origin` and opened as a pull request whose
body is written from the change, so the repository's own review bots and CI
see the change before it lands. The loop then
babysits the PR (`holophyte.loop`): `pr_state()` reads its unresolved
review threads and its checks, `reply_thread()` and `resolve_thread()`
answer the threads the adjudicator verdicted, `merge_pull_request()` lands
it through the merge API once it is green and quiet -- never a local push
of `main`.

Everything that talks to GitHub is in this file, so the surface is one seam
to port: `check_pr_route()` is the startup preflight, `push_branch()` and
`create_pull_request()` the two calls the merge path makes; the babysitter's
calls go through `graphql()` and `rest()`. The route is `gh` on PATH,
authenticated, or -- with no `gh` --
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

import ticket_template
from holophyte import config_tables
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
# The reviewer the babysitter stamps a pass with when no thread named one: the
# pass judged the checks alone.
NO_AUTHOR = "ci"

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
# The one read `_open_pr` makes between the push and `gh pr create`
# (KO-407): is the branch already the head of an open pull request? A run
# resumed on a branch its failed predecessor opened as a PR adopts that PR;
# the create would refuse with one still open.
OPEN_PULL_QUERY = """
query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $branch, states: OPEN, first: 1) {
      nodes { url }
    }
  }
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
    """Comment text and author, whose kind is user, bot, or unknown."""

    author: str
    body: str
    author_kind: str = "unknown"


@dataclass(frozen=True)
class Thread:
    """A review thread or conversation instruction, with replies oldest first."""

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
    kind: str = "review"
    classification: str = ""
    request: str = ""

    @property
    def comments(self):
        """The complete conversation, oldest first, with account kinds."""
        return (Comment(self.author, self.body, self.author_kind),) + self.replies


@dataclass(frozen=True)
class PrState:
    """The pull request as one `pr_state()` read saw it. `mergeable` is
    GitHub's answer: MERGEABLE, CONFLICTING or UNKNOWN -- and UNKNOWN is
    what a read that predates or omits the field is held to, never a
    license to merge. `updated_at` is epoch milliseconds, or None if absent."""

    threads: tuple  # unresolved `Thread`s, oldest first
    checks: str  # "success", "pending" or "failure"
    head_sha: str | None
    merged: bool = False
    merge_sha: str | None = None
    closed_by: str | None = None
    closed: bool = False
    mergeable: str = "UNKNOWN"
    updated_at: int | None = None
    pending_contexts: tuple = ()


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


def pr_body_stub(task, reason, issue_url):
    """A short Summary and the failure reason, followed by the ticket link."""
    summary = ticket_template.parse(task.get("body") or "").sections.get(
        "Summary", "").strip()
    paragraph = []
    for line in summary.splitlines():
        if not line.strip():
            break
        paragraph.append(line.strip())
    summary = " ".join(paragraph)[:600]
    reason = " ".join(reason.split())
    text = f"The description could not be written: {reason}"
    if summary:
        text = f"{summary}\n\n{text}"
    return pr_body_written(text, task["id"], issue_url)


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


def split_pr_body(body):
    """Split loop text, Linear line, Evidence block and appended tail losslessly.

    Whitespace following a preserved section belongs to that section. The
    first HTML comment after Linear always starts externally owned text.
    """
    linear = re.search(r"^Linear:[^\n]*(?:\n|$)", body, re.MULTILINE)
    if linear is None:
        return body, "", "", ""
    tail_start = body.find("<!--", linear.end())
    region = body if tail_start < 0 else body[:tail_start]
    heading = re.search(r"^## Evidence[ \t]*\r?$", region, re.MULTILINE)
    evidence = ""
    if heading:
        end = re.search(r"^## |^Linear:|<!--", body[heading.end():], re.MULTILINE)
        cut = heading.end() + end.start() if end else len(body)
        evidence = body[heading.start():cut]
        body = body[:heading.start()] + body[cut:]
        linear = re.search(r"^Linear:[^\n]*(?:\n|$)", body, re.MULTILINE)
    own, link = body[:linear.start()], linear.group()
    rest = body[linear.end():]
    space = len(rest) - len(rest.lstrip("\r\n"))
    link += rest[:space]
    return own, link, evidence, rest[space:]


def replace_pr_text(body, text):
    """Replace only the loop's prose, retaining the preserved slices verbatim."""
    _, link, evidence, tail = split_pr_body(body)
    before_linear = evidence and body.index(evidence) < body.index(link)
    preserved = evidence + link if before_linear else link + evidence
    return text.rstrip() + ("\n\n" if link else "") + preserved + tail


def edit_pr_body(target, pull, body):
    """Edit only the body through the configured GitHub route."""
    if shutil.which(GH) is None:
        rest(target, pull, "PATCH", f"repos/{pull.repo}/pulls/{pull.number}",
             {"body": body})
        return
    try:
        result = subprocess.run(
            [GH, "pr", "edit", pull.url, "--body-file", "-"],
            cwd=target.path, input=body, capture_output=True, text=True,
            timeout=PR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InfraFailure(f"{GH} pr edit did not answer in {PR_TIMEOUT}s") from None
    if result.returncode:
        detail = " ".join((result.stderr or result.stdout).split())[-500:]
        raise InfraFailure(f"{GH} pr edit failed: {detail}")


def push_branch(target, branch):
    """`git push origin BRANCH` from the target checkout; a refusal is an
    `InfraFailure` naming the remote's answer, with the branch untouched."""
    from holophyte.environment_git import refuse_environment_push

    refuse_environment_push(target, branch)
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


def open_pull_request(target, branch):
    """The URL of the open pull request `branch` is the head of, or None.

    Asked between the push and `create_pull_request()` (KO-407): a run
    resumed on a branch its failed predecessor already opened as a pull
    request adopts that PR instead of opening a second one, which GitHub
    would refuse. The repository is `origin`'s, its owner and name read
    off the remote URL by `parse_pr_url()` the way a PR's own URL is. An
    `origin` this cannot read, or an answer carrying no open pull request
    for the branch, is None -- the create that follows answers with its
    own InfraFailure; a node without a readable URL is InfraFailure, as
    every unreadable answer here is.
    """
    # Imported at the call: `holophyte.pr_status` imports this module's
    # transport at module top, so the import runs one way.
    from holophyte.pr_status import parse_pr_url
    pull = _origin_pull(target)
    if pull is None:
        return None
    data = graphql(target, pull, OPEN_PULL_QUERY,
                   {"owner": pull.owner, "name": pull.name,
                    "branch": branch})
    repository = data.get("repository") if isinstance(data, dict) else None
    nodes = (((repository or {}).get("pullRequests") or {})
             .get("nodes"))
    if not nodes:
        return None
    first = nodes[0]
    url = first.get("url") if isinstance(first, dict) else None
    if not isinstance(url, str) or parse_pr_url(url) is None:
        raise InfraFailure(f"GitHub answered the open pull requests of"
                           f" {branch} without a readable URL:"
                           f" {_short(first)}")
    return url


def _origin_pull(target):
    """The repository `origin` names as a `PullRequest` shell -- the host
    its API answers on and the owner and name the queries are scoped by,
    with the number and url `parse_pr_url()` filled in left as dummies --
    or None when the remote's URL is not one this can read. The remote
    URL is put in the shape `PR_URL_RE` matches first, so the ssh forms
    and an Enterprise host read the same way as an https one.
    """
    # Imported at the call: `holophyte.pr_status` imports this module's
    # transport at module top, so the import runs one way.
    from holophyte.pr_status import parse_pr_url
    url = origin_url(target)
    if not url:
        return None
    text = url.strip()
    ssh = re.match(r"(?:git@|ssh://git@)([^/:]+)[:/](.*)", text)
    if ssh:
        text = f"https://{ssh.group(1)}/{ssh.group(2)}"
    text = re.sub(r"\.git$", "", text.rstrip("/"))
    return parse_pr_url(f"{text}/pull/0")


def _count(value):
    """`value` as a non-negative int, or None for anything GitHub did not
    answer as one."""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


def _comment_url(node):
    """The opening comment's URL off a thread node, or None."""
    nodes = (node.get("comments") or {}).get("nodes") or ()
    first = next((c for c in nodes if isinstance(c, dict)), None)
    return first.get("url") if first else None


def reply_thread(target, pull, thread_id, body):
    """Post `body` as a reply on review thread `thread_id`."""
    graphql(target, pull, REPLY_MUTATION, {"thread": thread_id, "body": body})


def comment_on_pull(target, pull, body):
    """Post a new conversation comment on the pull request."""
    rest(target, pull, "POST",
         f"repos/{pull.repo}/issues/{pull.number}/comments", {"body": body})


def resolve_thread(target, pull, thread_id):
    """Mark review thread `thread_id` resolved."""
    graphql(target, pull, RESOLVE_MUTATION, {"thread": thread_id})


def merge_pull_request(target, pull, sha):
    """Merge pinned head `sha`; return the landed sha. Squash/merge use the
    PR title plus ` (#N)` and Summary paragraph; rebase leaves messages alone.
    Refusals raise `MergeRefused`; route failures raise `InfraFailure`."""
    method = config_tables.merge_config(target).pr_merge_method
    payload = {"merge_method": method, "sha": sha}
    if method in {"squash", "merge"}:
        details = rest(target, pull, "GET",
                       f"repos/{pull.repo}/pulls/{pull.number}")
        summary = re.search(r"^## Summary\s*\n(.*?)(?=^## |\Z)",
                            details.get("body") or "", re.MULTILINE | re.DOTALL)
        payload.update(
            commit_title=f"{details['title']} (#{pull.number})",
            commit_message=re.split(r"\n\s*\n", summary.group(1).strip())[0]
            if summary else "")
    try:
        answer = rest(target, pull, "PUT",
                      f"repos/{pull.repo}/pulls/{pull.number}/merge",
                      payload)
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
