"""The branch held to origin's copy and to the candidate a park recorded."""
import subprocess

import store
from holophyte.loop.gates import RunFailure, sh
from holophyte.pr import pr_status
from holophyte.pr.pullrequest import _park_on_pr
from holophyte.redact import safe_print as print


def _sync_branch_from_origin(project, conn, run_id, provider, task_id,
                             branch, wt, url=None, reviewed=None,
                             diverged=None):
    """A divergence parks on `url`'s pull request, or fails with `diverged`."""
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    fetched = subprocess.run(["git", "fetch", "origin", branch], cwd=wt,
                             capture_output=True, text=True)
    if fetched.returncode != 0:
        print(f"[holo2] could not fetch {branch} from origin; working from"
              f" the local branch at {sha[:12]}:"
              f" {fetched.stderr.strip() or fetched.stdout.strip()}")
        return sha
    remote = sh(["git", "rev-parse", "FETCH_HEAD"], cwd=wt)

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=wt, capture_output=True).returncode == 0

    if remote == sha or is_ancestor(remote, sha):
        return sha
    if not is_ancestor(sha, remote):
        if diverged is not None:
            raise RunFailure(diverged.format(branch=branch, local=sha,
                                             remote=remote))
        pull = pr_status.parse_pr_url(url)
        if pull is None:
            raise RunFailure(f"cannot read a pull request off {url!r};"
                             f" branch {branch} preserved at {sha[:12]}")
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    f"the local branch {branch} at {sha[:12]} and origin's at"
                    f" {remote[:12]} diverged; neither fast-forwards to the"
                    " other, so nothing was fetched into the worktree and"
                    " a human reconciles them", (), reviewed=reviewed)
    count = sh(["git", "rev-list", "--count", f"{sha}..{remote}"], cwd=wt)
    sh(["git", "merge", "--ff-only", remote], cwd=wt)
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    note = (f"Fast-forwarded {branch} to {sha} from origin ({count}"
            f" commit(s) pushed by someone else)")
    print(f"[holo2] {note}")
    if conn is not None and run_id is not None:
        store.record_ledger(conn, run_id, "note", note)
    return sha


def _candidate_drift(wt, branch, approved):
    if approved is None:
        return ("the park recorded no candidate sha, so nothing vouches for"
                f" what {branch} now holds")
    dirty = sh(["git", "status", "--porcelain"], cwd=wt)
    if dirty:
        return (f"the worktree holds uncommitted changes on top of"
                f" {approved[:12]}:\n{dirty}")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if head != approved:
        return f"the worktree is at {head[:12]}, not {approved[:12]}"
    tip = sh(["git", "rev-parse", "--verify", "--quiet",
              f"refs/heads/{branch}"], cwd=wt)
    if tip != approved:
        return f"branch {branch} is at {tip[:12]}, not {approved[:12]}"
    return None
