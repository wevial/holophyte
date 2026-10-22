"""A GitHub failure is recorded on the run, never undone into the store."""
import json
from urllib.parse import quote

import store
from holophyte.loop.gates import InfraFailure
from holophyte.pr.github import rest
from holophyte.pr.pr_status import parse_pr_url
from holophyte.redact import safe_print as print

LABEL = "holophyte:paused"
KIND = "pause_notice"


def mark(target, conn, run_id):
    (pr_url,) = conn.execute("SELECT prUrl FROM runs WHERE id = ?",
                             (run_id,)).fetchone()
    pull = _pull(conn, run_id, pr_url)
    if pull is None:
        return
    issue = f"repos/{pull.repo}/issues/{pull.number}"
    _attempt(conn, run_id, "label", lambda: rest(
        target, pull, "POST", f"{issue}/labels", {"labels": [LABEL]}))


def unmark(target, conn, run_id):
    (pr_url,) = conn.execute("SELECT prUrl FROM runs WHERE id = ?",
                             (run_id,)).fetchone()
    pull = _pull(conn, run_id, pr_url)
    if pull is None:
        return
    posted = conn.execute(
        "SELECT json_extract(payload, '$.commentId') FROM runEvents"
        " WHERE runId = ? AND kind = ? AND json_extract(payload, '$.commentId')"
        " IS NOT NULL ORDER BY id DESC LIMIT 1", (run_id, KIND)).fetchone()
    if posted is not None:
        _attempt(conn, run_id, "delete comment", lambda: rest(
            target, pull, "DELETE",
            f"repos/{pull.repo}/issues/comments/{posted[0]}"))
    _attempt(conn, run_id, "unlabel", lambda: rest(
        target, pull, "DELETE",
        f"repos/{pull.repo}/issues/{pull.number}/labels/{quote(LABEL, safe='')}"))


def _pull(conn, run_id, pr_url):
    if not pr_url:
        return None
    pull = parse_pr_url(pr_url)
    if pull is None:
        _failed(conn, run_id, "read pull request", f"unreadable url {pr_url}")
    return pull


def _attempt(conn, run_id, step, call):
    try:
        return call()
    except InfraFailure as refused:
        _failed(conn, run_id, step, str(refused))
        return None


def _failed(conn, run_id, step, error):
    store.record_event(conn, run_id, KIND, f"pause notice {step} failed: {error}",
                       level="detail",
                       payload=json.dumps({"step": step, "error": error}))
    print(f"[holo2] run {run_id}: pause notice {step} failed: {error}")
