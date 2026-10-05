import json
import socket
from contextvars import ContextVar
from dataclasses import asdict

import store
from holophyte.config.project import Project, worktree_path
from store.schema import _transaction

_checkpoint = ContextVar("pause_checkpoint", default=None)
_route = ContextVar("pause_route", default=None)

ABORTS = ("abort", "abort_close")


def stop_if_requested(conn, run_id, phase):
    """End a marked run at its next phase; never kill a turn."""
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
    if action in ABORTS:
        end_aborted(conn, run_id)
    stopped_at, phase = phase, "merge_gate" if pr_url else phase
    target = Project.locate(repo)
    sha = preserve(target, branch) if branch else None
    with _transaction(conn):
        ended, outcome, reason = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if ended is not None:
            raise store.RunEnded(run_id, outcome, reason)
        saved = _checkpoint.get()
        state = saved[3] if saved and saved[:3] == (conn, run_id, phase) else {}
        route = _route.get()
        if route and route[:2] == (conn, run_id):
            state = {**route[2], **state}
        store.record_event(conn, run_id, "pause_checkpoint",
                           f"continuation at {phase}", level="detail",
                           payload=json.dumps(state))
        store.release(conn, run_id, "paused", note, resume_phase=phase,
                      candidate_sha=sha)
        store.walk_ticket(conn, ticket_id, "blocked_on_operator")
        store.set_question(conn, ticket_id, note)
    if pr_url:
        from holophyte.loop import pause_notice
        pause_notice.mark(target, conn, run_id, stopped_at)
    raise store.RunEnded(run_id, "paused", note)


class Aborted(store.RunEnded):
    pass


def abort_requested(conn, run_id):
    return conn.execute(
        "SELECT 1 FROM runs r JOIN interventions i ON i.id = r.stopRequested"
        " WHERE r.id = ? AND r.endedAt IS NULL"
        " AND i.action IN ('abort', 'abort_close')",
        (run_id,)).fetchone() is not None


def end_aborted(conn, run_id):
    from holophyte.loop.gates import InfraFailure
    from holophyte.pr import github
    branch, ticket_id, repo, pr_url, identifier = conn.execute(
        "SELECT r.branch, r.ticketId, p.repoPath, r.prUrl, t.linearIdentifier"
        " FROM runs r JOIN projects p ON p.id = r.projectId"
        " JOIN tickets t ON t.id = r.ticketId WHERE r.id = ?",
        (run_id,)).fetchone()
    target = Project.locate(repo)
    sha = preserve(target, branch, "abort") if branch else None
    if sha and pr_url:
        try:
            github.push_branch(target, branch)
        except InfraFailure as refused:
            store.record_event(conn, run_id, "warning", f"abort push: {refused}")
    with _transaction(conn):
        ended, outcome, reason = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if ended is not None:
            raise store.RunEnded(run_id, outcome, reason)
        note, action, source, trigger = conn.execute(
            'SELECT i.guidance, i.action, i.source, i."trigger" FROM runs r'
            " JOIN interventions i ON i.id = r.stopRequested WHERE r.id = ?",
            (run_id,)).fetchone()
        store.release(conn, run_id, "abandoned", note, candidate_sha=sha)
        if trigger in ("linear_cancelled", "board_cancelled"):
            store.walk_ticket(conn, ticket_id, "abandoned")
        else:
            store.walk_ticket(conn, ticket_id, "blocked_on_operator")
            store.set_question(conn, ticket_id, note)
    # Closed after the run ends, so a reconcile never finds a parked run to reject.
    if pr_url and action == "abort_close":
        from holophyte.babysit.babysitter import COMMENT_HEADER
        who = "the operator" if source == "human" else f"the {source}"
        kept = f"WIP commit {sha[:7]} on" if sha else "the"
        close_pull(target, conn, run_id, pr_url, (
            f"{COMMENT_HEADER.format(model='holophyte')}\n\n"
            f"Aborted by {who}: {note}\n\n"
            f"The work is kept as {kept} branch `{branch}`; start again with"
            f" `factory.py <repo> --requeue {identifier} --note TEXT`."))
    raise Aborted(run_id, "abandoned", note)


