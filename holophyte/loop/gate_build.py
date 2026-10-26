"""A pool worker's loaded build against the factory checkout before the merge gate."""
import json
import os
import subprocess
from dataclasses import replace
from time import sleep, time

import store
import store.read
from holophyte.host import startup
from holophyte.loop import pool_handoff
from holophyte.loop.reexec import reexec_self
from holophyte.loop.runs import warn_on_run
from holophyte.redact import safe_print as print

GATE_RUN_ENV = "HOLOPHYTE_GATE_RUN"
EXEC = os.execv
BOARD_TRIES = 3
BOARD_WAIT_S = 10


class GateHandoff(store.RunEnded):
    pass


class GateReexec(Exception):
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
    hand_off(conn, run_id, sha, f"the factory checkout moved from {loaded[:12]}"
             f" to {head[:12]} under this run")


def hand_off(conn, run_id, sha, why):
    note = (f"{why}; handing the approved candidate {sha[:12]} to the next"
            " build at the merge gate")
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


def _fetch_task(provider, issue_id):
    for attempt in range(1, BOARD_TRIES + 1):
        try:
            return provider.fetch_task(issue_id), None
        except Exception as e:
            print(f"[holo2] {issue_id} could not be read back from the board"
                  f" ({e}); try {attempt} of {BOARD_TRIES}")
            error = e
            if attempt < BOARD_TRIES:
                sleep(BOARD_WAIT_S)
    return None, error


def _fail_gate_run(target, conn, provider, run_id, ticket_id, refused):
    from holophyte.loop.dispatch import fail_run
    from holophyte.loop.gates import outcome_class_of

    print(f"[holo2] run failed: {refused.reason}")
    fail_run(target, conn, run_id, ticket_id, provider, refused.reason,
             outcome_class_of(refused), refused.failure_kind, refresh=False)


def _gate_event(conn, run_id):
    row = store.read.run_snapshot(conn, run_id)
    event = row and row.endedAt is None and conn.execute(
        "SELECT payload FROM runEvents WHERE runId = ? AND kind = 'gate_reexec'"
        " ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    return (row, json.loads(event[0])) if event else (None, None)


def gate_task(target, conn, provider, run_id):
    from holophyte.loop.claim import claimed_run
    from holophyte.loop.claim_store import BOARD_DOWN
    from holophyte.loop.gates import InfraFailure, RunFailure

    row, gate = _gate_event(conn, run_id)
    if row is None:
        return None
    ticket = store.read.ticket_by_id(conn, row.ticketId)
    live, error = _fetch_task(provider, ticket.linearIssueId)
    if error is not None:
        _fail_gate_run(target, conn, provider, run_id, ticket.id, InfraFailure(
            f"the board did not answer after the re-exec into the merge gate"
            f" ({error}); the approved candidate was not gated"))
        return BOARD_DOWN, ticket.id
    if live is None:
        _fail_gate_run(target, conn, provider, run_id, ticket.id, RunFailure(
            f"{ticket.linearIssueId} is gone from the board after the re-exec"
            " into the merge gate; the approved candidate was not gated"))
        return None, ticket.id
    run = claimed_run(target, live, conn, run_id, provider)
    run = replace(run, started=run.clock() - (time() * 1000 - row.startedAt) / 1000)
    return dict(live, _run=run, _gate=gate), ticket.id


def hand_off_gate_run(target, provider, run_id, why):
    from holophyte.board import projection
    from holophyte.loop.runs import open_store

    conn = open_store(target)
    try:
        row, gate = _gate_event(conn, run_id)
        if gate is not None:
            hand_off(conn, run_id, gate["sha"], why)
    except GateHandoff:
        projection.mirror_push(conn, row.ticketId, provider)
        projection.release_lease_label(target, conn, row.ticketId, provider,
                                       run_id)
    finally:
        conn.close()
