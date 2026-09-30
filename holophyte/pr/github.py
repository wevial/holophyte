import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

import ticket_template
from holophyte import deadline
from holophyte.config import config_tables
from holophyte.loop.gates import InfraFailure
from holophyte.redact import known_secrets, outbound

REMOTE = "origin"
BASE = "main"
# Both are names `gh` itself honours, so an operator sets one thing.
TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
GH = "gh"
API = "https://api.github.com"
# A call unanswered this long is a route down: fail rather than hold a lease.
PR_TIMEOUT = 120
CHECK_POLL_S = 30
CHECK_WAIT_S = 1800
SLEEP = time.sleep
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
REACT_MUTATION = """
mutation($subject: ID!) {
  addReaction(input: {subjectId: $subject, content: EYES}) {
    reaction { content }
  }
}"""
OPEN_PULL_QUERY = """
query($owner: String!, $name: String!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $branch, states: OPEN, first: 1) {
      nodes { url }
    }
  }
}"""


class MergeRefused(Exception):
    pass


@dataclass(frozen=True)
class PullRequest:
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
    author: str
    body: str
    author_kind: str = "unknown"
    node_id: str = ""
    acknowledged: bool = False


@dataclass(frozen=True)
class Thread:
    id: str
    path: str
    line: int | None
    author: str
    body: str
    url: str
    outdated: bool = False
    replies: tuple = ()
    # Unknown authors (including deleted accounts) are treated as people.
    author_kind: str = "unknown"
    kind: str = "review"
    classification: str = ""
    request: str = ""
    intent: str = "unmarked"
    triage: dict | None = None
    node_id: str = ""
    acknowledged: bool = False

    @property
    def comments(self):
        return (Comment(self.author, self.body, self.author_kind, self.node_id,
                        self.acknowledged),) + self.replies


@dataclass(frozen=True)
class PrState:
    """UNKNOWN `mergeable` is never a license to merge."""

    threads: tuple
    checks: str  # "success", "pending" or "failure"
    head_sha: str | None
    merged: bool = False
    merge_sha: str | None = None
    closed_by: str | None = None
    closed: bool = False
    mergeable: str = "UNKNOWN"
    updated_at: int | None = None
    pending_contexts: tuple = ()
    failed_checks: tuple = ()
    missing_checks: tuple = ()


