import getpass
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

import store
import store.read
import store.tickets
from holophyte.agents.agent_routes import reset, routes
from holophyte.agents.fallback import startup_routes
from holophyte.agents.probes import probe_diagnostic, probe_implementer
from holophyte.board.projection import release_lease_label
from holophyte.cli.report import migration_header, report_lines
from holophyte.config.config_tables import loop_config, merge_config, report_config
from holophyte.host.reconcile import (
    CloseRefused,
    _reconcile_at_startup,
    _reconcile_pull_requests,
    close_out_landed,
)
from holophyte.host.startup import banner
from holophyte.host.supervisor import linear_budget_low, supervisor_liveness_line
from holophyte.loop.claim import _claim_next
from holophyte.loop.claim_store import BOARD_DOWN, announce, store_mode, sync_board
from holophyte.loop.gates import sh
from holophyte.loop.pool import scheduler
from holophyte.loop.pool_handoff import (  # noqa: F401
    _fetch_main,
    _ff_main,
    _prepare_reexec,
)
from holophyte.loop.reexec import reexec_self
from holophyte.loop.runs import open_store
from holophyte.pr.merge_ready import PARKED_PHASE, readiness
from holophyte.review.findings import commit_findings
from store import operator_notes
from store.gap_layers import record_gap_layer

BABYSIT_DEFAULT_NOTE = "sent back to the babysitter"

EXEC = os.execv


def self_hosted(target):
    return Path(__file__).resolve().parents[2] == target.path.resolve()


def main(target, provider):
    banner(target)
    reset(target)
    try:
        knobs = loop_config(target)
        if not startup_routes(target, provider, probe_implementer,
                              activate=knobs.workers == 1):
            return 1
        run = _serial if knobs.workers == 1 else scheduler
        return run(target, provider, knobs)
    except store.SchemaNewer as moved:
        reexec_self(_schema_reason(moved), EXEC)
    finally:
        reset(target)


def _record_startup_probe(target, provider, probe):
    from time import time

    from store import launch_backoff

    if probe.ok and not target.store_path.exists():
        return
    conn = open_store(target)
    try:
        project = store.ensure_project(conn, provider.team, target.path)
        if probe.ok:
            launch_backoff.clear(conn, project)
        else:
            launch_backoff.failure(conn, project, probe_diagnostic(target, probe),
                                   int(time() * 1000), pending=True)
    finally:
        conn.close()


def _serial(target, provider, knobs):
    from holophyte.loop.dispatch import PARKED, SWEPT, _dispatch, _startup_sweep
    from holophyte.story.witness import witness_step

    restart_after_merge = self_hosted(target)
    stop_on_failure = knobs.stop_on_failure
    order = knobs.order
    failed = False
    conn = open_store(target)
    try:
        project = store.tickets.ensure_project(conn, provider.team, target.path)
        seen = _startup_sweep(target, conn)
        announce(target)
        _reconcile_at_startup(target, conn, project, provider)
        # Refused tickets stay listed; skipping them keeps the queue moving.
        skip = set()
        first_pass = True
        while True:
            moved = _schema_move(target)
            if moved and not moved.readable:
                _reexec(target, conn, project, moved.reason)
                return
            witness_step(target, conn, project)
            if not first_pass:
                skip -= _reconcile_pull_requests(target, conn, project,
                                                 provider)
            first_pass = False
            _read_board(target, conn, project, provider, knobs)
            # After the mirror: its listing may have spent the budget.
            if linear_budget_low():
                store.record_loop_return(conn, project)
                print("[holo2] the ready listing waits for the budget's"
                      " reset; done.")
                return 1
            task, ticket_id, run_id = _claim_next(target, conn, project,
                                                  provider, order, skip, seen)
            if not task:
                return _queue_ended(conn, project, task, failed)
            if run_id is None:
                return
            merged = _dispatch(target, conn, run_id, provider, task, ticket_id)
            if merged is PARKED:
                skip.add(task["id"])
                print(f"[holo2] {task['id']} parked awaiting merge approval;"
                      " continuing to the next ready ticket")
                continue
            if merged is SWEPT:
                skip.add(task["id"])
                outcome = conn.execute("SELECT outcome FROM runs WHERE id = ?",
                                       (run_id,)).fetchone()[0]
                print(f"[holo2] {task['id']} ended ({outcome}); continuing"
                      " to the next ready ticket")
                continue
            if not merged:
                if stop_on_failure or routes(target).failed:
                    return 1
                failed = True
                skip.add(task["id"])
                print(f"[holo2] {task['id']} failed; continuing to the next"
                      " ready ticket (stop_on_failure = false)")
                continue
            commit_findings(target,
                            f"Complete task {task['id']}: {task['title']}")
            if restart_after_merge:
                _reexec(target, conn, project)
                return
    finally:
        conn.close()


