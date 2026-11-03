"""Tell a merge-queue ejection's infra failure from a test failure."""
import json
import re
from time import monotonic

import store
from holophyte.babysit.check_failures import failure_lines, normalized
from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import heartbeat_while, warn_on_run
from holophyte.loop.stop import stop_if_requested
from holophyte.pr import github, pr_status
from holophyte.pr.merge_queue import RUNS_PAGE
from holophyte.redact import redact_values

INFRA, PARK, TEST, UNREADABLE = "infra", "park", "test", "unreadable"
_SPECS = (re.compile(r"(?:FAIL|ERROR): (\S+ \(\S+\))"),
          re.compile(r"(?:FAILED|ERROR) (\S*::\S*)"),
          re.compile(r"\d+\) (\[[^\]]+\] › .*?)[\s─]*$"))
_STAMP = re.compile(r"^\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
ANNOTATIONS_PAGE = 100
REQUEUE_EVENT = "queue_infra_requeue"
UNREPRODUCED_EVENT = "queue_test_requeue"
LABEL_EVENT = "queue_full_label"
PULL_EVENTS = ("pull_request", "pull_request_target")


def classify(target, pull, failed):
    """A park signature on a readable check wins; an unread check is never empty."""
    merge = merge_config(target)
    texts = [(check, *_lines(target, pull, check)) for check in failed]
    for _, lines, _ in texts:
        line = _first_match(merge.queue_park_signatures, lines)
        if line:
            return PARK, line, []
    if not all(read for _, _, read in texts):
        return UNREADABLE, "", []
    if all(check.conclusion == "cancelled"
           or _first_match(merge.queue_infra_signatures, lines)
           for check, lines, _ in texts):
        return INFRA, "", []
    specs = {_spec(line) for _, lines, _ in texts
             for line in failure_lines("\n".join(lines))} - {""}
    return TEST, "", sorted(specs) or sorted({check.name for check in failed})


def _spec(line):
    match = next(filter(None, (p.match(line) for p in _SPECS)), None)
    return normalized(match[1]) if match else ""


def _first_match(patterns, lines):
    compiled = [re.compile(p) for p in patterns]
    return next((line for line in lines
                 if any(p.search(line) for p in compiled)), "")


def _lines(target, pull, check):
    try:
        log = github.job_log(target, pull, check.job_id)
    except InfraFailure:
        return [], False
    lines = [_STAMP.sub("", line).strip() for line in log.splitlines()]
    try:
        notes = _annotations(target, pull, check)
    except InfraFailure:
        return [line for line in lines if line], False
    return [line for line in lines + notes if line], True


def _annotations(target, pull, check):
    base = (f"repos/{pull.repo}/check-runs/{check.job_id}/annotations"
            f"?per_page={ANNOTATIONS_PAGE}")
    notes, page = [], 1
    while True:
        answer = github.rest(target, pull, "GET", f"{base}&page={page}")
        if not isinstance(answer, list):
            raise InfraFailure(f"annotations page {page} of check run"
                               f" {check.job_id} did not return a list")
        notes.extend(note for note in answer if isinstance(note, dict))
        if len(answer) < ANNOTATIONS_PAGE:
            break
        page += 1
    return [line.strip() for note in notes
            for field in ("title", "message", "raw_details")
            for line in str(note.get(field) or "").splitlines()]


def requeue_or_park(run, beat_s, pull, removed, requeued, reviewed):
    """The ejections re-queued, this one added, or the head commit's failures."""
    with heartbeat_while(run.conn, run.run_id, beat_s):
        kind, line, specs = classify(run.project, pull, removed.failed)
    names = ", ".join(check.name for check in removed.failed)
    if kind == PARK:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request: {names} failed on"
              f" merge group {removed.group[:12]} with a problem a re-queue"
              f" cannot fix ([merge] queue_park_signatures):"
              f" {redact_values(line)}")
    if kind == TEST:
        return _reproduce(run, beat_s, pull, removed, specs, requeued,
                          reviewed)
    failed = sorted({check.name for check in removed.failed})
    cause = ("for infra" if kind == INFRA
             else "and the failed checks' evidence could not be read")
    earlier = _earlier(run, requeued, INFRA, REQUEUE_EVENT, failed)
    if earlier is not None:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request again: {names}"
              f" failed on merge group {earlier[:12]}, was re-queued once,"
              f" and failed the same way on merge group"
              f" {removed.group[:12]} {cause}")
    _record_requeue(run, REQUEUE_EVENT, failed, removed.group,
                    f"{names} failed on merge group {removed.group[:12]}"
                    f" {cause}; re-queuing {pull.url} once without a fix"
                    " round")
    return (*requeued, (INFRA, failed, removed.group)), None


