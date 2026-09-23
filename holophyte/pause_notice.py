"""A paused run's pull request says so: the `holophyte:paused` label and one
comment naming who paused it, why, and how to resume (KO-608).

Both run after the pause or resume has committed; a GitHub failure is
recorded on the run as a `pause_notice` event and printed, never undone
into the store.
"""
import json
from urllib.parse import quote

import store
from holophyte.babysitter import COMMENT_HEADER
from holophyte.gates import InfraFailure
from holophyte.pr import rest
from holophyte.pr_status import parse_pr_url
from holophyte.redact import safe_print as print

LABEL = "holophyte:paused"
KIND = "pause_notice"


def body(identifier, who, note, boundary):
    """The comment: the factory's header, who paused the run and why, where
    it stopped, and the two ways back."""
    return (f"{COMMENT_HEADER.format(model='holophyte')}\n\n"
            f"**Paused.** The factory has stopped working on this pull request:"
            f" {who} paused {identifier} at its `{boundary}` boundary.\n\n"
            f"Note: {note or '(none recorded)'}\n\n"
            f"To resume, run `factory.py PROJECT --resume {identifier} --note TEXT`"
            " on the writer host, or press Resume on the ticket in the console."
            f" Resuming removes the `{LABEL}` label and this comment.")


def mark(target, conn, run_id, boundary):
    """Label the paused run's pull request and post the notice; record the
    comment id for `unmark()`."""
    identifier, pr_url, source, note = conn.execute(
        "SELECT t.linearIdentifier, r.prUrl, i.source, i.guidance FROM runs r"
        " JOIN tickets t ON t.id = r.ticketId"
        " LEFT JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
        (run_id,)).fetchone()
    pull = _pull(conn, run_id, pr_url)
    if pull is None:
        return
    issue = f"repos/{pull.repo}/issues/{pull.number}"
    _attempt(conn, run_id, "label", lambda: rest(
        target, pull, "POST", f"{issue}/labels", {"labels": [LABEL]}))
    who = "the supervisor" if source == "supervisor" else "the operator"
    answer = _attempt(conn, run_id, "comment", lambda: rest(
        target, pull, "POST", f"{issue}/comments",
        {"body": body(identifier, who, note, boundary)}))
    if answer is not None:
        comment = answer.get("id") if isinstance(answer, dict) else None
        store.record_event(conn, run_id, KIND, f"posted pause comment {comment}",
                           level="detail", payload=json.dumps({"commentId": comment}))


def unmark(target, conn, run_id):
    """Delete the paused run's notice comment and remove its label."""
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
    """The run's pull request, or None: no URL is no notice, an unreadable
    one is a recorded failure."""
    if not pr_url:
        return None
    pull = parse_pr_url(pr_url)
    if pull is None:
        _failed(conn, run_id, "read pull request", f"unreadable url {pr_url}")
    return pull


def _attempt(conn, run_id, step, call):
    """`call()`'s answer, or None with the failure recorded and printed."""
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
