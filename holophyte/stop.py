"""Cooperative run stops and their durable continuation at stage boundaries."""
import json
import socket
from contextvars import ContextVar
from dataclasses import asdict

import store
from holophyte.target import Target, worktree_path
from store.schema import _transaction

_checkpoint = ContextVar("pause_checkpoint", default=None)


def stop_if_requested(conn, run_id, phase):
    """Preserve work and end a marked run at its next phase; never kill a turn."""
    if conn is None or run_id is None:
        return
    row = conn.execute(
        "SELECT r.endedAt, r.branch, r.ticketId, p.repoPath, i.guidance, r.prUrl,"
        " i.action FROM runs r JOIN projects p ON p.id = r.projectId"
        " JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
        (run_id,)).fetchone()
    if row is None or row[0] is not None:
        return
    _, branch, ticket_id, repo, note, pr_url, action = row
    if action == "abort":
        end_aborted(conn, run_id)
    stopped_at, phase = phase, "merge_gate" if pr_url else phase
    target = Target.locate(repo)
    sha = preserve(target, branch) if branch else None
    with _transaction(conn):
        ended, outcome, reason = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if ended is not None:
            raise store.RunEnded(run_id, outcome, reason)
        saved = _checkpoint.get()
        state = saved[3] if saved and saved[:3] == (conn, run_id, phase) else {}
        store.record_event(conn, run_id, "pause_checkpoint",
                           f"continuation at {phase}", level="detail",
                           payload=json.dumps(state))
        store.release(conn, run_id, "paused", note, resume_phase=phase,
                      candidate_sha=sha)
        store.walk_ticket(conn, ticket_id, "blocked_on_operator")
        store.set_question(conn, ticket_id, note)
    if pr_url:
        from holophyte import pause_notice
        pause_notice.mark(target, conn, run_id, stopped_at)
    raise store.RunEnded(run_id, "paused", note)


class Aborted(store.RunEnded):
    """This worker ended its own run `abandoned` for an operator's `--abort`."""


def abort_requested(conn, run_id):
    """Whether the live run carries an operator's pending abort."""
    return conn.execute(
        "SELECT 1 FROM runs r JOIN interventions i ON i.id = r.stopRequested"
        " WHERE r.id = ? AND r.endedAt IS NULL AND i.action = 'abort'",
        (run_id,)).fetchone() is not None


def end_aborted(conn, run_id):
    """Commit the tree as WIP, push it when a pull request is open, then end
    the run `abandoned` with the note and park the ticket; the turn's
    process group is the caller's to have killed. Merges, closes and
    deletes nothing."""
    from holophyte import pr
    from holophyte.gates import InfraFailure
    branch, ticket_id, repo, note, pr_url = conn.execute(
        "SELECT r.branch, r.ticketId, p.repoPath, i.guidance, r.prUrl"
        " FROM runs r JOIN projects p ON p.id = r.projectId"
        " JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
        (run_id,)).fetchone()
    target = Target.locate(repo)
    sha = preserve(target, branch, "abort") if branch else None
    if sha and pr_url:
        try:
            pr.push_branch(target, branch)
        except InfraFailure as refused:
            store.record_event(conn, run_id, "warning", f"abort push: {refused}")
    with _transaction(conn):
        ended, outcome, reason = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if ended is not None:
            raise store.RunEnded(run_id, outcome, reason)
        store.release(conn, run_id, "abandoned", note, candidate_sha=sha)
        store.walk_ticket(conn, ticket_id, "blocked_on_operator")
        store.set_question(conn, ticket_id, note)
    raise Aborted(run_id, "abandoned", note)


def preserve(target, branch, why="pause"):
    """Reuse the reclaim path's environment exclusions and staging policy."""
    from holophyte.claim import paths, sh, stage_work, unstage_environment
    from holophyte.environment_git import factory_identity
    wt = worktree_path(target, branch)
    if not wt.exists():
        return
    unstage_environment(target, wt)
    if sh(["git", "status", "--porcelain", "-uall", *paths(target)], cwd=wt):
        stage_work(target, wt)
        sh(["git", *factory_identity(wt), "commit", "-m",
            f"WIP: preserve work at operator {why}"], cwd=wt)

    return sh(["git", "rev-parse", "HEAD"], cwd=wt)


def boundary(conn, run_id, phase, **state):
    """Save the continuation inputs before honoring a pending stop."""
    _checkpoint.set((conn, run_id, phase, state))
    stop_if_requested(conn, run_id, phase)


def continuation(conn, run_id):
    """Read a prior paused run released by --resume, without consuming it."""
    if conn is None:
        return None
    row = conn.execute(
        "SELECT old.id, old.resumePhase, old.outcome FROM runs old JOIN runs current"
        " ON old.ticketId = current.ticketId WHERE current.id = ?"
        " AND old.id != current.id ORDER BY old.attempt DESC LIMIT 1",
        (run_id,)).fetchone()
    if (not row or row[2] != "paused"
            or row[1] not in ("working", "verifying", "reviewing", "addressing")):
        return None
    event = conn.execute("SELECT payload FROM runEvents WHERE runId = ?"
                         " AND kind = 'pause_checkpoint' ORDER BY id DESC LIMIT 1",
                         (row[0],)).fetchone()
    return {"phase": row[1], **(json.loads(event[0]) if event else {})}


