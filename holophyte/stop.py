"""Cooperative run stops and their durable continuation at stage boundaries."""
import json
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
        "SELECT r.endedAt, r.branch, r.ticketId, p.repoPath, i.guidance, r.prUrl"
        " FROM runs r JOIN projects p ON p.id = r.projectId"
        " JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
        (run_id,)).fetchone()
    if row is None or row[0] is not None:
        return
    _, branch, ticket_id, repo, note, pr_url = row
    phase = "merge_gate" if pr_url else phase
    sha = preserve(Target.locate(repo), branch) if branch else None
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
    raise store.RunEnded(run_id, "paused", note)


def preserve(target, branch):
    """Reuse the reclaim path's environment exclusions and staging policy."""
    from holophyte.claim import paths, sh, stage_work, unstage_environment
    wt = worktree_path(target, branch)
    if not wt.exists():
        return
    unstage_environment(target, wt)
    if sh(["git", "status", "--porcelain", "-uall", *paths(target)], cwd=wt):
        stage_work(target, wt)
        sh(["git", "-c", "user.name=holophyte",
            "-c", "user.email=holophyte@factory.invalid", "commit", "-m",
            "WIP: preserve work at operator pause"], cwd=wt)

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
        with _transaction(conn):
            row = conn.execute("SELECT COALESCE(activeRunId, lastRunId)"
                               " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            run_id = row[0]
            if not resume:
                store.pause(conn, run_id, note)
            else:
                old = conn.execute("SELECT outcome, resumePhase FROM runs WHERE id = ?",
                                   (run_id,)).fetchone()
                if old is None or old[0] != "paused":
                    raise ValueError(f"run {run_id}: --resume requires paused outcome")
                phase = store.resume(conn, run_id)
                # resume owns the re-entry decision; a new claim owns execution.
                store.release(conn, run_id, "paused", "released to resume",
                              resume_phase=phase)
                store.walk_ticket(conn, ticket_id, "ready")
                store.set_question(conn, ticket_id, None)
        message = "ready to resume" if resume else "pause requested"
        print(f"[holo2] {identifier}: {message}")
    except (ValueError, store.ResumeRefused) as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    finally:
        conn.close()


def pending_requests(conn):
    """Older read-only stores have no request column until their writer migrates."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "stopRequested" not in columns or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'interventions'").fetchone():
        return {}
    return dict(conn.execute("SELECT r.id, i.guidance FROM runs r"
                             " JOIN interventions i ON i.id = r.stopRequested"
                             " WHERE r.endedAt IS NULL"))


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
