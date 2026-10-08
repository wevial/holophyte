"""store.operate: the operator API, the writes that end, resume and requeue runs."""
from __future__ import annotations

import getpass
import re
import socket
import time

from . import _append_event, _redact_values, record_ledger, set_phase
from . import enums as _enums
from .enums import RESUMABLE_WORK_PHASES, RUN_PHASE_TRANSITIONS  # noqa: F401
from .enums import RunPhase as _Phase
from .schema import _transaction
from .tickets import walk_ticket
from .writes import _bounded_reason

TERMINAL_PHASES = {
    "paused": "paused",
    _enums.RunOutcome.REJECTED.value: _Phase.REJECTED.value,
    _enums.RunOutcome.MERGED.value: _Phase.DONE.value,
    _enums.RunOutcome.KILLED.value: _Phase.KILLED.value,
    _enums.RunOutcome.ABANDONED.value: _Phase.FAILED.value,
    _enums.RunOutcome.FAILED.value: _Phase.FAILED.value,
}

ENDED_PHASES = frozenset(TERMINAL_PHASES.values())

OUTCOME_CLASSES = frozenset(e.value for e in _enums.OutcomeClass)


def release(conn, run_id, outcome, reason=None, now=None,
            outcome_class="work", merge_sha=None, failure_kind=None, resume_phase=None,
            candidate_sha=None):
    """End run `run_id` with `outcome` and give the ticket's lease back."""
    failure_kind = (_enums.FailureKind(failure_kind or "unclassified").value
                    if outcome == "failed" else None)
    reason = _redact_values(reason) if reason is not None else None
    if outcome not in TERMINAL_PHASES:
        raise ValueError(f"unknown outcome {outcome!r}")
    if outcome_class not in OUTCOME_CLASSES:
        raise ValueError(f"unknown outcome class {outcome_class!r}")
    if merge_sha is not None and outcome != "merged":
        raise ValueError(
            f"merge_sha {merge_sha!r} on a run released as {outcome!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT ticketId, endedAt, phase FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ticket_id, ended_at, phase = row
        # Ended already: a second ending would overwrite its outcome and resumePhase.
        if ended_at is not None and phase in ENDED_PHASES:
            return
        reason = _bounded_reason(conn, run_id, reason, now)
        stopped_in = set_phase(conn, run_id, TERMINAL_PHASES[outcome],
                               note=f"run ended, outcome {outcome}", now=now)
        # The last moment the phase a failed run left is still known.
        resume_phase = resume_phase if outcome == "paused" else (stopped_in
                        if TERMINAL_PHASES[outcome] == "failed"
                        and stopped_in in RESUMABLE_WORK_PHASES
                        else None)
        conn.execute(
            "UPDATE runs SET endedAt = ?, outcome = ?, outcomeReason = ?,"
            " outcomeClass = ?, resumePhase = ?, mergeSha = ?, failureKind = ?,"
            " candidateSha = COALESCE(?, candidateSha),"
            " reviewRoundCount = (SELECT COUNT(*) FROM reviewRounds"
            "                     WHERE runId = ? AND verdict != 'error')"
            " WHERE id = ?",
            (now, outcome, reason, outcome_class, resume_phase, merge_sha,
             failure_kind, candidate_sha, run_id, run_id),
        )
        conn.execute(
            "UPDATE tickets SET activeRunId = NULL, lastRunId = ?"
            " WHERE id = ? AND activeRunId = ?",
            (run_id, ticket_id, run_id),
        )


class RequeueRefused(Exception):
    pass


# The loop composes the merge gate's conflict reason from this prefix.
GATE_CONFLICT_REASON = "merging main into "


def is_gate_conflict(reason):
    return (reason or "").startswith(GATE_CONFLICT_REASON) \
        and " conflicted on: " in reason


def requeue(conn, ticket_id, note, now=None, source="human", force=False):
    """Walk a failed, rejected or aborted ticket back to ready; return its last run."""
    with _transaction(conn):
        row = conn.execute(
            "SELECT linearIdentifier, status, activeRunId, lastRunId, boardState"
            " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise RequeueRefused(f"ticket {ticket_id} does not exist")
        identifier, status, active_run_id, last_run_id, board_state = row
        if board_state in ("Backlog", "Canceled", "Done"):
            raise RequeueRefused(
                f"{identifier}: board state is {board_state}; nothing to requeue")
        if active_run_id is not None:
            raise RequeueRefused(
                f"{identifier}: run {active_run_id} is still live;"
                " a requeue is for a ticket whose run has ended")
        run = (conn.execute("SELECT outcome, phase, prUrl, parkKind FROM runs"
                            " WHERE id = ?", (last_run_id,)).fetchone()
               if last_run_id is not None else None)
        unreproduced = _requeue_admits(identifier, status, last_run_id, run,
                                       _aborted(conn, last_run_id))
        relaunched = _relaunches(conn, ticket_id)
        if len(relaunched) >= RELAUNCH_LIMIT and not force:
            newest, older = relaunched[0], relaunched[1]
            raise RequeueRefused(
                f"{identifier} was relaunched {len(relaunched)} times against"
                f" one failure since a human last acted on it (runs {older}"
                f" and {newest} each ended failed or rejected and was"
                " requeued); another"
                " relaunch needs a written diagnosis, not another run: write"
                " the diagnosis as the note and requeue with --force"
                ' ("force": true over HTTP)')
        if len(relaunched) >= RELAUNCH_LIMIT:
            note = f"forced past {len(relaunched)} relaunches: {note}"
        record_intervention(conn, last_run_id, "requeue", note, source=source,
                            now=now)
        if unreproduced:
            release(conn, last_run_id, "abandoned",
                    "not reproduced; requeued for another attempt", now=now)
        conn.execute("UPDATE tickets SET blockedQuestion = NULL"
                     " WHERE id = ?", (ticket_id,))
        conn.execute("UPDATE runs SET approvedAt = NULL, approvedBy = NULL"
                     " WHERE id = ?", (last_run_id,))
        walk_ticket(conn, ticket_id, "ready")
    return last_run_id


RELAUNCH_LIMIT = 2


def _relaunches(conn, ticket_id):
    rows = conn.execute(
        'SELECT i.runId FROM interventions i JOIN runs r ON r.id = i.runId'
        " WHERE r.ticketId = ? AND i.\"action\" = 'requeue'"
        " AND r.outcome IN ('failed', 'rejected')"
        " AND i.id > COALESCE((SELECT MAX(h.id) FROM interventions h"
        "  JOIN runs hr ON hr.id = h.runId WHERE hr.ticketId = ?"
        "  AND h.source = 'human' AND h.\"action\" != 'requeue'), 0)"
        " ORDER BY i.id DESC", (ticket_id, ticket_id)).fetchall()
    return [run_id for (run_id,) in rows]


def _aborted(conn, run_id):
    return run_id is not None and conn.execute(
        'SELECT 1 FROM interventions WHERE runId = ? AND "action" IN (?, ?)',
        (run_id, _enums.InterventionAction.ABORT.value,
         _enums.InterventionAction.ABORT_CLOSE.value)).fetchone() is not None


def _requeue_admits(identifier, status, last_run_id, run, aborted):
    parked = status == "blocked_on_operator"
    if parked and run is not None and run[1] == "awaiting_merge_approval":
        if run[3] == _enums.ParkKind.NOT_REPRODUCED.value:
            return True
        command = "--babysit" if run[2] else "--approve or --babysit"
        raise RequeueRefused(
            f"{identifier} is parked awaiting merge approval; use {command}")
    if run is not None and run[0] == "abandoned" and not aborted:
        raise RequeueRefused(
            f"{identifier}: run {last_run_id} ended abandoned but was not"
            " aborted; nothing to requeue")
    ended = ("failed", "rejected", "abandoned")
    if status != "in_flight" and not (
            parked and run is not None and run[0] in ended):
        raise RequeueRefused(
            f"{identifier} is {status}, not in_flight; nothing to requeue")
    if run is None:
        raise RequeueRefused(
            f"{identifier} has no ended run to requeue after")
    if run[0] not in ended:
        raise RequeueRefused(
            f"{identifier}: run {last_run_id} ended {run[0]},"
            " not failed, rejected or aborted; nothing to requeue")
    return False


class ApproveRefused(Exception):
    pass


APPROVED_RESUME_PHASE = "merge_gate"


def approve(conn, ticket_id, note, now=None, run_id=None):
    return _release_parked(
        conn, ticket_id, "approve", note,
        "approved for merge; the next claim resumes the candidate"
        " at the merge gate", now, run_id=run_id)


def babysit(conn, ticket_id, note, now=None, source="human"):
    return _release_parked(
        conn, ticket_id, "babysit", note,
        "sent back to the babysitter; the next claim resumes the candidate"
        " on its pull request", now, require_pr=True, source=source)


def _release_parked(conn, ticket_id, action, note, reason, now,
                    require_pr=False, source="human", guidance=None,
                    before_release=None, run_id=None):
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT linearIdentifier, status, activeRunId, lastRunId"
            " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise ApproveRefused(f"ticket {ticket_id} does not exist")
        identifier, status, active_run_id, last_run_id = row
        if active_run_id is not None:
            raise ApproveRefused(
                f"{identifier} is {status} with run {active_run_id} still"
                " live; an approval is for a run parked awaiting merge"
                " approval")
        if status != "blocked_on_operator":
            raise ApproveRefused(
                f"{identifier} is {status}, not blocked_on_operator; nothing"
                " is parked awaiting merge approval")
        if run_id is not None and last_run_id != run_id:
            raise ApproveRefused(
                f"{identifier}'s newest run is {last_run_id}, not run"
                f" {run_id}; nothing approved")
        run = (conn.execute("SELECT phase, prUrl FROM runs WHERE id = ?",
                            (last_run_id,)).fetchone()
               if last_run_id is not None else None)
        if run is None:
            raise ApproveRefused(
                f"{identifier} is {status} and has no run; nothing is"
                " parked awaiting merge approval")
        phase, pr_url = run
        if phase != "awaiting_merge_approval":
            raise ApproveRefused(
                f"{identifier} is {status} and its newest run {last_run_id}"
                f" is {phase}, not awaiting_merge_approval; nothing to"
                " approve")
        if require_pr and pr_url is None:
            raise ApproveRefused(
                f"{identifier} is parked with no pull request (run"
                f" {last_run_id} was parked under [merge] mode = \"local\");"
                " there are no threads to shepherd, and a release here would"
                " merge the candidate -- that is --approve's to say")
        record_intervention(conn, last_run_id, action, note, now=now,
                            source=source, guidance=guidance)
        if before_release is not None:
            before_release()
        # Never a strike: the escalation count reads `failed` only.
        release(conn, last_run_id, "abandoned", reason, now=now)
        # After `release()`, which records a resume point for failed runs only.
        conn.execute("UPDATE runs SET resumePhase = ?, approvedAt = ?,"
                     " approvedBy = ? WHERE id = ?",
                     (APPROVED_RESUME_PHASE, now if action == "approve" else None,
                      getpass.getuser() if action == "approve" else None,
                      last_run_id))
        if action in ("babysit", "operator_note"):
            conn.execute("UPDATE tickets SET blockedQuestion = NULL WHERE id = ?",
                         (ticket_id,))
        walk_ticket(conn, ticket_id, "ready")
    return last_run_id


class RepointRefused(Exception):
    pass


FULL_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


def repoint(conn, ticket_id, sha, note, now=None):
    if not isinstance(sha, str) or not FULL_SHA.match(sha):
        raise RepointRefused(
            f"ticket {ticket_id}: {sha!r} is not a full 40-hex commit id;"
            " a re-point names the exact commit the gate will hold the"
            " branch to")
    sha = sha.lower()
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT linearIdentifier, status, activeRunId, lastRunId"
            " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None:
            raise RepointRefused(f"ticket {ticket_id} does not exist")
        identifier, status, active_run_id, last_run_id = row
        if active_run_id is not None:
            raise RepointRefused(
                f"{identifier} is {status} with run {active_run_id} still"
                " live; a re-point is for a run parked awaiting merge"
                " approval")
        run = (conn.execute("SELECT phase, candidateSha, resumePhase FROM"
                            " runs WHERE id = ?", (last_run_id,)).fetchone()
               if last_run_id is not None else None)
        if run is None:
            raise RepointRefused(
                f"{identifier} is {status} and has no run; nothing is"
                " parked awaiting merge approval")
        phase, old_sha, resume_phase = run
        if resume_phase is not None:
            raise RepointRefused(
                f"{identifier} is {status} and its newest run {last_run_id}"
                f" is already approved (resumes at {resume_phase}); its"
                " release is in flight, so requeue instead of re-pointing")
        if phase != "awaiting_merge_approval":
            raise RepointRefused(
                f"{identifier} is {status} and its newest run {last_run_id}"
                f" is {phase}, not awaiting_merge_approval; nothing to"
                " re-point")
        record_intervention(conn, last_run_id, "repoint", note,
                            guidance=note, now=now)
        _append_event(conn, last_run_id, "narrative", "repoint",
                      f"candidate re-pointed from {old_sha} to {sha}: {note}",
                      now)
        conn.execute("UPDATE runs SET candidateSha = ? WHERE id = ?",
                     (sha, last_run_id))
    return last_run_id, old_sha


# Staleness is the supervisor's judgement: resume is always safe to attempt.
RESUMABLE_PHASES = RESUMABLE_WORK_PHASES | {
    _Phase.FAILED.value, _Phase.BLOCKED_ON_OPERATOR.value, "paused"}
# No heartbeat by design, so the supervisor sweep leaves these alone.
PARKED_PHASES = frozenset({
    _Phase.BLOCKED_ON_OPERATOR.value, _Phase.AWAITING_MERGE_APPROVAL.value})

class ResumeRefused(Exception):
    pass


def pause(conn, run_id, note, source="human", now=None):
    with _transaction(conn):
        row = conn.execute("SELECT endedAt, outcome, stopRequested FROM runs"
                           " WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        if row[0] is not None:
            raise ValueError(f"run {run_id} already ended with outcome {row[1]}")
        if row[2] is not None:
            return row[2]
        request = record_intervention(conn, run_id, "pause", note,
                                      source=source, guidance=note, now=now)
        conn.execute("UPDATE runs SET stopRequested = ? WHERE id = ?",
                     (request, run_id))
    return request


_CANCEL_TRIGGERS = ("linear_cancelled", "board_cancelled")


def abort(conn, run_id, note, source="human", now=None, close=False, trigger="manual"):
    with _transaction(conn):
        row = conn.execute(
            "SELECT r.endedAt, r.outcome, r.phase, r.stopRequested, i.action,"
            ' i."trigger" FROM runs r'
            " LEFT JOIN interventions i ON i.id = r.stopRequested"
            " WHERE r.id = ?", (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ended, outcome, phase, pending, action, pending_trigger = row
        if ended is not None:
            raise ValueError(f"run {run_id} already ended with outcome {outcome}")
        if TERMINAL_PHASES["abandoned"] not in RUN_PHASE_TRANSITIONS[phase]:
            raise ValueError(f"run {run_id} is {phase}; it cannot end abandoned")
        wanted = "abort_close" if close or action == "abort_close" else "abort"
        if pending_trigger in _CANCEL_TRIGGERS:
            trigger = pending_trigger
        cancels = (trigger in _CANCEL_TRIGGERS
                   and pending_trigger not in _CANCEL_TRIGGERS)
        if action in (wanted, "abort_close") and not cancels:
            return pending
        request = record_intervention(conn, run_id, wanted, note, source=source,
                                      trigger=trigger, guidance=note, now=now)
        conn.execute("UPDATE runs SET stopRequested = ? WHERE id = ?",
                     (request, run_id))
    return request


def resume(conn, run_id, guidance=None, source="human", now=None, note=None):
    """Resume `run_id`, optionally with `guidance`; return the phase re-entered."""
    if guidance is not None and (
        not isinstance(guidance, str) or not guidance.strip()
    ):
        raise ValueError(f"guidance must be non-empty text or None, got {guidance!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT phase, resumePhase FROM runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ResumeRefused(f"run {run_id} does not exist")
        phase, resume_phase = row
        # Asked first: when both rules are broken, the injection is the one to name.
        if guidance is not None and phase != "blocked_on_operator":
            from . import GuidanceNotAccepted
            raise GuidanceNotAccepted(
                f"run {run_id} is in phase {phase}, not blocked_on_operator:"
                " guidance is only accepted by a run that asked for it"
            )
        if phase not in RESUMABLE_PHASES:
            raise ResumeRefused(
                f"run {run_id}: phase {phase} is not one state-model §5 resumes"
            )
        if phase in ("failed", "paused") and resume_phase is not None:
            target = resume_phase
        elif phase in ("failed", "blocked_on_operator"):
            target = "working"
        else:
            target = phase
        # No heartbeat: this call has no evidence the run is alive.
        conn.execute(
            "UPDATE runs SET phase = ?, resumePhase = NULL, parkKind = NULL,"
            " stopRequested = NULL"
            " WHERE id = ?",
            (target, run_id),
        )
        if phase in ENDED_PHASES:
            conn.execute(
                "UPDATE runs SET endedAt = NULL, outcome = NULL,"
                " outcomeReason = NULL, failureKind = NULL,"
                " outcomeClass = 'work' WHERE id = ?",
                (run_id,),
            )
        conn.execute(
            'INSERT INTO interventions'
            ' (runId, source, "trigger", "action", guidance, note, at)'
            " VALUES (?, ?, 'manual', 'resume', ?, ?, ?)",
            (run_id, source, guidance, note, now),
        )
    return target


INTERVENTION_SOURCES = tuple(e.value for e in _enums.InterventionSource)
INTERVENTION_TRIGGERS = tuple(e.value for e in _enums.InterventionTrigger)
INTERVENTION_ACTIONS = tuple(e.value for e in _enums.InterventionAction)


def _validate_intervention(action, note, source, trigger):
    for kind, value, allowed in (("action", action, INTERVENTION_ACTIONS),
                                 ("source", source, INTERVENTION_SOURCES),
                                 ("trigger", trigger, INTERVENTION_TRIGGERS)):
        if value not in allowed:
            raise ValueError(f"unknown intervention {kind} {value!r}")
    if not isinstance(note, str) or not note.strip():
        raise ValueError(f"note must be non-empty text, got {note!r}")


def record_intervention(conn, run_id, action, note, source="human",
                        trigger="manual", question=None, guidance=None,
                        now=None):
    """Record an operator or supervisor decision on a run; return its id."""
    _validate_intervention(action, note, source, trigger)
    if action == "redirect" and (
            not isinstance(question, str) or not question.strip()):
        raise ValueError("a redirect records the question it asked;"
                         f" got {question!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?",
                        (run_id,)).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            'INSERT INTO interventions'
            ' (runId, source, "trigger", "action", question, guidance, note, at)'
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, source, trigger, action, question, guidance,
             None if action == _enums.InterventionAction.MIGRATE else note, now))
        _append_event(conn, run_id, "narrative", "intervention",
                      f"{source} {action}: {note}", now)
        record_ledger(conn, run_id, "intervention",
                      f"{source} {action}: {note}",
                      source="operator" if source == "human" else "loop",
                      now=now)
    return cursor.lastrowid


def record_strike(conn, run_id, stale, heartbeat, now=None):
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        if not stale:
            conn.execute("DELETE FROM sweepStrikes WHERE runId = ?", (run_id,))
            strikes = 0
        else:
            row = conn.execute(
                "SELECT strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
                (run_id,)
            ).fetchone()
            # A heartbeat since the strike on file starts the silence over.
            if row is None or heartbeat > row[1]:
                strikes = 1
            else:
                strikes = row[0] + 1
            conn.execute(
                "INSERT INTO sweepStrikes (runId, strikes, lastSeen)"
                " VALUES (?, ?, ?)"
                " ON CONFLICT (runId) DO UPDATE SET strikes = excluded.strikes,"
                " lastSeen = excluded.lastSeen",
                (run_id, strikes, now),
            )
    return strikes


def record_supervisor_heartbeat(conn, pid, started_at, now=None):
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        conn.execute(
            # Keyed with startedAt too, because pids are reused.
            "INSERT INTO supervisorHeartbeats"
            " (pid, startedAt, lastBeat, passes, host)"
            " VALUES (?, ?, ?, 1, ?)"
            " ON CONFLICT (pid, startedAt) DO UPDATE SET"
            "   lastBeat = excluded.lastBeat, passes = passes + 1,"
            "   host = excluded.host",
            (pid, started_at, now, socket.gethostname()),
        )
        return conn.execute(
            "SELECT passes FROM supervisorHeartbeats"
            " WHERE pid = ? AND startedAt = ?", (pid, started_at)).fetchone()[0]


def latest_supervisor_heartbeat(conn):
    row = conn.execute(
        "SELECT pid, startedAt, lastBeat, passes, host"
        " FROM supervisorHeartbeats"
        " ORDER BY lastBeat DESC, startedAt DESC LIMIT 1").fetchone()
    return tuple(row) if row is not None else None


def record_loop_restart(conn, project_id, sha, now=None):
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        cursor = conn.execute(
            "INSERT INTO loopRestarts (projectId, sha, at) VALUES (?, ?, ?)",
            (project_id, sha, now))
        return cursor.lastrowid


def record_loop_return(conn, project_id, now=None):
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        return conn.execute(
            "UPDATE loopRestarts SET returnedAt = ?"
            " WHERE projectId = ? AND returnedAt IS NULL",
            (now, project_id)).rowcount


def unreturned_loop_restarts(conn, grace_ms, now=None):
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        rows = conn.execute(
            "SELECT id, projectId, sha, at FROM loopRestarts"
            " WHERE returnedAt IS NULL AND reportedAt IS NULL"
            "   AND at <= ?"
            # A claim stamps its run's heartbeat, so it counts as a return.
            "   AND NOT EXISTS (SELECT 1 FROM runs"
            "                   WHERE runs.projectId = loopRestarts.projectId"
            "                     AND runs.lastHeartbeat > loopRestarts.at)"
            " ORDER BY at, id", (now - grace_ms,)).fetchall()
        conn.executemany(
            "UPDATE loopRestarts SET reportedAt = ? WHERE id = ?",
            [(now, row[0]) for row in rows])
        return [(row[0], row[1], row[2], now - row[3]) for row in rows]


def hold(conn, project_id, note):
    from .tickets import set_admission
    return set_admission(conn, project_id, "held", note)


def release_hold(conn, project_id, note):
    from .tickets import set_admission
    return set_admission(conn, project_id, "enabled", note)


def _set_admission(conn, project_id, note, state, action):
    if not isinstance(note, str) or not note.strip():
        raise ValueError("note must be non-empty text")
    with _transaction(conn):
        row = conn.execute(
            "SELECT repoPath, admission, holdNote FROM projects WHERE id = ?",
            (project_id,)).fetchone()
        if row is None:
            raise ValueError(f"no project {project_id}")
        if row[1] == state:
            raise ValueError(f"project {row[0]} already {state}: {row[2] or ''}")
        intervention = conn.execute(
            'INSERT INTO interventions (projectId, source, "trigger", action, note, at)'
            " VALUES (?, 'human', 'manual', ?, ?, ?)",
            (project_id, action, note, int(time.time() * 1000))).lastrowid
        conn.execute("UPDATE projects SET admission = ?, holdNote = ? WHERE id = ?",
                     (state, note if state != "enabled" else None, project_id))
        return intervention


def record_project_intervention(conn, action, note, source="human",
                                trigger="manual", project_id=None, now=None):
    """Record a decision on the project rather than one run; return its id."""
    _validate_intervention(action, note, source, trigger)
    with _transaction(conn):
        projects = [row[0] for row in conn.execute("SELECT id FROM projects")]
        if project_id is None and len(projects) == 1:
            project_id = projects[0]
        if project_id is not None and project_id not in projects:
            raise ValueError(f"no project {project_id}")
        if project_id is None and action != "migrate":
            raise ValueError(f"{action} needs a project and the store has"
                             f" {len(projects)}; pass project_id")
        return conn.execute(
            'INSERT INTO interventions (projectId, source, "trigger", action,'
            " note, at) VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, source, trigger, action, note,
             now if now is not None else int(time.time() * 1000))).lastrowid


from .repair import repair_references  # noqa: E402,F401