def _reproduce(run, beat_s, pull, removed, specs, requeued, reviewed):
    shown = redact_values(specs[0] + (f" and {len(specs) - 1} more"
                                      if len(specs) > 1 else ""))
    earlier = _earlier(run, requeued, TEST, UNREPRODUCED_EVENT, specs)
    if earlier is not None:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request again: {shown}"
              f" failed on merge group {earlier[:12]}, did not reproduce in"
              f" the full run on the head commit, was re-queued once, and"
              f" failed the same way on merge group {removed.group[:12]}")
    label = merge_config(run.project).queue_full_label
    latest = _latest_full_run(run, beat_s, pull, reviewed,
                              f"a test failure ({shown}) ejected it")
    _label_event(run.conn, run.run_id, label, True,
                 f"labelling the pull request {label}")
    try:
        with heartbeat_while(run.conn, run.run_id, beat_s):
            github.add_label(run.project, pull, label)
            github.rerun_workflow_run(run.project, pull, latest["id"])
    except InfraFailure as refused:
        _park(run, pull, reviewed,
              f"the merge queue removed the pull request for a test failure"
              f" ({shown}), and labelling it {label} to re-run workflow run"
              f" {latest['id']} on the head commit failed: {refused}")
    _record(run, f"{shown} failed on merge group {removed.group[:12]};"
                 f" labelled {pull.url} {label} and re-running workflow run"
                 f" {latest['id']} on the head commit {run.sha[:12]}")
    if _awaited(run, beat_s, pull, reviewed, latest,
                (latest.get("run_attempt") or 1) + 1) != "success":
        return requeued, _head_failures(run, beat_s, pull, latest["id"])
    _record_requeue(run, UNREPRODUCED_EVENT, specs, removed.group,
                    f"the failure on merge group {removed.group[:12]} did not"
                    f" reproduce: workflow run {latest['id']} passed on the"
                    f" head commit {run.sha[:12]}; re-queuing {pull.url}"
                    " once")
    _drop_or_park(run, pull, reviewed, label)
    return (*requeued, (TEST, specs, removed.group)), None


def labelled_full_run(run, beat_s, pull, reviewed):
    label = _label_on(run.conn, run.run_id)
    if label is None:
        return None
    latest = _latest_full_run(run, beat_s, pull, reviewed,
                              f"it carries {label}")
    if _awaited(run, beat_s, pull, reviewed, latest,
                latest.get("run_attempt") or 1) != "success":
        return _head_failures(run, beat_s, pull, latest["id"])
    _record(run, f"workflow run {latest['id']} passed on the head commit"
                 f" {run.sha[:12]}; removing {label} before re-queuing")
    _drop_or_park(run, pull, reviewed, label)
    return None


def _drop_or_park(run, pull, reviewed, label):
    if not drop_label(run.project, run.conn, run.run_id, pull, label):
        _park(run, pull, reviewed,
              f"removing {label} from the pull request failed, so it is not"
              " re-queued with every later run of it a full one")


def unlabel_ended_run(target, conn, run_id):
    label = _label_on(conn, run_id)
    if label is None:
        return
    ended, pr_url = conn.execute(
        "SELECT endedAt, prUrl FROM runs WHERE id = ?", (run_id,)).fetchone()
    pull = pr_status.parse_pr_url(pr_url)
    if ended is not None and pull is not None:
        drop_label(target, conn, run_id, pull, label)


def drop_label(target, conn, run_id, pull, label):
    try:
        github.remove_label(target, pull, label)
    except InfraFailure as refused:
        warn_on_run(conn, run_id, f"removing {label} from {pull.url}"
                                  f" failed: {refused}")
        return False
    _label_event(conn, run_id, label, False, f"removed {label} from {pull.url}")
    return True


def _label_on(conn, run_id):
    if conn is None or run_id is None:
        return None
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
        " ORDER BY id DESC LIMIT 1", (run_id, LABEL_EVENT)).fetchone()
    record = json.loads(row[0]) if row else {}
    return record["label"] if record.get("on") else None