def _queue_ended(conn, project, task, failed):
    store.record_loop_return(conn, project)
    if task is BOARD_DOWN:
        print("[holo2] the board could not be read back at the claim;"
              " stopping. relaunch once the board answers")
        return 1
    print("[holo2] Linear has no ready tickets. done.")
    return 1 if failed else None


def _read_board(target, conn, project, provider, knobs):
    from holophyte.loop.dispatch import _mirror_queue

    if store_mode(target):
        sync_board(target, conn, project, provider,
                   min_interval_ms=knobs.tick_sec * 1000)
    else:
        _mirror_queue(target, conn, project, provider)


def _schema_reason(moved):
    return (f"store schema moved to {moved.version} under this process;"
            " re-executing")


class SchemaMove(NamedTuple):
    reason: str
    readable: bool


def _schema_move(target):
    try:
        conn = store.open(target.store_path)
    except store.SchemaNewer as moved:
        return SchemaMove(_schema_reason(moved), False)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    if version <= store.SCHEMA_VERSION:
        return None
    return SchemaMove(f"store schema moved to {version} under this process"
                      " and is readable by this build; re-executing", True)


def _reexec(target, conn, project, reason=None, *, prepared_sha=None,
            can_ff=None, worker_pids=()):
    from holophyte.loop.pool_handoff import factory_checkout

    sha = prepared_sha or sh(["git", "rev-parse", "--short", "HEAD"],
                             factory_checkout())
    if can_ff is None:
        can_ff, schema_moves = _prepare_reexec(target, worker_pids)
        if schema_moves and worker_pids:
            return False
    if can_ff:
        _ff_main(target)
    arriving = sh(["git", "rev-parse", "--short", "HEAD"], factory_checkout())
    store.record_loop_restart(conn, project, json.dumps({
        "leaving": sha, "arriving": arriving}))
    conn.close()
    reason = reason or "merged a change to the factory itself"
    reexec_self(f"{reason}; re-executing at {arriving} (leaving {sha})", EXEC)


def report(target, conn=None, out=None, now=None):
    out = out or sys.stdout
    if conn is None and not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return
    owned = conn is None
    conn = conn if conn is not None else store.open(target.store_path, migrate=False)
    try:
        for line in migration_header(conn):
            print(line, file=out)
        print("\n".join(report_lines(conn, target)), file=out)
        print(f"findings: {report_config(target).findings}", file=out)
        print(supervisor_liveness_line(target, conn, now), file=out)
    finally:
        if owned:
            conn.close()


def requeue(target, identifier, note, out=None, provider=None):
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        failed_run = _requeue_candidate(conn, ticket_id)
        if failed_run is not None:
            run_id, pr_url = failed_run
            release_lease_label(target, conn, ticket_id, provider, run_id)
            if pr_url:
                note = (f"{note}\n\nThe failed run's branch is still open"
                        f" as {pr_url}")
        try:
            run_id = store.requeue(conn, ticket_id, note)
        except (store.RequeueRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} requeued after run {run_id}", file=out)
    finally:
        conn.close()


def _requeue_candidate(conn, ticket_id):
    """Read first, so the lease label comes off while the ticket is in_flight."""
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if ticket is None or ticket.activeRunId is not None \
            or ticket.lastRunId is None \
            or ticket.boardState in ("Backlog", "Canceled", "Done"):
        return None
    row = conn.execute("SELECT outcome, phase, prUrl FROM runs"
                       " WHERE id = ?", (ticket.lastRunId,)).fetchone()
    if not row or row[0] != "failed":
        return None
    if ticket.status == "in_flight" or (
            ticket.status == "blocked_on_operator"
            and row[1] != "awaiting_merge_approval"):
        return ticket.lastRunId, row[2]
    return None

