from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

import store.schema
from store.run_reads import (  # noqa: F401
    SQLITE_INT64_MAX,
    SQLITE_INT64_MIN,
    ApprovedCandidate,
    ChainRun,
    EndedRun,
    FailedAttempt,
    LiveRun,
    MergedRun,
    ParkFacts,
    RecentFailedRun,
    RunSnapshot,
    Toil,
    approved_candidate,
    babysit_note,
    ended_runs,
    failed_attempts_since,
    finished_runs,
    last_independent_verdict,
    latest_human_intervention_at,
    live_runs,
    merged_runs,
    newest_run_id,
    park_facts,
    recent_failed_runs,
    run_chains,
    run_snapshot,
    stranded_runs,
    toil_since,
)

_lock_wait = ContextVar("read_lock_wait", default=None)


@contextmanager
def lock_wait(seconds):
    """Per context: no other thread's reads change, and a writer still queues."""
    token = _lock_wait.set(seconds)
    try:
        yield
    finally:
        _lock_wait.reset(token)


def open_readonly(path) -> sqlite3.Connection:
    """`row_factory` stays unset: each read's tuple shape is its contract."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    wait = _lock_wait.get()
    return sqlite3.connect(uri, uri=True, timeout=(
        store.schema.BUSY_TIMEOUT_S if wait is None else wait))


@dataclass(frozen=True)
class Ticket:
    id: int
    linearIssueId: str
    linearIdentifier: str
    status: str
    activeRunId: int | None
    lastRunId: int | None
    blockedQuestion: str | None = None
    boardState: str | None = None
    pushState: str | None = None
    pushFrom: str | None = None
    pushAt: int | None = None


def ticket_by_id(conn, ticket_id):
    row = conn.execute(
        "SELECT id, linearIssueId, linearIdentifier, status,"
        " activeRunId, lastRunId, blockedQuestion, boardState,"
        " pushState, pushFrom, pushAt FROM tickets WHERE id = ?",
        (ticket_id,)).fetchone()
    if row is None:
        return None
    return Ticket(id=row[0], linearIssueId=row[1], linearIdentifier=row[2],
                  status=row[3], activeRunId=row[4], lastRunId=row[5],
                  blockedQuestion=row[6], boardState=row[7],
                  pushState=row[8], pushFrom=row[9], pushAt=row[10])


@dataclass(frozen=True)
class BlockedTicket:
    id: int
    linearIdentifier: str
    blockedQuestion: str | None
    runId: int | None = None
    askedMs: int | None = None
    prUrl: str | None = None
    parkKind: str | None = None
    prSeenChecks: str | None = None
    prSeenReview: str | None = None
    prSeenThreads: int | None = None
    prSeenTitle: str | None = None
    title: str | None = None
    ticketUrl: str | None = None
    boardState: str | None = None
    outcome: str | None = None


def blocked_tickets(conn, project_id=None):
    where, params = "t.status = 'blocked_on_operator'", ()
    if project_id is not None:
        where += " AND t.projectId = ?"
        params = (project_id,)
    rows = conn.execute(
        "SELECT t.id, t.linearIdentifier, t.blockedQuestion, r.id,"
        " (SELECT MAX(i.at) FROM interventions i"
        "  WHERE i.runId = r.id AND i.\"action\" = 'redirect'),"
        " r.lastHeartbeat, r.prUrl, r.prSeenChecks, r.prSeenReview,"
        " r.prSeenThreads, t.url, t.boardState, r.parkKind, r.prSeenTitle,"
        " t.title, r.outcome"
        " FROM tickets t LEFT JOIN runs r ON r.id = t.lastRunId"
        f" WHERE {where} ORDER BY t.id", params).fetchall()
    return [BlockedTicket(id=row[0], linearIdentifier=row[1],
                          blockedQuestion=row[2], runId=row[3],
                          askedMs=row[4] if row[4] is not None else row[5],
                          prUrl=row[6], prSeenChecks=row[7],
                          prSeenReview=row[8], prSeenThreads=row[9], ticketUrl=row[10],
                          boardState=row[11], parkKind=row[12],
                          prSeenTitle=row[13], title=row[14], outcome=row[15])
            for row in rows]


@dataclass(frozen=True)
class OpenTicket:
    id: int
    linearIdentifier: str
    title: str
    status: str
    timeBoxMs: int | None
    activeRunId: int | None
    blockedQuestion: str | None
    waitsOn: tuple[str, ...]
    mirroredAt: int
    ticketUrl: str | None = None
    boardColumn: str | None = None
    priority: int | None = None
    labels: tuple[str, ...] = ()
    revision: int = 0


def open_tickets(conn, project_id=None):
    scope = "" if project_id is None else " AND projectId = ?"
    params = () if project_id is None else (project_id,)
    rows = conn.execute(
        "SELECT id, linearIssueId, linearIdentifier, title, status,"
        " timeBoxMs, activeRunId, blockedQuestion, dependsOn, mirroredAt, url,"
        " boardColumn, priority, labels, revision"
        " FROM tickets WHERE status NOT IN ('merged', 'abandoned')"
        + scope + " ORDER BY linearIdentifier", params).fetchall()
    mirrored = {row[1]: row[2] for row in rows}
    closed = {row[0] for row in conn.execute(
        "SELECT linearIssueId FROM tickets"
        " WHERE status IN ('merged', 'abandoned')")}
    return [OpenTicket(id=row[0], linearIdentifier=row[2], title=row[3],
                       status=row[4], timeBoxMs=row[5], activeRunId=row[6],
                       blockedQuestion=row[7],
                       waitsOn=tuple(mirrored.get(dep, dep)
                                     for dep in json.loads(row[8])
                                     if dep not in closed),
                       mirroredAt=row[9], ticketUrl=row[10],
                       boardColumn=row[11], priority=row[12],
                       labels=tuple(json.loads(row[13])), revision=row[14])
            for row in rows]


@dataclass(frozen=True)
class MirroredTicket:
    id: int
    linearIdentifier: str
    title: str
    status: str
    body: str
    acceptanceCriteria: tuple[str, ...]
    verificationCommands: tuple[str, ...]
    timeBoxMs: int | None
    activeRunId: int | None
    mirroredAt: int
    ticketUrl: str | None = None
    revision: int = 0
    claimedRevision: int | None = None
    claimedSnapshot: str | None = None


def ticket_by_identifier(conn, identifier):
    row = conn.execute(
        "SELECT t.id, t.linearIdentifier, t.title, t.status, t.body,"
        " t.acceptanceCriteria, t.verificationCommands, t.timeBoxMs,"
        " t.activeRunId, t.mirroredAt, t.url, t.revision, r.revision,"
        " r.ticketSnapshot FROM tickets t LEFT JOIN runs r"
        " ON r.id = t.activeRunId WHERE t.linearIdentifier = ?",
        (identifier,)).fetchone()
    if row is None:
        return None
    return MirroredTicket(id=row[0], linearIdentifier=row[1], title=row[2],
                          status=row[3], body=row[4],
                          acceptanceCriteria=tuple(json.loads(row[5])),
                          verificationCommands=tuple(json.loads(row[6])),
                          timeBoxMs=row[7], activeRunId=row[8],
                          mirroredAt=row[9], ticketUrl=row[10],
                          revision=row[11], claimedRevision=row[12],
                          claimedSnapshot=row[13])


@dataclass(frozen=True)
class TicketRevision:
    revision: int
    at: int
    author: str
    title: str
    body: str
    priority: int | None
    labels: tuple[str, ...]
    column: str | None


def ticket_revisions(conn, ticket_id):
    return [TicketRevision(revision=row[0], at=row[1], author=row[2],
                           title=row[3], body=row[4], priority=row[5],
                           labels=tuple(json.loads(row[6])), column=row[7])
            for row in conn.execute(
                "SELECT revision, at, author, title, body, priority, labels,"
                " boardColumn FROM ticketRevisions WHERE ticketId = ?"
                " ORDER BY revision DESC", (ticket_id,))]


@dataclass(frozen=True)
class PendingNote:
    id: int
    ticketId: int
    issueId: str
    identifier: str
    at: int
    text: str


def pending_notes(conn, project_id):
    return [PendingNote(*row) for row in conn.execute(
        "SELECT n.id, n.ticketId, t.linearIssueId, t.linearIdentifier, n.at,"
        " n.text FROM ticketNotes n JOIN tickets t ON t.id = n.ticketId"
        " WHERE t.projectId = ? AND n.postedAt IS NULL"
        " AND t.goneSince IS NULL ORDER BY n.at, n.id", (project_id,))]


@dataclass(frozen=True)
class TicketNote:
    id: int
    at: int
    author: str
    kind: str
    text: str
    postedAt: int | None
    postError: str | None


def ticket_notes(conn, ticket_id):
    return [TicketNote(*row) for row in conn.execute(
        "SELECT id, at, author, kind, text, postedAt, postError"
        " FROM ticketNotes WHERE ticketId = ? ORDER BY at, id", (ticket_id,))]


@dataclass(frozen=True)
class ReviewRound:
    id: int
    linearIdentifier: str
    round: int
    verdict: str
    reviewerModel: str
    verificationResults: str
    findings: str
    startedAt: int
    endedAt: int | None


def review_rounds(conn):
    """In no particular order: the caller sorts and must not lean on SQLite's."""
    rows = conn.execute(
        "SELECT rr.id, t.linearIdentifier, rr.round, rr.verdict,"
        " rr.reviewerModel, rr.verificationResults, rr.findings,"
        " rr.startedAt, rr.endedAt"
        " FROM reviewRounds rr JOIN runs r ON r.id = rr.runId"
        " JOIN tickets t ON t.id = r.ticketId").fetchall()
    return [ReviewRound(id=row[0], linearIdentifier=row[1], round=row[2],
                        verdict=row[3], reviewerModel=row[4],
                        verificationResults=row[5], findings=row[6],
                        startedAt=row[7], endedAt=row[8])
            for row in rows]


