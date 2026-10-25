"""A pool worker's loaded build against the factory checkout before the merge gate."""
import os
import subprocess

import store
import store.read
from holophyte.host import startup
from holophyte.loop import pool_handoff
from holophyte.loop.runs import warn_on_run
from holophyte.redact import safe_print as print


class GateHandoff(store.RunEnded):
    pass


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


def before_gate(conn, run_id, sha):
    from holophyte.loop.pool import WORKER_SLOT_ENV

    if conn is None or not os.environ.get(WORKER_SLOT_ENV):
        return
    moved = moved_build(conn, run_id)
    if moved is None:
        return
    loaded, head = moved
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