def approve(target, identifier, note, out=None, force=False):
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        parked_run = store.read.ticket_by_id(conn, ticket_id).lastRunId
        if _parked_at_pull_request(conn, parked_run) \
                and merge_config(target).approve == "human":
            ready = readiness(target, parked_run)
            if ready.reason is not None and not force:
                raise SystemExit(
                    f"[holo2] {identifier}: {ready.reason.replace('_', ' ')}"
                    f" ({ready.detail}); nothing approved, and --force"
                    " --note TEXT releases it anyway")
            if ready.reason is not None:
                note = f"forced past readiness: {ready.reason}; {note}"
        try:
            run_id = store.approve(conn, ticket_id, note, run_id=parked_run)
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} approved: run {run_id} released from"
              " awaiting_merge_approval and the ticket is ready; the loop's"
              " next claim resumes its candidate at the merge gate",
              file=out)
    finally:
        conn.close()


def _parked_at_pull_request(conn, run_id):
    park = None if run_id is None else store.read.park_facts(conn, run_id)
    return park is not None and park.ticket_status == "blocked_on_operator" \
        and park.phase == PARKED_PHASE and bool(park.pr_url)


def babysit_ticket(target, identifier, note, out=None, author=None):
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            if note != BABYSIT_DEFAULT_NOTE or author is not None:
                run_id = store.read.ticket_by_id(conn, ticket_id).lastRunId
                event_id = operator_notes.send_back(
                    conn, run_id, note,
                    getpass.getuser() if author is None else author)
                purpose = ("as a maintainer instruction "
                           f"(operator_note event {event_id})")
            else:
                run_id = store.babysit(conn, ticket_id, note)
                purpose = "for another look"
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} sent back to the babysitter: run {run_id}"
              f" released and the ticket is ready {purpose}", file=out)
    finally:
        conn.close()


def send_back_run(target, run_id, note, author=None, out=None):
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        try:
            event_id = operator_notes.send_back(
                conn, run_id, note,
                getpass.getuser() if author is None else author)
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] run {run_id}: {refused}") from None
        print(f"[holo2] run {run_id} sent back to the babysitter as a"
              f" maintainer instruction (operator_note event {event_id})",
              file=out)
    finally:
        conn.close()


def repoint(target, identifier, sha, note, out=None):
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id, old_sha = store.repoint(conn, ticket_id, sha, note)
        except (store.RepointRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} re-pointed: run {run_id}'s candidate"
              f" moved from {old_sha} to {sha}; the merge gate now holds the"
              " branch to the new sha", file=out)
    finally:
        conn.close()


def close_ticket(target, identifier, landed, note=None, out=None, provider=None):
    out = sys.stdout if out is None else out
    conn = _operator_store(target)
    message = f"Closed: change landed at {landed}; no factory merge occurred."
    if note:
        message += f"\n\n{note}"
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            close_out_landed(target, conn, ticket_id, message, provider)
        except CloseRefused as refused:
            raise SystemExit(f"[holo2] {identifier}: {refused}") from None
        print(f"[holo2] {identifier} closed: {landed}; no factory merge",
              file=out)
    finally:
        conn.close()


def gap_layer(target, identifier, layer, note, carried_by=None,
              found_by=None, out=None):
    out = sys.stdout if out is None else out
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        finder = {} if found_by is None else {"found_by": found_by}
        try:
            row_id = record_gap_layer(conn, ticket_id, layer, note,
                                      getpass.getuser(),
                                      carried_by=carried_by, **finder)
        except ValueError as refused:
            raise SystemExit(f"[holo2] {identifier}: {refused}") from None
        (found,) = conn.execute("SELECT foundBy FROM gapLayers WHERE id = ?",
                                (row_id,)).fetchone()
        carried = f", carried by {carried_by}" if carried_by else ""
        print(f"[holo2] {identifier} gap layer recorded: {layer}{carried},"
              f" found by {found}", file=out)
    finally:
        conn.close()


def _operator_store(target):
    if not target.store_path.exists():
        raise SystemExit(f"[holo2] no store at {target.store_path}")
    return open_store(target)


def _ticket_by_identifier(target, conn, identifier):
    rows = conn.execute(
        "SELECT id FROM tickets WHERE linearIdentifier = ?",
        (identifier,)).fetchall()
    if not rows:
        raise SystemExit(
            f"[holo2] {identifier}: no such ticket in {target.store_path}")
    if len(rows) > 1:
        raise SystemExit(
            f"[holo2] {identifier} names {len(rows)} tickets in"
            f" {target.store_path}; refusing to pick one")
    return rows[0][0]