def _label_event(conn, run_id, label, on, summary):
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, LABEL_EVENT, summary, level="detail",
                           payload=json.dumps({"label": label, "on": on}))


def _latest_full_run(run, beat_s, pull, reviewed, why):
    workflow, refused = merge_config(run.project).queue_full_workflow, ""
    try:
        with heartbeat_while(run.conn, run.run_id, beat_s):
            every = _head_runs(run, pull)
    except InfraFailure as failure:
        every, refused = [], f": {failure}"
    runs = [r for r in every if r.get("name") == workflow
            and isinstance(r.get("id"), int) and r.get("event") in PULL_EVENTS]
    if not runs:
        _park(run, pull, reviewed,
              f"the pull request is due a full run because {why}, and no run"
              f" of [merge] queue_full_workflow = {workflow!r} on the head"
              f" commit {run.sha[:12]} was found to re-run{refused}")
    return max(runs, key=lambda r: r["id"])


def _head_runs(run, pull):
    base = (f"repos/{pull.repo}/actions/runs?head_sha={run.sha}"
            f"&per_page={RUNS_PAGE}")
    runs, page = [], 1
    while True:
        answer = github.rest(run.project, pull, "GET", f"{base}&page={page}")
        batch = answer.get("workflow_runs") if isinstance(answer, dict) else None
        if not isinstance(batch, list):
            raise InfraFailure(f"workflow runs page {page} of {run.sha[:12]}"
                               " did not return a list")
        runs.extend(r for r in batch if isinstance(r, dict))
        if len(batch) < RUNS_PAGE:
            return runs
        page += 1


def _awaited(run, beat_s, pull, reviewed, latest, attempt):
    wait = merge_config(run.project).check_wait_sec
    deadline = monotonic() + wait
    while True:
        try:
            with heartbeat_while(run.conn, run.run_id, beat_s):
                read = github.rest(run.project, pull, "GET",
                                   f"repos/{pull.repo}/actions/runs"
                                   f"/{latest['id']}")
        except InfraFailure:
            read = {}
        if isinstance(read, dict) and read.get("status") == "completed" \
                and (read.get("run_attempt") or 1) >= attempt:
            return read.get("conclusion")
        remaining = deadline - monotonic()
        if remaining <= 0:
            _park(run, pull, reviewed,
                  f"workflow run {latest['id']} on the head commit"
                  f" {run.sha[:12]} did not finish within [merge]"
                  f" check_wait_sec = {wait}s")
        stop_if_requested(run.conn, run.run_id, "merge_gate")
        with heartbeat_while(run.conn, run.run_id, beat_s):
            github.SLEEP(min(github.CHECK_POLL_S, remaining))


def _head_failures(run, beat_s, pull, workflow_run_id):
    try:
        with heartbeat_while(run.conn, run.run_id, beat_s):
            runs = pr_status._check_runs_of(run.project, pull, run.sha)
    except InfraFailure:
        runs = None
    return tuple(check for check in pr_status._failed_checks(runs)
                 if check.workflow_run_id == workflow_run_id)


def _earlier(run, requeued, kind, event, key):
    persisted = run.conn is not None and run.run_id is not None
    history = (_requeued(run, event) if persisted else
               [(names, group) for of, names, group in requeued if of == kind])
    return next((group for names, group in history if names == key), None)


def _record_requeue(run, event, key, group, summary):
    _record(run, summary)
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, event,
                           f"re-queued after merge group {group[:12]}",
                           level="detail", payload=json.dumps(
                               {"names": key, "group": group}))


def _record(run, summary):
    if run.conn is not None and run.run_id is not None:
        store.record_event(run.conn, run.run_id, "merge_queue", summary)


def _requeued(run, event):
    rows = run.conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = ?"
        " ORDER BY id", (run.run_id, event)).fetchall()
    return [(record["names"], record["group"])
            for record in (json.loads(row[0]) for row in rows)]


def _park(run, pull, reviewed, why):
    from holophyte.pr.pullrequest import _park_on_pr
    _park_on_pr(run.project, run.conn, run.run_id, run.provider, run.task_id,
                run.branch, run.sha, pull, why, (), reviewed=reviewed)
