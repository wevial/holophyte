import getpass
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import store
import store.read
import store.tickets
from holophyte.agents.agent_routes import active_fallbacks, reset, routes
from holophyte.agents.fallback import startup_routes
from holophyte.agents.fix_session import resume_argv
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
from holophyte.loop.claim import _claim_next, claimed_run
from holophyte.loop.claim_store import BOARD_DOWN, announce, store_mode, sync_board
from holophyte.loop.gates import sh
from holophyte.loop.implementer_profile import run_view
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
from store import operator_notes, steer_notes
from store.gap_layers import record_gap_layer
from store.operate import acting_as, author_actor, swept_condition

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
                witness_step(target, conn, project)
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


def requeue(target, identifier, note, out=None, provider=None, force=False):
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
            run_id = store.requeue(conn, ticket_id, note, force=force)
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
        condition = swept_condition(conn, parked_run)
        if condition is not None:
            return _approve_swept(target, conn, identifier, ticket_id,
                                  parked_run, condition, note, force, out)
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


def _approve_swept(target, conn, identifier, ticket_id, run_id, condition,
                   note, force, out):
    rnd, sha, unmet = _reviewed_commit(conn, run_id)
    if sha is None:
        raise SystemExit(
            f"[holo2] {identifier}: run {run_id} was swept {condition} but no"
            " ended review round of it names the commit it judged; nothing"
            " approved")
    if unmet and not force:
        raise SystemExit(
            f"[holo2] {identifier}: run {run_id} was swept {condition} and"
            f" its review round {rnd} left {', '.join(unmet)}; nothing"
            " approved, and --force --note TEXT approves it anyway")
    if unmet:
        note = f"forced past criteria: {', '.join(unmet)}; {note}"
    branch = store.read.park_facts(conn, run_id).branch
    (title,) = conn.execute("SELECT title FROM tickets WHERE id = ?",
                            (ticket_id,)).fetchone()
    claim = claimed_run(target, {"id": identifier, "title": title,
                                 "budget_min": 0})
    if branch != claim.branch:
        raise SystemExit(
            f"[holo2] {identifier}: run {run_id} recorded branch {branch},"
            f" but the next claim resumes {claim.branch}; nothing approved")
    dropped = _set_aside(target.path, claim.wt, run_id, branch, sha,
                         identifier)
    try:
        store.approve_swept(conn, ticket_id, note, sha, run_id=run_id)
    except (store.ApproveRefused, ValueError) as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    print(f"[holo2] {identifier} approved at {sha}: run {run_id}, swept"
          f" {condition}, released at the commit its review round {rnd}"
          " judged and the ticket is ready; the loop's next claim resumes"
          " that candidate at the merge gate", file=out)
    if dropped:
        print(f"[holo2] set aside {len(dropped)} later commit(s) of {branch}"
              f" under {DROPPED_REF}{run_id}: {', '.join(dropped)}", file=out)


DROPPED_REF = "refs/holophyte/dropped/"


def _reviewed_commit(conn, run_id):
    rounds = store.read.newest_ended_rounds(conn, run_id, 1)
    if not rounds:
        return None, None, []
    rnd, sha = rounds[0].round, None
    (verdict,) = conn.execute(
        "SELECT verdict FROM reviewRounds WHERE runId = ? AND round = ?",
        (run_id, rnd)).fetchone()
    unmet = ["no criterion judged (the review errored)"] if (
        verdict == "error") else []
    for kind, payload in conn.execute(
            "SELECT kind, payload FROM runEvents WHERE runId = ? AND kind IN"
            " ('blast_radius', 'criteria') ORDER BY seq", (run_id,)):
        payload = json.loads(payload or "{}")
        if payload.get("round") != rnd:
            continue
        if kind == "blast_radius":
            sha = payload.get("sha")
        else:
            unmet = [f"{entry['id']} {entry['status']}"
                     for entry in payload.get("criteria", [])
                     if entry.get("status") != "met"]
    return rnd, sha, unmet


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True)


def _set_aside(repo, worktree, run_id, branch, sha, identifier):
    refused = f"[holo2] {identifier}: "
    tip = _git(repo, "rev-parse", "--verify", "--quiet",
               f"refs/heads/{branch}").stdout.strip()
    if not tip:
        raise SystemExit(f"{refused}branch {branch} is gone; nothing approved")
    if _git(repo, "merge-base", "--is-ancestor", sha, tip).returncode != 0:
        raise SystemExit(
            f"{refused}branch {branch} at {tip[:12]} does not descend from"
            f" the reviewed commit {sha[:12]}; nothing approved")
    if _checked_out(repo, branch) != worktree.resolve():
        raise SystemExit(
            f"{refused}branch {branch} is not checked out at {worktree},"
            " where the next claim resumes it; nothing approved")
    if sh(["git", "status", "--porcelain"], cwd=worktree):
        raise SystemExit(f"{refused}the worktree {worktree} holds uncommitted"
                         f" changes on {branch}; nothing approved")
    if tip == sha:
        return []
    ref = f"{DROPPED_REF}{run_id}"
    kept = _git(repo, "rev-parse", "--verify", "--quiet", ref).stdout.strip()
    if kept and _git(repo, "merge-base", "--is-ancestor", kept,
                     tip).returncode != 0:
        raise SystemExit(
            f"{refused}{ref} already holds {kept[:12]}, which {branch} does"
            " not contain; nothing approved")
    dropped = sh(["git", "rev-list", "--abbrev-commit", f"{sha}..{tip}"],
                 cwd=repo).split()
    why = _rewind(worktree, [f"update refs/heads/{branch} {sha} {tip}",
                             f"update {ref} {tip} {kept or NO_REF}"],
                  sha, tip)
    if why is not None:
        raise SystemExit(f"{refused}{why}; {branch} left at {tip[:12]};"
                         " nothing approved")
    return dropped