@dataclass(frozen=True)
class EndedRound:
    round: int
    findings: str


def newest_ended_rounds(conn, run_id, count=2):
    rows = conn.execute(
        "SELECT round, findings FROM reviewRounds"
        " WHERE runId = ? AND endedAt IS NOT NULL"
        " ORDER BY round DESC LIMIT ?", (run_id, count)).fetchall()
    return [EndedRound(round=row[0], findings=row[1]) for row in rows]


@dataclass(frozen=True)
class RunDetail:
    id: int
    linearIdentifier: str
    title: str
    phase: str
    attempt: int
    startedAt: int
    endedAt: int | None
    lastHeartbeat: int
    outcome: str | None
    timeBoxMs: int | None
    branch: str | None
    host: str | None
    mergeSha: str | None
    reviewRoundCap: int | None
    prUrl: str | None = None
    workingMs: int | None = None
    workStartedAt: int | None = None
    verifyMs: int | None = None
    verifyStartedAt: int | None = None
    ticketUrl: str | None = None
    approvedAt: int | None = None
    approvedBy: str | None = None


def run_detail(conn, run_id):
    row = conn.execute(
        "SELECT r.id, t.linearIdentifier, t.title, r.phase, r.attempt,"
        " r.startedAt, r.endedAt, r.lastHeartbeat, r.outcome, r.timeBoxMs,"
        " r.branch, r.host, r.mergeSha, r.reviewRoundCap, r.prUrl,"
        " r.workingMs, r.workStartedAt, t.url, r.approvedAt, r.approvedBy,"
        " r.verifyMs, r.verifyStartedAt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return RunDetail(id=row[0], linearIdentifier=row[1], title=row[2],
                     phase=row[3], attempt=row[4], startedAt=row[5],
                     endedAt=row[6], lastHeartbeat=row[7], outcome=row[8],
                     timeBoxMs=row[9], branch=row[10], host=row[11],
                     mergeSha=row[12], reviewRoundCap=row[13],
                     prUrl=row[14], workingMs=row[15], workStartedAt=row[16],
                     ticketUrl=row[17], approvedAt=row[18], approvedBy=row[19],
                     verifyMs=row[20], verifyStartedAt=row[21])


