"""Store writes recording what a run ran under; none of them may stop the run."""
import json
from time import time

import store
from holophyte.loop.gates import InfraFailure, sh
from holophyte.loop.implementer_profile import latest_profile
from holophyte.loop.stop import resumes
from holophyte.stages import standard
from store import pipelines, stages

FAILED = "stage_record_failed"
REFUSED = object()


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
    document = standard.resolve(run.project, row and row[1]["profile"])
    return pipelines.pin_pipeline(conn, run_id, standard.NAME, document)


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
    if conn is None or run_id is None:
        return False
    row = guarded(conn, run_id, "stage_read", lambda: conn.execute(
        "SELECT stageId, outcome, outputSha FROM stageAttempts WHERE runId = ?"
        " ORDER BY id DESC LIMIT 1", (run_id,)).fetchone())
    return row not in (None, REFUSED) and tuple(row) == ("verify", "pass", sha)


def skip(conn, run_id, stage_id, reason, sha=None):
    if conn is not None and run_id is not None:
        guarded(conn, run_id, "skip_stage",
                lambda: stages.skip_stage(conn, run_id, stage_id, reason, sha))
