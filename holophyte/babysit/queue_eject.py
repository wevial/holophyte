"""Tell a merge-queue ejection's infra failure from a test failure."""
import re
from dataclasses import dataclass

import store
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import github

INFRA, PARK, TEST = "infra", "park", "test"
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
ANNOTATIONS_PAGE = 100


@dataclass(frozen=True)
class Verdict:
    kind: str
    line: str = ""


def classify(target, pull, failed):
    """Every failed check is read: a park signature anywhere wins."""
    merge = merge_config(target)
    texts = [(check, _lines(target, pull, check)) for check in failed]
    for _, lines in texts:
        line = _first_match(merge.queue_park_signatures, lines)
        if line:
            return Verdict(PARK, line)
    if all(check.conclusion == "cancelled"
           or _first_match(merge.queue_infra_signatures, lines)
           for check, lines in texts):
        return Verdict(INFRA)
    return Verdict(TEST)


def _first_match(patterns, lines):
    compiled = [re.compile(p) for p in patterns]
    return next((line for line in lines
                 if any(p.search(line) for p in compiled)), "")


def _lines(target, pull, check):
    try:
        log = github.job_log(target, pull, check.job_id)
    except InfraFailure:
        log = ""
    lines = [_STAMP.sub("", line).strip() for line in log.splitlines()]
    return [line for line in lines + _annotations(target, pull, check) if line]


def _annotations(target, pull, check):
    base = (f"repos/{pull.repo}/check-runs/{check.job_id}/annotations"
            f"?per_page={ANNOTATIONS_PAGE}")
    notes, page = [], 1
    while True:
        try:
            answer = github.rest(target, pull, "GET", f"{base}&page={page}")
        except InfraFailure:
            break
        if not isinstance(answer, list):
            break
        notes.extend(note for note in answer if isinstance(note, dict))
        if len(answer) < ANNOTATIONS_PAGE:
            break
        page += 1
    return [line.strip() for note in notes for field in ("title", "message")
            for line in str(note.get(field) or "").splitlines()]


def requeue_or_park(run, beat_s, pull, removed, requeued, reviewed):
    """The ejections re-queued so far, this one added; None for a test failure."""
    with heartbeat_while(run.conn, run.run_id, beat_s):
        verdict = classify(run.project, pull, removed.failed)
    names = ", ".join(check.name for check in removed.failed)
    if verdict.kind == PARK:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request: {names} failed on"
              f" merge group {removed.group[:12]} with a problem a re-queue"
              f" cannot fix ([merge] queue_park_signatures): {verdict.line}")
    if verdict.kind == TEST:
        return None
    failed = {check.name for check in removed.failed}
    earlier = next((e for e in requeued
                    if {c.name for c in e.failed} == failed), None)
    if earlier is not None:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request again for infra:"
              f" {names} failed on merge group {earlier.group[:12]}, was"
              f" re-queued once, and failed the same way on merge group"
              f" {removed.group[:12]}")
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, "merge_queue",
                           f"{names} failed on merge group"
                           f" {removed.group[:12]} for infra; re-queuing"
                           f" {pull.url} once without a fix round")
    return (*requeued, removed)


def _park(run, pull, reviewed, why):
    from holophyte.pr.pullrequest import _park_on_pr
    _park_on_pr(run.project, run.conn, run.run_id, run.provider, run.task_id,
                run.branch, run.sha, pull, why, (), reviewed=reviewed)
