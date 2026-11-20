"""Store writes recording what a run ran under; none of them may stop the run."""
import json
from time import time

import store
from holophyte.config.pipeline_settings import current_notes, pinned_checks
from holophyte.loop.gates import InfraFailure, MergeParked, sh
from holophyte.loop.implementer_profile import latest_profile
from holophyte.loop.runs import RunSwept
from holophyte.loop.stop import resumes
from holophyte.stages import notes, standard
from store import pipelines, stages

FAILED = "stage_record_failed"
REFUSED = object()
RESTORED = "restored"
RELEASES = {"approve": ("pass", "next"), "babysit": ("fail", "on_fail"),
            "operator_note": ("fail", "on_fail")}


def guarded(conn, run_id, write, record):
    try:
        return record()
    except (Exception, SystemExit) as error:
        try:
            store.record_event(
                conn, run_id, FAILED, f"{write} not recorded: {error}",
                level="detail",
                payload=json.dumps({"write": write, "error": repr(error)}))
        except Exception:
            pass
        return REFUSED


def checks_guarded(conn, run_id, run_checks):
    ended = []

    def kept():
        try:
            return run_checks()
        except (store.RunEnded, RunSwept) as error:
            ended.append(error)

    guarded(conn, run_id, "pipeline_checks", kept)
    if ended:
        raise ended[0]


def pin(run):
    conn, run_id = run.conn, run.run_id
    if conn is None or run_id is None:
        return
    inherited = guarded(conn, run_id, "inherit_pipeline",
                        lambda: _inherited(run))
    if inherited is None:
        guarded(conn, run_id, "pin_pipeline", lambda: _resolved(run))


def _inherited(run):
    conn, run_id = run.conn, run.run_id
    if not resumes(run):
        return None
    return pipelines.inherit_pipeline(conn, run_id, predecessor(conn, run_id))


def _resolved(run):
    conn, run_id = run.conn, run.run_id
    row = latest_profile(conn, run_id)
    notes = guarded(conn, run_id, "pipeline_notes",
                    lambda: current_notes(run.project))
    checks = guarded(conn, run_id, "pipeline_checks",
                     lambda: pinned_checks(run.project))
    document = standard.resolve(run.project, row and row[1]["profile"],
                                None if notes is REFUSED else notes,
                                () if checks is REFUSED else checks)
    return pipelines.pin_pipeline(conn, run_id, standard.NAME, document)


def notes_block(project, conn, run_id, target):
    found = guarded(conn, run_id, "pipeline_notes",
                    lambda: notes.block(project, conn, run_id, target))
    return "" if found is REFUSED else found


def predecessor(conn, run_id):
    (previous,) = conn.execute(
        "SELECT old.id FROM runs old JOIN runs current"
        " ON old.ticketId = current.ticketId WHERE current.id = ?"
        " AND old.id != current.id ORDER BY old.attempt DESC LIMIT 1",
        (run_id,)).fetchone()
    return previous


def stage_roles(conn, run_id, stage_id):
    row = conn.execute(
        "SELECT p.document FROM runs r JOIN pipelines p ON p.id = r.pipelineId"
        " WHERE r.id = ?", (run_id,)).fetchone()
    found = [stage for stage in json.loads(row[0])["stages"]
             if stage["id"] == stage_id] if row else []
    return found[0]["roles"] if found else ()


class Attempt:
    def __init__(self, conn, run_id, stage_id, sha, detail, wt=None):
        self.conn, self.run_id, self.stage_id = conn, run_id, stage_id
        self.sha, self.wt = sha, wt
        self.detail, self.closed = dict(detail), False
        self.id = None
        if conn is not None and run_id is not None:
            self.id = self._open()

    def _open(self, **mark):
        conn, run_id = self.conn, self.run_id
        return guarded(conn, run_id, "open_stage", lambda: stages.open_stage(
            conn, run_id, self.stage_id, self.sha, self.detail, **mark))

    @property
    def recording(self):
        return self.id not in (None, REFUSED) and not self.closed

    def note(self, **detail):
        self.detail.update(detail)

    def close(self, outcome, edge, sha=None, **detail):
        self.detail.update(detail)
        if not self.recording:
            return
        self.closed = True
        conn, run_id = self.conn, self.run_id
        guarded(conn, run_id, "close_stage", lambda: stages.close_stage(
            conn, self.id, outcome, edge, sha,
            stage_roles(conn, run_id, self.stage_id), self.detail))

    def __enter__(self):
        return self

    def __exit__(self, kind, error, trace):
        if error is not None:
            self.close_at_head(
                "error" if isinstance(error, InfraFailure) else "fail", "end",
                error=str(error))
        return False

    def close_at_head(self, outcome, edge, **detail):
        if not self.recording:
            return
        head = guarded(self.conn, self.run_id, "stage_head", self.head)
        self.close(outcome, edge, None if head is REFUSED else head, **detail)


    def head(self):
        if self.wt is None or not self.wt.exists():
            return self.sha
        return sh(["git", "rev-parse", "HEAD"], cwd=self.wt)


def fix_round(conn, run_id, stage_id, sha, detail, wt, fix):
    with Attempt(conn, run_id, stage_id, sha, detail, wt=wt) as attempt:
        try:
            fixed = fix()
        except MergeParked:
            attempt.close_at_head("fail", "park")
            raise
        attempt.close("pass", "next", fixed)
    return fixed


def head(conn, run_id, wt):
    found = guarded(conn, run_id, "stage_head",
                    lambda: sh(["git", "rev-parse", "HEAD"], cwd=wt))
    return None if found is REFUSED else found


