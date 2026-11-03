"""Tell a merge-queue ejection's infra failure from a test failure."""
import json
import re

import store
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import github
from holophyte.redact import redact_values

INFRA, PARK, TEST, UNREADABLE = "infra", "park", "test", "unreadable"
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
ANNOTATIONS_PAGE = 100
REQUEUE_EVENT = "queue_infra_requeue"


def classify(target, pull, failed):
    """A park signature on a readable check wins; an unread check is never empty."""
    merge = merge_config(target)
    texts = [(check, _lines(target, pull, check)) for check in failed]
    for _, lines in texts:
        line = _first_match(merge.queue_park_signatures, lines or ())
        if line:
            return PARK, line
    if any(lines is None for _, lines in texts):
        return UNREADABLE, ""
    if all(check.conclusion == "cancelled"
           or _first_match(merge.queue_infra_signatures, lines)
           for check, lines in texts):
        return INFRA, ""
    return TEST, ""


def _first_match(patterns, lines):
    compiled = [re.compile(p) for p in patterns]
    return next((line for line in lines
                 if any(p.search(line) for p in compiled)), "")


def _lines(target, pull, check):
    try:
        log = github.job_log(target, pull, check.job_id)
        notes = _annotations(target, pull, check)
    except InfraFailure:
        return None
    lines = [_STAMP.sub("", line).strip() for line in log.splitlines()]
    return [line for line in lines + notes if line]


def _annotations(target, pull, check):
    base = (f"repos/{pull.repo}/check-runs/{check.job_id}/annotations"
            f"?per_page={ANNOTATIONS_PAGE}")
    notes, page = [], 1
    while True:
        answer = github.rest(target, pull, "GET", f"{base}&page={page}")
        if not isinstance(answer, list):
            break
        notes.extend(note for note in answer if isinstance(note, dict))
        if len(answer) < ANNOTATIONS_PAGE:
            break
        page += 1
    return [line.strip() for note in notes
            for field in ("title", "message", "raw_details")
            for line in str(note.get(field) or "").splitlines()]


def requeue_or_park(run, beat_s, pull, removed, requeued, reviewed):
    """The ejections re-queued so far, this one added; None for a test failure."""
    with heartbeat_while(run.conn, run.run_id, beat_s):
        kind, line = classify(run.project, pull, removed.failed)
    names = ", ".join(check.name for check in removed.failed)
    if kind == PARK:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request: {names} failed on"
              f" merge group {removed.group[:12]} with a problem a re-queue"
              f" cannot fix ([merge] queue_park_signatures):"
              f" {redact_values(line)}")
    if kind == TEST:
        return None
    failed = sorted({check.name for check in removed.failed})
    cause = ("for infra" if kind == INFRA
             else "and the failed checks' evidence could not be read")
    persisted = run.conn is not None and run.run_id is not None
    history = _requeued(run) if persisted else requeued
    earlier = next((group for names_of, group in history
                    if names_of == failed), None)
    if earlier is not None:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request again: {names}"
              f" failed on merge group {earlier[:12]}, was re-queued once,"
              f" and failed the same way on merge group"
              f" {removed.group[:12]} {cause}")
    if persisted:
        store.record_event(run.conn, run.run_id, "merge_queue",
                           f"{names} failed on merge group"
                           f" {removed.group[:12]} {cause}; re-queuing"
                           f" {pull.url} once without a fix round")
        store.record_event(run.conn, run.run_id, REQUEUE_EVENT,
                           f"re-queued after merge group {removed.group[:12]}",
                           level="detail", payload=json.dumps(
                               {"names": failed, "group": removed.group}))
    return (*requeued, (failed, removed.group))


def _requeued(run):
    rows = run.conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
        " ORDER BY id", (run.run_id, REQUEUE_EVENT)).fetchall()
    return [(record["names"], record["group"])
            for record in (json.loads(row[0]) for row in rows)]


def _park(run, pull, reviewed, why):
    from holophyte.pr.pullrequest import _park_on_pr
    _park_on_pr(run.project, run.conn, run.run_id, run.provider, run.task_id,
                run.branch, run.sha, pull, why, (), reviewed=reviewed)
