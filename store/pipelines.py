"""The pipeline document each run was claimed with, one revision per digest."""
import hashlib
import json
import time

from holophyte.redact import redact_document

from .schema import _transaction


def pin_pipeline(conn, run_id, name, document, now=None):
    canonical = json.dumps(redact_document(document), sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute("SELECT projectId, pipelineId FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        project_id, pinned = row
        if pinned is not None:
            return pinned
        found = conn.execute(
            "SELECT id FROM pipelines WHERE projectId = ? AND digest = ?",
            (project_id, digest)).fetchone()
        if found is not None:
            (pipeline_id,) = found
        else:
            (revision,) = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM pipelines"
                " WHERE projectId = ?", (project_id,)).fetchone()
            pipeline_id = conn.execute(
                "INSERT INTO pipelines (projectId, name, revision, format,"
                " digest, document, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (project_id, name, revision, document.get("format"), digest,
                 canonical, now)).lastrowid
        conn.execute("UPDATE runs SET pipelineId = ? WHERE id = ?",
                     (pipeline_id, run_id))
    return pipeline_id


def inherit_pipeline(conn, run_id, from_run_id):
    with _transaction(conn):
        row = conn.execute("SELECT pipelineId FROM runs WHERE id = ?",
                           (from_run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {from_run_id}")
        (pipeline_id,) = row
        if pipeline_id is None:
            return None
        conn.execute("UPDATE runs SET pipelineId = ? WHERE id = ?"
                     " AND pipelineId IS NULL", (pipeline_id, run_id))
    return pipeline_id


def run_pipelines(conn, run_ids):
    rows = conn.execute(
        "SELECT r.id, p.id, p.revision, p.document FROM runs r"
        " JOIN pipelines p ON p.id = r.pipelineId"
        f" WHERE r.id IN ({', '.join('?' * len(run_ids))})", run_ids)
    return {run_id: {**json.loads(document), "id": pipeline_id,
                     "revision": revision}
            for run_id, pipeline_id, revision, document in rows}