def command(target, identifier, note, *, resume=False):
    """CLI adapter: request a stop, or release a paused continuation to claim."""
    from holophyte.operator import _operator_store, _ticket_by_identifier
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        if resume:
            resume_paused(target, conn, ticket_id, note)
        else:
            (run_id,) = conn.execute("SELECT COALESCE(activeRunId, lastRunId)"
                                     " FROM tickets WHERE id = ?",
                                     (ticket_id,)).fetchone()
            store.pause(conn, run_id, note)
        message = "ready to resume" if resume else "pause requested"
        print(f"[holo2] {identifier}: {message}")
    except (ValueError, store.ResumeRefused) as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    finally:
        conn.close()


def resume_paused(target, conn, ticket_id, note):
    """Release the ticket's paused run to claim, `note` on its resume
    intervention, then clear its pull request's pause notice; the run's
    id. `--resume` and `POST /actions/resume` both call this (KO-609);
    ValueError or `store.ResumeRefused`, before any write, when the
    ticket's latest run did not end paused."""
    with _transaction(conn):
        (run_id,) = conn.execute("SELECT COALESCE(activeRunId, lastRunId)"
                                 " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        old = conn.execute("SELECT outcome FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
        if old is None or old[0] != "paused":
            raise ValueError(f"run {run_id}: resume requires paused outcome")
        phase = store.resume(conn, run_id, note=note)
        # resume owns the re-entry decision; a new claim owns execution.
        store.release(conn, run_id, "paused", "released to resume",
                      resume_phase=phase)
        store.walk_ticket(conn, ticket_id, "ready")
        store.set_question(conn, ticket_id, None)
    from holophyte import pause_notice
    pause_notice.unmark(target, conn, run_id)
    return run_id


def abort_command(target, identifier, note, *, provider):
    """CLI adapter: record the abort, then end the run here when no worker
    can still touch its tree, and project the park to the board as the
    worker path does; otherwise a live worker ends it at its next heartbeat."""
    from holophyte import board
    from holophyte.operator import _operator_store, _ticket_by_identifier
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        (run_id,) = conn.execute("SELECT COALESCE(activeRunId, lastRunId)"
                                 " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if run_id is None:
            raise ValueError(f"{identifier} has no run to abort")
        store.abort(conn, run_id, note)
        if not worker_gone(conn, run_id):
            print(f"[holo2] {identifier}: abort requested; run {run_id} ends"
                  " at its worker's next heartbeat (this host cannot confirm"
                  " that worker gone; a silent one is the sweep's)")
            return
        try:
            end_aborted(conn, run_id)
        except Aborted:
            board.mirror_push(conn, ticket_id, provider)
            board.release_lease_label(target, conn, ticket_id, provider, run_id)
        print(f"[holo2] {identifier}: run {run_id} had no live worker;"
              " ended abandoned and parked")
    except ValueError as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    finally:
        conn.close()


def worker_gone(conn, run_id):
    """Whether no worker can still write the run's tree: the run is parked,
    or it was claimed on this host by a recorded process that no longer
    exists. A stale heartbeat proves nothing -- a slow worker, another
    host's pid, or a run with no recorded pid may still be writing -- so
    those leave the abort pending rather than commit under a live writer."""
    from holophyte.supervisor_lock import pid_alive
    phase, host, pid = conn.execute(
        "SELECT phase, host, workerPid FROM runs WHERE id = ?",
        (run_id,)).fetchone()
    if phase in store.PARKED_PHASES:
        return True
    return pid is not None and host == socket.gethostname() and not pid_alive(pid)


def pending_requests(conn):
    """Pending stops as run id -> (action, note); older read-only stores
    have no request column until their writer migrates."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "stopRequested" not in columns or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'interventions'").fetchone():
        return {}
    return {run: (action, note) for run, action, note in conn.execute(
        "SELECT r.id, i.action, i.guidance FROM runs r"
        " JOIN interventions i ON i.id = r.stopRequested WHERE r.endedAt IS NULL")}


def fix_state(sha, fixes, timed_out, addressed, model, pass_no, review_follows):
    """Serializable inputs for finishing an already-completed babysit fix turn."""
    return dict(step="babysit_fix", sha=sha, fixes=str(fixes), timed_out=timed_out,
                addressed=[(n, asdict(thread), reason)
                           for n, thread, reason in addressed],
                model=model, pass_no=pass_no, review_follows=review_follows, posted=0)


def resume_babysit_fix(target, conn, run_id, provider, task_id, branch, wt, sha,
                       beat_s, pull, ticket, verify_cmd, contracts, budget_min,
                       carried):
    """Finish the preserved fix before reading another babysit pass."""
    from holophyte.babysitter import _fix_threads
    from holophyte.pr import Comment, Thread
    row = conn.execute("SELECT payload FROM runEvents WHERE runId = ?"
                       " AND kind = 'pause_checkpoint' ORDER BY id DESC LIMIT 1",
                       (carried.run_id,)).fetchone()
    saved = json.loads(row[0]) if row and row[0] else {}
    if not carried.paused or saved.get("step") != "babysit_fix":
        return sha, False
    if sha != carried.sha:
        from holophyte.gates import RunFailure
        raise RunFailure("paused fix candidate moved; reconcile the preserved work")
    addressed = []
    for number, fields, reason in saved["addressed"]:
        fields = dict(fields, replies=tuple(Comment(**r) for r in fields["replies"]))
        addressed.append((number, Thread(**fields), reason))
    fixed = _fix_threads(target, conn, run_id, provider, task_id, branch, wt,
                         saved["sha"], beat_s, pull, addressed, saved["model"], ticket,
                         verify_cmd, contracts, budget_min, saved["pass_no"],
                         review_follows=saved["review_follows"], resume_step=saved)
    return fixed, True