def origin_url(target):
    r = subprocess.run(["git", "remote", "get-url", REMOTE], cwd=target.path,
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def token_from_env(environ=None):
    """The value goes to the request header and nowhere else."""
    environ = os.environ if environ is None else environ
    for name in TOKEN_VARS:
        value = environ.get(name, "").strip()
        if value:
            return value
    return None


def check_pr_route(target):
    """`gh auth status` probes, never writes; with no `gh` no token is sent."""
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
    return f"{task_id}: {task}"


def pr_body_stub(task, reason, issue_url):
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


# GitHub truncates past 256; a title longer than this is a paragraph.
PR_TITLE_MAX = 120


def parse_pr_text(reply):
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
    body = (body or "").strip()
    link = f"Linear: {task_id}"
    if issue_url:
        link = f"{link} ({issue_url})"
    return f"{body}\n\n{link}" if body else link


def split_pr_body(body):
    """The first HTML comment after Linear always starts externally owned text."""
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
    _, link, evidence, tail = split_pr_body(body)
    before_linear = evidence and body.index(evidence) < body.index(link)
    preserved = evidence + link if before_linear else link + evidence
    return text.rstrip() + ("\n\n" if link else "") + preserved + tail


def replace_pr_evidence(body, section):
    own, link, evidence, tail = split_pr_body(body)
    section = section.rstrip()
    if not evidence:
        if not link:
            return f"{body.rstrip()}\n\n{section}"
        return (own.rstrip() + "\n\n" if own.strip() else "") + (
            f"{section}\n\n{link}{tail}")
    start = body.index(evidence)
    space = evidence[len(evidence.rstrip()):]
    return body[:start] + section + space + body[start + len(evidence):]


def edit_pr_body(target, pull, body):
    body = outbound(body, known_secrets(target.config()))
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
    from holophyte.commit_hygiene import strip_attribution
    from holophyte.environment_git import refuse_environment_history

    strip_attribution(target, target.path, branch)
    checked = refuse_environment_history(target, branch, action="push")
    refspec = f"{checked}:refs/heads/{branch}" if checked != branch else branch
    try:
        r = subprocess.run(["git", "push", REMOTE, refspec], cwd=target.path,
                           capture_output=True, text=True, timeout=PR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InfraFailure(f"git push {REMOTE} {branch} did not answer in "
                           f"{PR_TIMEOUT}s; branch preserved") from None
    if r.returncode:
        detail = " ".join((r.stderr or r.stdout).split())[-500:]
        raise InfraFailure(f"git push {REMOTE} {branch} failed: {detail};"
                           " branch preserved, no pull request opened")


def create_pull_request(target, branch, title, body):
    secrets = known_secrets(target.config())
    title, body = outbound(title, secrets), outbound(body, secrets)
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
    """Without `--repo`, `gh` opens the PR in its own default repository."""
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
        # The answer names the refusal; the token's header is not in it.
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
    if not url:
        return None
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _url_in(text):
    """`gh pr create` prints progress first and the PR URL last."""
    urls = re.findall(r"https://\S+", text or "")
    return urls[-1] if urls else None


def open_pull_request(target, branch):
    # Imported at the call: `pr_status` imports this module at its top.
    from holophyte.pr.pr_status import parse_pr_url
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
    # Imported at the call: `pr_status` imports this module at its top.
    from holophyte.pr.pr_status import parse_pr_url
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
    return value if isinstance(value, int) and not isinstance(value, bool) \
        and value >= 0 else None


def _comment_url(node):
    nodes = (node.get("comments") or {}).get("nodes") or ()
    first = next((c for c in nodes if isinstance(c, dict)), None)
    return first.get("url") if first else None


def acknowledged(node):
    groups = node.get("reactionGroups") if isinstance(node, dict) else None
    return any(isinstance(g, dict) and g.get("content") == "EYES"
               and g.get("viewerHasReacted") is True for g in groups or ())


def react_eyes(target, pull, node_id):
    graphql(target, pull, REACT_MUTATION, {"subject": node_id})


def reply_thread(target, pull, thread_id, body):
    graphql(target, pull, REPLY_MUTATION, {"thread": thread_id, "body": body})


def comment_on_pull(target, pull, body):
    rest(target, pull, "POST",
         f"repos/{pull.repo}/issues/{pull.number}/comments", {"body": body})


def resolve_thread(target, pull, thread_id):
    graphql(target, pull, RESOLVE_MUTATION, {"thread": thread_id})


def merge_pull_request(target, pull, sha):
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
        # `_call` folds every non-2xx into one exception: read the status off it.
        if re.search(r"\b(405|409|422)\b", str(e)):
            raise MergeRefused(str(e)) from None
        raise
    sha = answer.get("sha") if isinstance(answer, dict) else None
    if not sha or not answer.get("merged"):
        raise MergeRefused(f"GitHub did not merge {pull.url}:"
                           f" {_short(answer)}")
    return sha


def graphql(target, pull, query, variables):
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
    return _call(target, pull.host, method, path, payload)


def job_log(target, pull, job_id):
    """The endpoint redirects to plain text, not JSON."""
    path = f"repos/{pull.owner}/{pull.name}/actions/jobs/{job_id}/logs"
    if shutil.which(GH) is not None:
        return _gh_output(target, pull.host, "GET", path, None)
    return _api_output(pull.host, "GET", path, None, _route_token())


def _call(target, host, method, path, payload):
    if shutil.which(GH) is not None:
        return _call_with_gh(target, host, method, path, payload)
    return _call_with_api(host, method, path, payload, _route_token())


def _route_token():
    token = token_from_env()
    if token is None:
        raise InfraFailure(f"no {GH!r} on PATH and no "
                           f"{' or '.join(TOKEN_VARS)} in the environment")
    return token


def _call_with_gh(target, host, method, path, payload):
    stdout = _gh_output(target, host, method, path, payload)
    try:
        return json.loads(stdout) if stdout.strip() else {}
    except ValueError:
        raise InfraFailure(f"{GH} api {path} answered something that is not"
                           f" JSON: {_short(stdout)}") from None


def _gh_output(target, host, method, path, payload):
    deadline.admit(f"GitHub's {method} {path} request")
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
    return r.stdout


def _call_with_api(host, method, path, payload, token):
    text = _api_output(host, method, path, payload, token)
    try:
        return json.loads(text) if text.strip() else {}
    except ValueError:
        raise InfraFailure(f"GitHub answered {method} {path} with something"
                           f" that is not JSON: {_short(text)}") from None


class _TokenStaysHome(urllib.request.HTTPRedirectHandler):
    """The job-log redirect leaves the API host: the token stays behind."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Past the host sweep's bound, the redirect is refused like the first.
        deadline.admit(f"GitHub's redirect to"
                       f" {urllib.parse.urlsplit(newurl).netloc}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).netloc
                                != urllib.parse.urlsplit(req.full_url).netloc):
            new.remove_header("Authorization")
        return new


def _api_output(host, method, path, payload, token):
    deadline.admit(f"GitHub's {method} {path} request")
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
        opener = urllib.request.build_opener(_TokenStaysHome)
        with opener.open(request, timeout=PR_TIMEOUT) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = " ".join(e.read().decode("utf-8", "replace").split())[-500:]
        raise InfraFailure(f"GitHub refused {method} {path} ({e.code}):"
                           f" {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise InfraFailure(f"GitHub did not answer {method} {path}:"
                           f" {e}") from None


def _short(value):
    text = value if isinstance(value, str) else json.dumps(value)
    return " ".join(text.split())[:300]