NO_REF = "0" * 40


def _rewind(worktree, updates, sha, tip):
    lock = subprocess.Popen(["git", "update-ref", "--stdin"], cwd=worktree,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    lock.stdin.write("start\n" + "".join(f"{u}\n" for u in updates)
                     + "prepare\n")
    lock.stdin.flush()
    if [lock.stdout.readline() for _ in range(2)] != ["start: ok\n",
                                                      "prepare: ok\n"]:
        return f"cannot lock the refs: {lock.communicate()[1].strip()}"
    why = _moved_tree(worktree, sha, tip)
    _, error = lock.communicate("abort\n" if why else "commit\n")
    if lock.returncode != 0 and why is None:
        why = f"cannot move the refs: {error.strip()}"
    return why


def _moved_tree(worktree, sha, tip):
    added = _git(worktree, "diff", "--name-only", "--no-renames",
                 "--diff-filter=A", tip, sha).stdout.splitlines()
    present = [path for path in added if os.path.lexists(worktree / path)]
    if present:
        return f"the rewind would overwrite {', '.join(present)} in {worktree}"
    moved = _git(worktree, "read-tree", "-m", "-u", tip, sha)
    if moved.returncode != 0:
        return f"cannot check out {sha[:12]}: {moved.stderr.strip()}"
    if (_git(worktree, "diff-index", "--quiet", "--cached", sha).returncode
            or _git(worktree, "diff-files", "--quiet").returncode):
        _git(worktree, "read-tree", "-m", "-u", sha, tip)
        return f"the worktree {worktree} changed while it was rewound"
    return None


def _checked_out(repo, branch):
    path = None
    for line in sh(["git", "worktree", "list", "--porcelain"],
                   cwd=repo).splitlines():
        if line.startswith("worktree "):
            path = Path(line.removeprefix("worktree ")).resolve()
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


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
            with acting_as(author_actor(author)):
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
            with acting_as(author_actor(author)):
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


def steer_ticket(target, identifier, note, hint=False, author=None, out=None,
                 now=False):
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            with acting_as(author_actor(author)):
                line = steered_line(
                    target, conn, ticket_id, identifier, note,
                    getpass.getuser() if author is None else author,
                    hint=hint, now=now, out=out)
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {line}", file=out)
    finally:
        conn.close()


def steered_line(target, conn, ticket_id, identifier, note, author, hint, now,
                 out):
    interrupt = now and _interrupts(target, conn, ticket_id, out)
    steered = steer_notes.steer(conn, ticket_id, note, author, hint=hint,
                                interrupt=interrupt)
    if interrupt:
        return (f"{identifier} steered --now: {steered.kind} (steer note"
                f" {steered.id}) on live run {steered.run_id}; a running"
                " implementer turn stops at its next heartbeat and resumes"
                " its session with the note, else the note lands at the next"
                " implementer turn")
    if steered.live and steered.event_id is not None:
        return (f"{identifier} steered: {steered.kind} (operator_note"
                f" event {steered.event_id}, steer note {steered.id}) on live"
                f" run {steered.run_id}, landing at its babysitter's next"
                " fix round")
    if steered.event_id is not None:
        return (f"{identifier} steered: run {steered.run_id} sent"
                " back to the babysitter as a maintainer instruction"
                f" (operator_note event {steered.event_id}, steer note"
                f" {steered.id})")
    if steered.live:
        return (f"{identifier} steered: {steered.kind} (steer note"
                f" {steered.id}) on live run {steered.run_id}, landing at"
                " its next implementer turn")
    reader = "its next implement turn" if hint else "its next run's contract"
    return (f"{identifier} steered: {steered.kind} (steer note"
            f" {steered.id}) for {reader}")


def withdraw_steers(target, identifier, note, out=None, author=None):
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            with acting_as(author_actor(author)):
                count, intervention_id = steer_notes.withdraw(conn, ticket_id,
                                                              note)
        except ValueError as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier}: {count} amendment(s) withdrawn (steer"
              f" intervention {intervention_id}); later runs read the ticket"
              " without them", file=out)
    finally:
        conn.close()


def _interrupts(target, conn, ticket_id, out):
    live = store.read.ticket_by_id(conn, ticket_id).activeRunId
    run = live and store.read.run_detail(conn, live)
    babysat = (run is not None and run.phase in operator_notes.BABYSIT_PHASES
               and run.prUrl)
    if run is None or not babysat and (
            run.phase not in steer_notes.LIVE_PHASES or run.prUrl):
        return True
    view = run_view(target, conn, live)
    argv, reason = ((None, "fallback implementer route")
                    if "implementer" in active_fallbacks(target)
                    else (None, "implementer profile no longer configured")
                    if view is None else resume_argv(view, conn, live))
    if argv is None:
        print(f"[holo2] --now: cannot resume the implementer session"
              f" ({reason}); the note lands at the next turn boundary",
              file=out)
    return argv is not None


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