@dataclass(frozen=True)
class RunRound:
    round: int
    startedAt: int
    endedAt: int | None
    verdict: str
    reviewerModel: str
    findings: str


def rounds_of(conn, run_id):
    rows = conn.execute(
        "SELECT round, startedAt, endedAt, verdict, reviewerModel, findings"
        " FROM reviewRounds WHERE runId = ? ORDER BY round", (run_id,)).fetchall()
    return [RunRound(round=row[0], startedAt=row[1], endedAt=row[2],
                     verdict=row[3], reviewerModel=row[4], findings=row[5])
            for row in rows]


@dataclass(frozen=True)
class NarrativeEvent:
    at: int
    kind: str
    summary: str


def narrative_events(conn, run_id, detail_kinds=()):
    marks = ", ".join("?" for _ in detail_kinds)
    rows = conn.execute(
        "SELECT at, kind, summary FROM runEvents"
        " WHERE runId = ? AND (level = 'narrative'"
        + (f" OR kind IN ({marks})" if detail_kinds else "")
        + ") ORDER BY seq",
        (run_id, *detail_kinds)).fetchall()
    return [NarrativeEvent(at=row[0], kind=row[1], summary=row[2])
            for row in rows]


@dataclass(frozen=True)
class RunEvent:
    id: int
    runId: int
    ticket: str | None
    at: int
    kind: str
    summary: str


