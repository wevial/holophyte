from __future__ import annotations

import fcntl
from functools import partial

import store
from holophyte.admission import project_of
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import open_store
from holophyte.pr import github
from holophyte.pr.merge_queue import (
    GRAPHQL_REFUSED,
    dequeue_pull_request,
    enqueue_pull_request,
)
from holophyte.pr.pr_status import parse_pr_url
from holophyte.serve.serve_levers import console_author
from holophyte.serve.serve_merge_queue import CACHE_FILE, read_merge_queue
from holophyte.serve.serve_runs import no_store

QUEUE_JUMP_ACTION = "queue-jump"
QUEUE_REMOVE_ACTION = "queue-remove"



def queue_action(project, body, action):
    number = body.get("pr")
    if type(number) is not int or not 0 < number < 2**63:
        return 400, {"error": "pr must be a positive integer"}
    if not project.store_path.exists():
        return 503, no_store(project)
    jump = action == QUEUE_JUMP_ACTION
    entry, refused = queued_entry(read_merge_queue(project), number, jump)
    if refused is not None:
        reason, detail = refused
        return 200, {"action": action, "ok": False, "pr": number,
                     "reason": reason, "detail": detail, "recorded": None}
    verb = "jump" if jump else "remove"
    recorded = record(project, entry["run"], verb, number, body)
    answer = {"action": action, "pr": number, "recorded": recorded}
    if recorded is None:
        return 200, {**answer, "ok": False, "reason": "unrecorded",
                     "detail": "the store holds no"
                     " project to record the intervention against; nothing"
                     " sent"}
    pull = parse_pr_url(entry["pr"]["url"])
    try:
        if jump:
            enqueue_pull_request(project, pull, entry["pr"]["head_oid"],
                                 jump=True)
        else:
            dequeue_pull_request(project, pull)
    except github.MergeRefused as no:
        return 200, {**answer, "ok": False, "reason": "refused",
                     "detail": str(no).removeprefix(GRAPHQL_REFUSED)}
    except InfraFailure as failed:
        return 200, {**answer, "ok": False, "reason": "failed",
                     "detail": str(failed)}
    forget_cached_queue(project)
    done = ("jumped to the front of" if jump else "removed from")
    return 200, {**answer, "ok": True,
                 "detail": f"#{number} {done} the merge queue"}


def queued_entry(queue, number, jump):
    if "error" in queue:
        return None, ("unread",
                      f"the merge queue was not read: {queue['error']}")
    entry = next((e for e in queue["entries"]
                  if e["pr"]["number"] == number), None)
    if entry is None:
        return None, ("not_queued", f"#{number} is not in the merge queue")
    run = entry["run"]
    if jump and entry["position"] == 1:
        return entry, ("at_front",
                       f"#{number} is already at the front of the queue")
    if not jump and run is not None:
        return entry, ("factory_owned", (
            f"#{number} is run {run['id']}'s pull request ({run['ticket']});"
            " pause or abort the run instead"))
    return entry, None


def record(project, run, verb, number, body):
    """An action that cannot be recorded does not run."""
    note = f"{console_author(body)} via the console: {verb} #{number}"
    extra = body.get("note")
    if isinstance(extra, str) and extra.strip():
        note += f"; {extra.strip()}"
    action = f"queue_{verb}"
    conn = open_store(project)
    try:
        if run is not None:
            return store.record_intervention(conn, run["id"], action, note,
                                             source="human", trigger="manual")
        key = project_of(conn, project)
        return None if key is None else store.record_project_intervention(
            conn, action, note, source="human", trigger="manual",
            project_id=key)
    finally:
        conn.close()


def forget_cached_queue(project):
    path = project.holo_dir / CACHE_FILE
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path.unlink(missing_ok=True)


QUEUE_ACTIONS = {action: partial(queue_action, action=action)
                 for action in (QUEUE_JUMP_ACTION, QUEUE_REMOVE_ACTION)}
