"""`[merge] mode = "pr"`: the loop's one GitHub surface.

Design note 7's first half. Instead of the `--no-ff` merge into main, an
approved, verified candidate is pushed to `origin` and opened as a pull
request whose body is the ticket body plus the run's FINDINGS entry, so the
repository's own review bots and CI see the change before it lands; the loop
then parks the run exactly as `[merge] approve = "human"` does, with the PR's
URL on the run (`runs.prUrl`) and in the question the ticket asks. Reading
review threads, waiting for CI and merging the PR are the second half and are
not here.

Everything that talks to GitHub is in this file, so the surface is one seam
to port: `check_pr_route()` is the startup preflight, `push_branch()` and
`create_pull_request()` the two calls the merge path makes, `pr_body()` the
body they carry. The route is `gh` on PATH, authenticated, or -- with no `gh`
-- the REST API with a token read from `TOKEN_VARS` in the environment. The
token is read there and only there: never written to the config, the store or
a log line.

Failures on the route are `InfraFailure`: a push refused, a PR create that
did not answer, a `gh` gone missing since startup say nothing about the
ticket, so they spend none of its strikes, and every one of them leaves the
branch and worktree as they were.
"""
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

import store.read
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