def narrative_events_after(conn, after_id, since):
    rows = conn.execute(
        "SELECT runEvents.id, runEvents.runId, tickets.linearIdentifier,"
        " runEvents.at, runEvents.kind, runEvents.summary"
        " FROM runEvents JOIN runs ON runs.id = runEvents.runId"
        " LEFT JOIN tickets ON tickets.id = runs.ticketId"
        " WHERE runEvents.id > ? AND runEvents.at >= ?"
        " AND runEvents.level = 'narrative' ORDER BY runEvents.id",
        (after_id, since)).fetchall()
    return [RunEvent(*row) for row in rows]


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    runId: int
    ticketId: int
    at: int
    kind: str
    text: str
    source: str
    cleared: str | None = None
    waitedMs: int | None = None


_LEDGER_MARKS = (
    " (SELECT MAX(i.at) FROM interventions i"
    "  WHERE i.runId = ledger.runId AND i.\"action\" = 'redirect'"
    "  AND i.at < ledger.at),"
    " (SELECT r.endedAt FROM runs r"
    "  WHERE r.id = ledger.runId AND r.endedAt < ledger.at)")


def _cleared_by(kind, at, asked, ended):
    """The rule docs/reference/http.md states; the two change together."""
    if kind != "intervention":
        return None, None
    if asked is None and ended is None:
        return None, None
    # Timestamps alone: a tuple `max` would break a tie on the name.
    if ended is None or (asked is not None and asked > ended):
        return "question", at - asked
    return "failed", at - ended


def _ledger_entry(cls, row, **owner):
    cleared, waited = _cleared_by(row[4], row[3], row[7], row[8])
    return cls(id=row[0], runId=row[1], at=row[3], kind=row[4], text=row[5],
               source=row[6], cleared=cleared, waitedMs=waited, **owner)


def ledger(conn, run_id):
    rows = conn.execute(
        "SELECT ledger.id, ledger.runId, ledger.ticketId, ledger.at,"
        " ledger.kind, ledger.text, ledger.source," + _LEDGER_MARKS +
        " FROM ledger WHERE ledger.runId = ? ORDER BY ledger.at, ledger.id",
        (run_id,)).fetchall()
    return [_ledger_entry(LedgerEntry, row, ticketId=row[2]) for row in rows]


@dataclass(frozen=True)
class LedgerWindowEntry:
    id: int
    runId: int
    ticket: str
    at: int
    kind: str
    text: str
    source: str
    cleared: str | None = None
    waitedMs: int | None = None


_LAUNCH_BACKOFF_SHOWN = (
    "NOT (ledger.kind = 'intervention' AND"
    " (ledger.text LIKE 'supervisor launch_loop:%' OR"
    " ledger.text LIKE 'supervisor launch_backoff:%') AND EXISTS"
    " (SELECT 1 FROM projects p WHERE p.id = tickets.projectId"
    " AND p.launchBackoffReason IS NOT NULL"
    " AND ledger.at >= json_extract(p.launchBackoffReason, '$.since')))")