class Deferred(Attempt):
    def __init__(self, conn, run_id, stage_id, sha, detail, wt=None):
        super().__init__(None, None, stage_id, sha, detail, wt)
        self.conn, self.run_id, self.mark = conn, run_id, None
        if conn is not None and run_id is not None:
            self.mark = guarded(conn, run_id, "stage_read", lambda: (
                int(time() * 1000), conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM runEvents"
                    " WHERE runId = ?", (run_id,)).fetchone()[0]))

    def opened(self):
        if self.id is None and self.mark not in (None, REFUSED):
            started, seq = self.mark
            self.id = self._open(now=started, open_seq=seq)
        return self

    def __exit__(self, kind, error, trace):
        if error is not None:
            self.opened()
        return super().__exit__(kind, error, trace)


def verified_last(conn, run_id, sha):
    if conn is None or run_id is None or sha is None:
        return False
    rows = guarded(conn, run_id, "stage_read", lambda: conn.execute(
        "SELECT stageId, outcome, outputSha FROM stageAttempts WHERE runId = ?"
        " ORDER BY id DESC", (run_id,)).fetchall())
    if rows is REFUSED:
        return False
    for row in rows:
        if tuple(row)[::2] != ("implement", sha):
            return tuple(row) == ("verify", "pass", sha)
    return False


def skip(conn, run_id, stage_id, reason, sha=None):
    if conn is not None and run_id is not None:
        guarded(conn, run_id, "skip_stage",
                lambda: stages.skip_stage(conn, run_id, stage_id, reason, sha))


def repeats(conn, run_id, stage_id, sha, outcome, edge, detail):
    row = guarded(conn, run_id, "stage_read", lambda: conn.execute(
        "SELECT inputSha, outcome, edge, detail FROM stageAttempts"
        " WHERE runId = ? AND stageId = ? ORDER BY id DESC LIMIT 1",
        (run_id, stage_id)).fetchone())
    return row not in (None, REFUSED) and (
        tuple(row[:3]) == (sha, outcome, edge) and json.loads(row[3]) == detail)


def span(conn, run_id, stage_id, sha, outcome, edge, detail, times=None):
    started, ended = times or (None, None)
    opened = guarded(conn, run_id, "open_stage", lambda: stages.open_stage(
        conn, run_id, stage_id, sha, detail, now=started))
    if opened is not REFUSED:
        guarded(conn, run_id, "close_stage", lambda: stages.close_stage(
            conn, opened, outcome, edge, sha, (), None, now=ended))


def recorded(conn, run_id, stage_id, key, value):
    return conn.execute(
        "SELECT 1 FROM stageAttempts s JOIN runs old ON old.id = s.runId"
        " JOIN runs current ON current.ticketId = old.ticketId"
        " WHERE current.id = ? AND s.stageId = ?"
        " AND json_extract(s.detail, '$.' || ?) = ?",
        (run_id, stage_id, key, value)).fetchone() is not None


def waiting(conn, run_id, sha, approve, ready=False):
    if conn is None or run_id is None:
        return
    detail = guarded(conn, run_id, "stage_read",
                     lambda: _park_detail(conn, run_id, approve(), ready))
    Attempt(conn, run_id, "waiting", sha, {} if detail is REFUSED else detail)


def _park_detail(conn, run_id, approve, ready):
    (kind,) = conn.execute("SELECT parkKind FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
    (summary,) = conn.execute(
        "SELECT summary FROM runEvents WHERE runId = ? AND kind ="
        " 'phase_change' ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
    reason = summary.partition(": ")[2]
    if ready == RESTORED:
        ready = _waited_for_approval(conn, run_id)
    if approve == "human" and ready:
        kind = "approval"
    return {"kind": kind, "reason": reason, "approve": approve}


def _waited_for_approval(conn, run_id):
    row = conn.execute(
        "SELECT json_extract(s.detail, '$.kind') FROM stageAttempts s"
        " JOIN runs old ON old.id = s.runId JOIN runs current"
        " ON current.ticketId = old.ticketId WHERE current.id = ?"
        " AND s.stageId = 'waiting' ORDER BY s.id DESC LIMIT 1",
        (run_id,)).fetchone()
    return row is not None and row[0] == "approval"


def release(run):
    conn, run_id = run.conn, run.run_id
    if conn is None or run_id is None:
        return
    parked = guarded(conn, run_id, "stage_read", lambda: conn.execute(
        "SELECT s.id, s.runId, s.inputSha FROM stageAttempts s"
        " JOIN runs old ON old.id = s.runId JOIN runs current"
        " ON current.ticketId = old.ticketId WHERE current.id = ?"
        " AND old.id != current.id AND s.stageId = 'waiting'"
        " AND s.endedAt IS NULL", (run_id,)).fetchall())
    for attempt_id, parked_run, sha in [] if parked is REFUSED else parked:
        guarded(conn, run_id, "close_stage", lambda: _released(
            conn, attempt_id, parked_run, sha))


def _released(conn, attempt_id, parked_run, sha):
    marks = ", ".join("?" * len(RELEASES))
    row = conn.execute(
        'SELECT i.id, i."action", i.source, i.actor, i.at FROM interventions i'
        " JOIN runs r ON r.id = i.runId WHERE i.runId = ?"
        " AND r.outcome = 'abandoned'"
        f' AND i."action" IN ({marks}) ORDER BY i.id DESC LIMIT 1',
        (parked_run, *RELEASES)).fetchone()
    if row is None:
        return
    intervention, action, source, actor, at = row
    stages.close_stage(conn, attempt_id, *RELEASES[action], sha, (),
                       {"release": action, "source": source, "actor": actor,
                        "intervention": intervention}, now=at)