def close_pull(target, conn, run_id, pr_url, body):
    from holophyte.loop.gates import InfraFailure
    from holophyte.pr import github
    from holophyte.pr.pr_status import parse_pr_url
    pull = parse_pr_url(pr_url)
    steps = (("comment", lambda: github.comment_on_pull(target, pull, body)),
             ("close", lambda: github.rest(
                 target, pull, "PATCH",
                 f"repos/{pull.repo}/pulls/{pull.number}", {"state": "closed"})))
    for step, call in steps if pull else ():
        try:
            call()
        except InfraFailure as refused:
            store.record_event(conn, run_id, "warning",
                               f"abort {step} of {pr_url}: {refused}")
            print(f"[holo2] run {run_id}: abort {step} of {pr_url} failed:"
                  f" {refused}", flush=True)


def preserve(target, branch, why="pause"):
    from holophyte.environment_git import factory_identity
    from holophyte.loop.claim import paths, sh, stage_work, unstage_environment
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
    _checkpoint.set((conn, run_id, phase, state))
    stop_if_requested(conn, run_id, phase)


def keep_route(conn, run_id, **state):
    _route.set((conn, run_id, state))


def continuation(conn, run_id):
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
    from holophyte.cli.operator import _operator_store, _ticket_by_identifier
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
    from holophyte.loop import pause_notice
    pause_notice.unmark(target, conn, run_id)
    return run_id


def abort_command(target, identifier, note, *, provider, close=False):
    from holophyte.cli.operator import _operator_store, _ticket_by_identifier
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        (run_id,) = conn.execute("SELECT COALESCE(activeRunId, lastRunId)"
                                 " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if run_id is None:
            raise ValueError(f"{identifier} has no run to abort")
        if not abort_run(target, conn, run_id, note, provider=provider,
                         close=close):
            print(f"[holo2] {identifier}: abort requested; run {run_id} ends"
                  " at its worker's next heartbeat (this host cannot confirm"
                  " that worker gone; a silent one is the sweep's)")
            return
        print(f"[holo2] {identifier}: run {run_id} had no live worker;"
              " ended abandoned and parked")
    except ValueError as refused:
        raise SystemExit(f"[holo2] {refused}") from None
    finally:
        conn.close()


def abort_run(target, conn, run_id, note, *, provider, close=False,
              source="human", trigger="manual"):
    from holophyte.board import projection
    store.abort(conn, run_id, note, source=source, close=close,
                trigger=trigger)
    if not worker_gone(conn, run_id):
        return False
    try:
        end_aborted(conn, run_id)
    except Aborted:
        (ticket_id,) = conn.execute("SELECT ticketId FROM runs WHERE id = ?",
                                    (run_id,)).fetchone()
        projection.mirror_push(conn, ticket_id, provider)
        projection.release_lease_label(target, conn, ticket_id, provider, run_id)
    return True


def worker_gone(conn, run_id):
    """A stale heartbeat proves nothing, so an unproven worker keeps the abort."""
    from holophyte.host.supervisor_lock import pid_alive
    phase, host, pid = conn.execute(
        "SELECT phase, host, workerPid FROM runs WHERE id = ?",
        (run_id,)).fetchone()
    if phase in store.PARKED_PHASES:
        return True
    return pid is not None and host == socket.gethostname() and not pid_alive(pid)


def pending_requests(conn):
    """Older read-only stores have no request column until their writer migrates."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if "stopRequested" not in columns or not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'interventions'").fetchone():
        return {}
    return {run: (action, note) for run, action, note in conn.execute(
        "SELECT r.id, i.action, i.guidance FROM runs r"
        " JOIN interventions i ON i.id = r.stopRequested WHERE r.endedAt IS NULL")}


def fix_state(sha, fixes, timed_out, addressed, model, pass_no, review_follows):
    return dict(step="babysit_fix", sha=sha, fixes=str(fixes), timed_out=timed_out,
                addressed=[(n, asdict(thread), reason)
                           for n, thread, reason in addressed],
                model=model, pass_no=pass_no, review_follows=review_follows, posted=0)


def resume_babysit_fix(target, conn, run_id, provider, task_id, branch, wt, sha,
                       beat_s, pull, ticket, verify_cmd, contracts, budget_min,
                       carried):
    from holophyte.babysit.babysitter import _fix_threads
    from holophyte.pr.github import Comment, Thread
    row = conn.execute("SELECT payload FROM runEvents WHERE runId = ?"
                       " AND kind = 'pause_checkpoint' ORDER BY id DESC LIMIT 1",
                       (carried.run_id,)).fetchone()
    saved = json.loads(row[0]) if row and row[0] else {}
    if not carried.paused or saved.get("step") != "babysit_fix":
        return sha, False
    if sha != carried.sha:
        from holophyte.loop.gates import RunFailure
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