def ledger_since(conn, since, kind=None, ticket=None, limit=200,
                 hide_launch_backoff=False):
    where = ["ledger.at >= ?"]
    if hide_launch_backoff:
        where.append(_LAUNCH_BACKOFF_SHOWN)
    args = [since]
    if kind is not None:
        where.append("ledger.kind = ?")
        args.append(kind)
    if ticket is not None:
        where.append("tickets.linearIdentifier = ?")
        args.append(ticket)
    args.append(limit)
    rows = conn.execute(
        "SELECT ledger.id, ledger.runId, tickets.linearIdentifier, ledger.at,"
        " ledger.kind, ledger.text, ledger.source," + _LEDGER_MARKS +
        " FROM ledger JOIN tickets ON tickets.id = ledger.ticketId"
        f" WHERE {' AND '.join(where)} ORDER BY ledger.at DESC, ledger.id DESC"
        " LIMIT ?", args).fetchall()
    return [_ledger_entry(LedgerWindowEntry, row, ticket=row[2])
            for row in rows]


def ledger_after(conn, after_id, since, limit=200):
    rows = conn.execute(
        "SELECT ledger.id, ledger.runId, tickets.linearIdentifier, ledger.at,"
        " ledger.kind, ledger.text, ledger.source," + _LEDGER_MARKS +
        " FROM ledger JOIN tickets ON tickets.id = ledger.ticketId"
        " WHERE ledger.id > ? AND ledger.at >= ? AND " + _LAUNCH_BACKOFF_SHOWN +
        " ORDER BY ledger.id LIMIT ?", (after_id, since, limit)).fetchall()
    return [_ledger_entry(LedgerWindowEntry, row, ticket=row[2])
            for row in rows]


@dataclass(frozen=True)
class Strike:
    runId: int
    strikes: int
    lastSeen: int


def strike(conn, run_id):
    row = conn.execute(
        "SELECT runId, strikes, lastSeen FROM sweepStrikes WHERE runId = ?",
        (run_id,)).fetchone()
    if row is None:
        return None
    return Strike(runId=row[0], strikes=row[1], lastSeen=row[2])


@dataclass(frozen=True)
class SupervisorBeat:
    pid: int
    startedAt: int
    lastBeat: int
    passes: int
    host: str | None


def supervisor_beat(conn):
    row = conn.execute(
        "SELECT pid, startedAt, lastBeat, passes, host"
        " FROM supervisorHeartbeats"
        " ORDER BY lastBeat DESC, startedAt DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return SupervisorBeat(pid=row[0], startedAt=row[1], lastBeat=row[2],
                          passes=row[3], host=row[4])


def ready_tickets(conn, project_id=None):
    """History subtracts nothing: liveness, not a record, stops a second start."""
    where = "t.status = 'ready' AND t.activeRunId IS NULL"
    params = ()
    if project_id is not None:
        where += " AND t.projectId = ?"
        params = (project_id,)
    return conn.execute(
        f"SELECT t.id, t.lastRunId FROM tickets t WHERE {where}"
        " ORDER BY t.id", params).fetchall()


@dataclass(frozen=True)
class ClaimableTicket:
    id: int
    linearIssueId: str
    linearIdentifier: str
    revision: int
    title: str
    body: str
    timeBoxMs: int | None
    priority: int | None
    labels: tuple[str, ...]
    url: str | None
    boardState: str | None
    filedAt: int | None
    lastRunId: int | None
    acceptanceCriteria: tuple[str, ...] = ()
    verificationCommands: tuple[str, ...] = ()


_CLAIM_ORDER = {
    "identifier": "linearIdentifier",
    "priority": "CASE WHEN priority BETWEEN 1 AND 4 THEN priority ELSE 5 END,"
                " linearIdentifier",
}


def claimable(conn, project_id, order="identifier"):
    import store.tickets
    rows = conn.execute(
        "SELECT id, linearIssueId, linearIdentifier, revision, title, body,"
        " timeBoxMs, priority, labels, url, boardState, filedAt, lastRunId,"
        " acceptanceCriteria, verificationCommands FROM tickets"
        " WHERE projectId = ? AND status = 'ready' AND activeRunId IS NULL"
        " AND boardColumn = 'ready' AND goneSince IS NULL"
        f" ORDER BY {_CLAIM_ORDER[order]}", (project_id,)).fetchall()
    verdicts = store.tickets.pickable_tickets(conn, project_id)
    return [ClaimableTicket(*row[:8], tuple(json.loads(row[8])), *row[9:13],
                            tuple(json.loads(row[13])),
                            tuple(json.loads(row[14])))
            for row in rows if verdicts.get(row[2])]
