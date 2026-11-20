"""Store writes recording what a run ran under; none of them may stop the run."""
import json

import store
from holophyte.loop.implementer_profile import latest_profile
from holophyte.loop.stop import resumes
from holophyte.stages import standard
from store import pipelines

FAILED = "stage_record_failed"


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
        return None


def pin(run):
    if run.conn is None or run.run_id is None:
        return None
    return guarded(run.conn, run.run_id, "pin_pipeline", lambda: _pin(run))


def _pin(run):
    conn, run_id = run.conn, run.run_id
    if resumes(run):
        inherited = pipelines.inherit_pipeline(conn, run_id,
                                               predecessor(conn, run_id))
        if inherited is not None:
            return inherited
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
