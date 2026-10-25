"""A pool worker's loaded build against the factory checkout before the merge gate."""
import json
import os
import subprocess

import store
import store.read
from holophyte.host import startup
from holophyte.loop import pool_handoff
from holophyte.loop.reexec import reexec_self
from holophyte.loop.runs import warn_on_run
from holophyte.redact import safe_print as print

GATE_RUN_ENV = "HOLOPHYTE_GATE_RUN"
EXEC = os.execv


class GateHandoff(store.RunEnded):
    pass


class GateReexec(Exception):
    def __init__(self, run_id):
        super().__init__(run_id)
        self.run_id = run_id


def _commit(checkout, rev):
    r = subprocess.run(["git", "rev-parse", rev], cwd=checkout,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(" ".join((r.stderr or r.stdout).split()))
    return r.stdout.strip()


def moved_build(conn, run_id):
    if startup.BUILD is None:
        return None
    checkout = pool_handoff.factory_checkout()
    try:
        loaded = _commit(checkout, f"{startup.BUILD}^{{commit}}")
        head = _commit(checkout, "HEAD")
    except (OSError, RuntimeError) as e:
        warn_on_run(conn, run_id,
                    f"the loaded build {startup.BUILD} could not be compared"
                    f" with the factory checkout ({e}); the merge gate goes on")
        return None
    return None if loaded == head else (loaded, head)


def before_gate(project, conn, run_id, sha, rnd):
    from holophyte.loop.pool import WORKER_SLOT_ENV
    from store.schema import SCHEMA_VERSION

    if conn is None or not os.environ.get(WORKER_SLOT_ENV):
        return
    moved = moved_build(conn, run_id)
    if moved is None:
        return
    loaded, head = moved
    version, _ = pool_handoff.fetched_schema(project, ref="HEAD")
    if version == SCHEMA_VERSION:
        _checkpoint_reexec(conn, run_id, sha, rnd, loaded, head)
    note = (f"the factory checkout moved from {loaded[:12]} to {head[:12]}"
            " under this run; handing the approved candidate"
            f" {sha[:12]} to the next build at the merge gate")
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    with store.transaction(conn):
        store.record_event(conn, run_id, "gate_handoff", note)
        store.record_event(conn, run_id, "approval_carried",
                           f"the approval of {sha} carries to the next build")
        store.release(conn, run_id, "paused", note, resume_phase="merge_gate",
                      candidate_sha=sha)
        store.walk_ticket(conn, ticket_id, "ready")
    print(f"[holo2] {note}")
    raise GateHandoff(run_id, "paused", note)


def _checkpoint_reexec(conn, run_id, sha, rnd, loaded, head):
    note = (f"the factory checkout moved from {loaded} to {head} under this"
            " run with the schema unchanged; re-executing into the merge gate"
            f" at {sha}")
    store.record_event(conn, run_id, "gate_reexec", note, level="detail",
                       payload=json.dumps({"sha": sha, "rnd": rnd,
                                           "leaving": loaded,
                                           "arriving": head}))
    print(f"[holo2] {note}")
    raise GateReexec(run_id)


def replace_into_gate(run_id):
    os.environ[GATE_RUN_ENV] = str(run_id)
    reexec_self(f"re-executing run {run_id} into the merge gate", EXEC)


def gate_task(target, conn, provider, run_id):
    from holophyte.loop.claim import claimed_run

    row = store.read.run_snapshot(conn, run_id)
    event = row and row.endedAt is None and conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'gate_reexec'"
        " ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    if not event:
        return None
    ticket = store.read.ticket_by_id(conn, row.ticketId)
    live = provider.fetch_task(ticket.linearIssueId)
    return dict(live, _run=claimed_run(target, live, conn, run_id, provider),
                _gate=json.loads(event[0])), ticket.id
