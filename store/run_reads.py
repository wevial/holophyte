from __future__ import annotations

import json
from dataclasses import dataclass, fields
from time import time

from .enums import FailureKind, InterventionActor


@dataclass(frozen=True)
class RunSnapshot:
    id: int
    ticketId: int
    phase: str
    lastHeartbeat: int
    endedAt: int | None
    startedAt: int
    timeBoxMs: int | None
    reviewRoundCount: int = 0
    reviewRoundCap: int | None = None
    workingMs: int | None = None
    workStartedAt: int | None = None
    verifyMs: int | None = None
    verifyStartedAt: int | None = None


def run_snapshot(conn, run_id):
    # Rounds are counted off their rows: `runs.reviewRoundCount` is only
    # stamped at close-out, so it reads 0 on a live run.
    row = conn.execute(
        "SELECT id, ticketId, phase, lastHeartbeat, endedAt, startedAt,"
        " timeBoxMs, workingMs, workStartedAt, (SELECT COUNT(*) FROM reviewRounds"
        " WHERE runId = runs.id AND verdict != 'error'),"
        " reviewRoundCap, verifyMs, verifyStartedAt FROM runs WHERE id = ?",
        (run_id,)).fetchone()
    if row is None:
        return None
    return RunSnapshot(id=row[0], ticketId=row[1], phase=row[2],
                       lastHeartbeat=row[3], endedAt=row[4], startedAt=row[5],
                       timeBoxMs=row[6], workingMs=row[7], workStartedAt=row[8],
                       reviewRoundCount=row[9], reviewRoundCap=row[10],
                       verifyMs=row[11], verifyStartedAt=row[12])


@dataclass(frozen=True)
class LiveRun:
    id: int
    linearIdentifier: str
    title: str
    phase: str
    lastHeartbeat: int
    startedAt: int
    timeBoxMs: int | None
    host: str | None
    reviewRoundCount: int
    reviewRoundCap: int | None
    prUrl: str | None = None
    workingMs: int | None = None
    workStartedAt: int | None = None
    verifyMs: int | None = None
    verifyStartedAt: int | None = None
    ticketUrl: str | None = None
    boardState: str | None = None
    prSeenChecks: str | None = None
    prSeenReview: str | None = None
    prSeenThreads: int | None = None
    prSeenOpenThreads: int | None = None
    prSeenOpenThreadsFloor: int | None = None
    prSeenTitle: str | None = None
    prSeenState: str | None = None
    prSeenAdditions: int | None = None
    prSeenDeletions: int | None = None
    prSeenChangedFiles: int | None = None
    prSeenPreviewUrl: str | None = None
    prSeenScreenshots: str | None = None
    prSeenVideos: str | None = None
    wokeAt: int | None = None


@dataclass(frozen=True)
class ApprovedCandidate:
    run_id: int
    sha: str | None
    pr_url: str | None = None
    # False for a `--babysit` release: look again rather than merge.
    approved: bool = True
    # What the last independent judgement covered; `sha` may be past it.
    approved_sha: str | None = None
    paused: bool = False


def approved_candidate(conn, ticket_id, run_id):
    row = conn.execute(
        "SELECT id, resumePhase, candidateSha, prUrl, approvedSha, approvedAt, outcome,"
        " EXISTS (SELECT 1 FROM runEvents e WHERE e.runId = runs.id"
        " AND e.kind = 'approval_carried')"
        " FROM runs"
        " WHERE ticketId = ? AND id <> ?"
        " ORDER BY attempt DESC LIMIT 1", (ticket_id, run_id)).fetchone()
    if row is None or row[1] not in ("merge_gate", "merging"):
        return None
    return ApprovedCandidate(run_id=row[0], sha=row[2], pr_url=row[3],
                             approved=row[5] is not None or bool(row[7]),
                             approved_sha=row[4], paused=row[6] == "paused")


@dataclass(frozen=True)
class ParkFacts:
    run_id: int
    ticket_id: int
    identifier: str
    ticket_status: str
    active_run_id: int | None
    newest: bool
    phase: str
    branch: str | None
    pr_url: str | None
    candidate_sha: str | None
    approved_sha: str | None


def park_facts(conn, run_id):
    row = conn.execute(
        "SELECT r.id, r.ticketId, t.linearIdentifier, t.status, t.activeRunId,"
        " t.lastRunId = r.id AND NOT EXISTS (SELECT 1 FROM runs o"
        " WHERE o.ticketId = r.ticketId AND o.id > r.id),"
        " r.phase, r.branch, r.prUrl, r.candidateSha, r.approvedSha"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId WHERE r.id = ?",
        (run_id,)).fetchone()
    if row is None:
        return None
    return ParkFacts(*row[:5], bool(row[5]), *row[6:])


def last_independent_verdict(conn, ticket_id):
    """Rounds carry no sha: `approvedSha` is the only record of what was reviewed."""
    return conn.execute(
        "SELECT rr.verdict, r.approvedSha"
        " FROM reviewRounds rr JOIN runs r ON r.id = rr.runId"
        " WHERE r.ticketId = ? AND rr.reviewerModel NOT GLOB 'github:*'"
        " ORDER BY r.attempt DESC, rr.round DESC LIMIT 1",
        (ticket_id,)).fetchone()


def babysit_note(conn, run_id):
    if conn.execute("SELECT 1 FROM runEvents WHERE runId = ? AND kind IN"
                    " ('ci_wake', 'conflict_wake') LIMIT 1",
                    (run_id,)).fetchone():
        return ""
    row = conn.execute(
        "SELECT e.summary FROM interventions i JOIN runEvents e"
        " ON e.runId = i.runId AND e.at = i.at AND e.kind = 'intervention'"
        " AND e.summary LIKE i.source || ' babysit: %'"
        " WHERE i.runId = ? AND i.action = 'babysit'"
        " ORDER BY i.id DESC, e.seq DESC LIMIT 1", (run_id,)).fetchone()
    return row[0].partition(" babysit: ")[2] if row else ""


PR_SEEN_MEDIA = ("prSeenAdditions", "prSeenDeletions", "prSeenChangedFiles",
                 "prSeenPreviewUrl", "prSeenScreenshots", "prSeenVideos")


def _pr_seen_media_sql(columns, names=PR_SEEN_MEDIA):
    return ", ".join(f"r.{name}" if name in columns else "NULL"
                     for name in names)


def _pr_seen_media(row):
    return dict(zip(PR_SEEN_MEDIA, row, strict=True))


def live_runs(conn, phases):
    phases = tuple(phases)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    pr_state = "r.prSeenState" if "prSeenState" in columns else "NULL"
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, t.title, r.phase, r.lastHeartbeat,"
        " r.startedAt, r.timeBoxMs, r.host,"
        " (SELECT COUNT(*) FROM reviewRounds rr WHERE rr.runId = r.id"
        " AND rr.verdict != 'error'),"
        " r.reviewRoundCap, r.prUrl, r.workingMs, r.workStartedAt, t.url, t.boardState,"
        " r.verifyMs, r.verifyStartedAt, r.prSeenChecks, r.prSeenReview,"
        " r.prSeenThreads, r.prSeenOpenThreads, r.prSeenOpenThreadsFloor,"
        f" r.prSeenTitle, {pr_state}, {_pr_seen_media_sql(columns)},"
        " CASE WHEN EXISTS (SELECT 1 FROM runs earlier"
        " WHERE earlier.ticketId = r.ticketId AND earlier.id < r.id"
        " AND earlier.prUrl = r.prUrl AND EXISTS (SELECT 1 FROM runEvents e"
        " WHERE e.runId = earlier.id AND e.kind = 'phase_change'"
        " AND (e.summary GLOB '* -> awaiting_merge_approval'"
        " OR e.summary GLOB '* -> awaiting_merge_approval:*')))"
        " THEN r.startedAt END"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NULL"
        f"   AND r.phase IN ({', '.join('?' * len(phases))})"
        " ORDER BY r.id", phases).fetchall()
    return [LiveRun(id=row[0], linearIdentifier=row[1], title=row[2],
                    phase=row[3], lastHeartbeat=row[4], startedAt=row[5],
                    timeBoxMs=row[6], host=row[7], reviewRoundCount=row[8],
                    reviewRoundCap=row[9], prUrl=row[10],
                    workingMs=row[11], workStartedAt=row[12], ticketUrl=row[13],
                    boardState=row[14], verifyMs=row[15],
                    verifyStartedAt=row[16], prSeenChecks=row[17],
                    prSeenReview=row[18], prSeenThreads=row[19],
                    prSeenOpenThreads=row[20], prSeenOpenThreadsFloor=row[21],
                    prSeenTitle=row[22], prSeenState=row[23],
                    **_pr_seen_media(row[24:30]), wokeAt=row[30])
            for row in rows]


@dataclass(frozen=True)
class EndedRun:
    id: int
    linearIdentifier: str
    startedAt: int
    endedAt: int
    timeBoxMs: int | None
    reviewRoundCount: int
    outcome: str | None
    outcomeReason: str | None
    branch: str | None
    host: str | None
    mergeSha: str | None
    workingMs: int | None = None
    workStartedAt: int | None = None
    verifyMs: int | None = None
    verifyStartedAt: int | None = None


def newest_run_id(conn):
    row = conn.execute("SELECT MAX(id) FROM runs").fetchone()
    return row[0] if row is not None else None


def ended_runs(conn):
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, r.startedAt, r.endedAt, r.timeBoxMs,"
        " r.reviewRoundCount, r.outcome, r.outcomeReason, r.branch, r.host,"
        " r.mergeSha, r.workingMs, r.workStartedAt, r.verifyMs, r.verifyStartedAt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.endedAt IS NOT NULL"
        " ORDER BY r.endedAt, r.id").fetchall()
    return [EndedRun(id=row[0], linearIdentifier=row[1], startedAt=row[2],
                     endedAt=row[3], timeBoxMs=row[4], reviewRoundCount=row[5],
                     outcome=row[6], outcomeReason=row[7], branch=row[8],
                     host=row[9], mergeSha=row[10],
                     workingMs=row[11], workStartedAt=row[12],
                     verifyMs=row[13], verifyStartedAt=row[14])
            for row in rows]


@dataclass(frozen=True)
class Toil:
    by_action: dict
    merged: int
    by_actor: dict


ACTORS = tuple(actor.value for actor in InterventionActor)


def toil_since(conn, since_ms):
    """A read-only daemon may open an unmigrated store with no `interventions`."""
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(interventions)")}
    rows = conn.execute(
        'SELECT "action", COUNT(*) FROM interventions'
        " WHERE source = 'human' AND at >= ?"
        ' GROUP BY "action" ORDER BY COUNT(*) DESC, "action"',
        (since_ms,)).fetchall() if columns else []
    by_actor = _actor_counts(conn, columns, since_ms)
    merged = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE outcome = 'merged' AND endedAt >= ?",
        (since_ms,)).fetchone()[0]
    return Toil(by_action=dict(rows), merged=merged, by_actor=by_actor)


def _actor_counts(conn, columns, since_ms):
    actor = "actor" if "actor" in columns else "NULL"
    rows = conn.execute(
        f"SELECT {actor}, COUNT(*) FROM interventions"
        " WHERE action != 'migrate' AND at >= ?"
        f" GROUP BY {actor}", (since_ms,)).fetchall() if columns else []
    counted = dict(rows)
    return {**{name: counted.get(name, 0) for name in ACTORS},
            "unknown": counted.get(None, 0)}


@dataclass(frozen=True)
class MergedRun:
    id: int
    linearIdentifier: str
    title: str
    startedAt: int
    endedAt: int
    timeBoxMs: int | None
    reviewRoundCount: int
    findingCount: int
    host: str | None
    mergeSha: str | None
    prUrl: str | None = None
    outcome: str | None = None
    outcomeReason: str | None = None
    workingMs: int | None = None
    workStartedAt: int | None = None
    verifyMs: int | None = None
    verifyStartedAt: int | None = None
    ticketUrl: str | None = None


SQLITE_INT64_MIN = -(2 ** 63)
SQLITE_INT64_MAX = 2 ** 63 - 1


def merged_runs(conn, limit, before=None):
    return finished_runs(conn, limit, before, outcomes=("merged",))


def finished_runs(conn, limit, before=None, outcomes=None):
    if (before is not None
            and not SQLITE_INT64_MIN <= before <= SQLITE_INT64_MAX):
        # No run has such an id, and binding it would raise OverflowError.
        return []
    where = ("r.endedAt IS NOT NULL AND (r.outcome = 'merged' OR r.id ="
             " (SELECT MAX(x.id) FROM runs x WHERE x.ticketId = r.ticketId))")
    params = []
    if outcomes is not None:
        where += f" AND r.outcome IN ({', '.join('?' for _ in outcomes)})"
        params.extend(outcomes)
    if before is not None:
        where += (" AND (r.endedAt, r.id) < (SELECT endedAt, id FROM runs"
                  " WHERE id = ? AND endedAt IS NOT NULL)")
        params.append(before)
    rows = conn.execute(
        "SELECT r.id, t.linearIdentifier, t.title, r.startedAt, r.endedAt,"
        " r.timeBoxMs, r.reviewRoundCount,"
        " (SELECT COALESCE(SUM(json_array_length(rr.findings)), 0)"
        "    FROM reviewRounds rr WHERE rr.runId = r.id),"
        " r.host, r.mergeSha, r.prUrl, r.outcome, r.outcomeReason,"
        " r.workingMs, r.workStartedAt, t.url, r.verifyMs, r.verifyStartedAt"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        f" WHERE {where}"
        " ORDER BY r.endedAt DESC, r.id DESC LIMIT ?",
        (*params, limit)).fetchall()
    return [MergedRun(id=row[0], linearIdentifier=row[1], title=row[2],
                      startedAt=row[3], endedAt=row[4], timeBoxMs=row[5],
                      reviewRoundCount=row[6], findingCount=row[7],
                      host=row[8], mergeSha=row[9], prUrl=row[10],
                      outcome=row[11], outcomeReason=row[12],
                      workingMs=row[13], workStartedAt=row[14], ticketUrl=row[15],
                      verifyMs=row[16], verifyStartedAt=row[17])
            for row in rows]


@dataclass(frozen=True)
class RecentRun:
    id: int
    linearIdentifier: str
    ticketUrl: str | None
    title: str
    attempt: int
    phase: str
    outcome: str | None
    outcomeReason: str | None
    failureKind: str | None
    parkKind: str | None
    startedAt: int
    endedAt: int | None
    lastHeartbeat: int
    reviewRoundCount: int
    reviewRoundCap: int | None
    prUrl: str | None
    host: str | None
    timeBoxMs: int | None
    workingMs: int | None
    workStartedAt: int | None
    findingCount: int
    mergeSha: str | None
    prSeenAdditions: int | None
    prSeenDeletions: int | None
    resumedAs: int | None
    implementer: str | None = None


HISTORY_SORTS = ("started", "ended", "duration", "rounds", "state")
HISTORY_ORDERS = ("asc", "desc")
STATE_GROUPS = ("active", "merged", "failed", "abandoned")
FAILURE_KINDS = tuple(kind.value for kind in FailureKind)


@dataclass(frozen=True)
class RunQuery:
    offset: int = 0
    sort: str = "started"
    order: str = "desc"
    q: str | None = None
    states: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    implementers: tuple[str, ...] = ()
    now: int | None = None


DEFAULT_QUERY = RunQuery()
STATE_GROUP = (
    "CASE WHEN endedAt IS NULL OR (outcome = 'paused' AND resumedAs IS NULL)"
    " THEN 'active' WHEN outcome = 'paused' THEN NULL"
    " WHEN outcome IN ('merged', 'abandoned') THEN outcome ELSE 'failed' END")
SORT_KEYS = {
    "started": "startedAt",
    "ended": f"COALESCE(endedAt, {SQLITE_INT64_MAX})",
    "duration": "COALESCE(endedAt, ?) - startedAt",
    "rounds": "reviewRoundCount",
    "state": f"CASE {STATE_GROUP} WHEN 'active' THEN 0 WHEN 'merged' THEN 1"
             " WHEN 'failed' THEN 2 WHEN 'abandoned' THEN 3 ELSE 4 END",
}
IMPLEMENTER_LABEL = (
    "CASE WHEN json_valid({0}payload) AND json_type({0}payload, '$.label')"
    " = 'text' THEN json_extract({0}payload, '$.label') END")


def _history_base(conn, since, until):
    since = max(SQLITE_INT64_MIN, min(since, SQLITE_INT64_MAX))
    where = "r.endedAt IS NULL OR r.startedAt >= ?"
    params = [since]
    if until is not None:
        until = max(SQLITE_INT64_MIN, min(until, SQLITE_INT64_MAX))
        where = "r.endedAt IS NOT NULL AND r.startedAt >= ? AND r.startedAt < ?"
        params.append(until)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    additions, deletions = (
        _pr_seen_media_sql(columns, (name,)) + f" AS {name}"
        for name in PR_SEEN_MEDIA[:2])
    return (
        "SELECT r.id AS id, t.linearIdentifier AS linearIdentifier,"
        " t.url AS ticketUrl, t.title AS title, r.attempt AS attempt,"
        " r.phase AS phase, r.outcome AS outcome,"
        " r.outcomeReason AS outcomeReason, r.failureKind AS failureKind,"
        " r.parkKind AS parkKind, r.startedAt AS startedAt,"
        " r.endedAt AS endedAt, r.lastHeartbeat AS lastHeartbeat,"
        " CASE WHEN r.endedAt IS NULL THEN (SELECT COUNT(*) FROM reviewRounds rr"
        "   WHERE rr.runId = r.id AND rr.verdict != 'error')"
        " ELSE r.reviewRoundCount END AS reviewRoundCount,"
        " r.reviewRoundCap AS reviewRoundCap, r.prUrl AS prUrl, r.host AS host,"
        " r.timeBoxMs AS timeBoxMs, r.workingMs AS workingMs,"
        " r.workStartedAt AS workStartedAt,"
        " (SELECT COALESCE(SUM(json_array_length(rr.findings)), 0)"
        "    FROM reviewRounds rr WHERE rr.runId = r.id) AS findingCount,"
        f" r.mergeSha AS mergeSha, {additions}, {deletions},"
        " CASE WHEN r.outcome = 'paused' THEN (SELECT MIN(n.id) FROM runs n"
        "   WHERE n.ticketId = r.ticketId AND n.id > r.id) END AS resumedAs,"
        f" (SELECT {IMPLEMENTER_LABEL.format('e.')} FROM runEvents e"
        "   WHERE e.runId = r.id AND e.kind = 'implementer_profile'"
        "   ORDER BY e.id DESC LIMIT 1) AS implementer"
        " FROM runs r JOIN tickets t ON t.id = r.ticketId"
        f" WHERE {where}"), params


def _history_filters(query, skip=None):
    clauses, params = [], []
    if query.q:
        clauses.append("(instr(lower(linearIdentifier), lower(?)) > 0"
                       " OR instr(lower(title), lower(?)) > 0)")
        params += [query.q, query.q]
    for name, column, values in (("state", STATE_GROUP, query.states),
                                 ("failure", "failureKind", query.failures),
                                 ("implementer", "implementer",
                                  query.implementers)):
        if values and name != skip:
            clauses.append(f"({column}) IN ({', '.join('?' * len(values))})")
            params.extend(values)
    return " AND ".join(clauses) or "1", params


def _query_now(query):
    return int(time() * 1000) if query.now is None else query.now


def recent_runs(conn, since, limit, until=None, *, query=DEFAULT_QUERY):
    base, params = _history_base(conn, since, until)
    where, filters = _history_filters(query)
    key = SORT_KEYS[query.sort]
    ordering = [_query_now(query)] if "?" in key else []
    names = ", ".join(field.name for field in fields(RecentRun))
    rows = conn.execute(
        f"SELECT {names}, COUNT(*) OVER () FROM ({base}) WHERE {where}"
        f" ORDER BY {key} {query.order.upper()}, startedAt DESC, id DESC"
        " LIMIT ? OFFSET ?",
        (*params, *filters, *ordering, limit, query.offset)).fetchall()
    if rows:
        return [RecentRun(*row[:-1]) for row in rows], rows[0][-1]
    if not query.offset:
        return [], 0
    (total,) = conn.execute(f"SELECT COUNT(*) FROM ({base}) WHERE {where}",
                            (*params, *filters)).fetchone()
    return [], total


def run_counts(conn, since, until=None, query=DEFAULT_QUERY):
    base, params = _history_base(conn, since, until)
    keys = {"state": STATE_GROUPS, "failure": FAILURE_KINDS,
            "implementer": implementer_labels(conn)}
    columns = {"state": STATE_GROUP, "failure": "failureKind",
               "implementer": "implementer"}
    counts = {}
    for name, column in columns.items():
        where, filters = _history_filters(query, skip=name)
        counted = dict(conn.execute(
            f"SELECT {column}, COUNT(*) FROM ({base}) WHERE {where}"
            " GROUP BY 1", (*params, *filters)).fetchall())
        counts[name] = {key: counted.get(key, 0) for key in keys[name]}
    return counts


def implementer_labels(conn):
    rows = conn.execute(
        f"SELECT DISTINCT {IMPLEMENTER_LABEL.format('')} FROM runEvents"
        " WHERE kind = 'implementer_profile'").fetchall()
    return sorted(row[0] for row in rows if row[0] is not None)


@dataclass(frozen=True)
class ChainRun:
    id: int
    startedAt: int
    endedAt: int | None
    reviewRoundCount: int
    findingCount: int
    workingMs: int | None = None
    workStartedAt: int | None = None
    verifyMs: int | None = None
    verifyStartedAt: int | None = None


def run_chains(conn, run_ids):
    run_ids = tuple(run_ids)
    if not run_ids:
        return {}
    rows = conn.execute(
        "SELECT c.id, r.id, r.startedAt, r.endedAt,"
        " CASE WHEN r.endedAt IS NULL THEN (SELECT COUNT(*) FROM reviewRounds rr"
        "   WHERE rr.runId = r.id AND rr.verdict != 'error')"
        " ELSE r.reviewRoundCount END,"
        " (SELECT COALESCE(SUM(json_array_length(rr.findings)), 0)"
        "    FROM reviewRounds rr WHERE rr.runId = r.id),"
        " r.workingMs, r.workStartedAt, r.verifyMs, r.verifyStartedAt"
        " FROM runs c JOIN runs r ON r.ticketId = c.ticketId AND r.id <= c.id"
        " AND NOT EXISTS (SELECT 1 FROM runs m WHERE m.ticketId = c.ticketId"
        "   AND m.outcome = 'merged' AND m.id >= r.id AND m.id < c.id)"
        f" WHERE c.id IN ({', '.join('?' * len(run_ids))})"
        " ORDER BY c.id, r.id", run_ids).fetchall()
    chains = {}
    for row in rows:
        chains.setdefault(row[0], []).append(ChainRun(*row[1:]))
    return chains


def round_caps(conn, run_ids):
    run_ids = tuple(run_ids)
    if not run_ids:
        return {}
    rows = conn.execute(
        "SELECT r.id, COALESCE(r.reviewRoundCap, (SELECT e.reviewRoundCap"
        " FROM runs e WHERE e.ticketId = r.ticketId AND e.id < r.id"
        " AND e.reviewRoundCap IS NOT NULL AND NOT EXISTS (SELECT 1 FROM runs m"
        "   WHERE m.ticketId = r.ticketId AND m.outcome = 'merged'"
        "   AND m.id >= e.id AND m.id < r.id)"
        " ORDER BY e.id DESC LIMIT 1))"
        f" FROM runs r WHERE r.id IN ({', '.join('?' * len(run_ids))})",
        run_ids).fetchall()
    return {**dict.fromkeys(run_ids), **dict(rows)}


@dataclass(frozen=True)
class CriteriaEvent:
    runId: int
    payload: dict


def criteria_events(conn, run_ids):
    rows = conn.execute(
        "SELECT runId, payload FROM runEvents"
        f" WHERE kind = 'criteria' AND runId IN ({', '.join('?' * len(run_ids))})"
        " ORDER BY runId DESC, seq DESC", run_ids).fetchall()
    return [CriteriaEvent(runId=row[0], payload=json.loads(row[1]))
            for row in rows]


def witness_prechecks(conn, run_id):
    rows = conn.execute(
        "SELECT payload FROM runEvents"
        " WHERE kind = 'witness_precheck_results' AND runId = ?"
        " ORDER BY seq", (run_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


@dataclass(frozen=True)
class FailedAttempt:
    attempt: int
    outcomeReason: str | None


def admission_changes(conn, project_id, limit):
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(interventions)")}
    if "projectId" not in columns:
        return []
    actor = "actor" if "actor" in columns else "NULL"
    rows = conn.execute(
        f"SELECT id, at, action, note, {actor} FROM interventions"
        " WHERE projectId = ? AND action IN ('hold', 'release_hold', 'disable')"
        " ORDER BY at DESC, id DESC LIMIT ?",
        (project_id, limit + 1)).fetchall()
    return [dict(zip(("id", "at", "action", "note", "actor"), row))
            for row in rows]


def latest_intervention(conn, action, project_id):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interventions)")}
    if not columns:
        return None
    actor = "i.actor" if "actor" in columns else "NULL"
    return conn.execute(
        f"SELECT i.at, {actor} FROM interventions i"
        " JOIN runs r ON r.id = i.runId"
        " WHERE i.action = ? AND r.projectId = ?"
        " ORDER BY i.at DESC, i.id DESC LIMIT 1",
        (action, project_id)).fetchone()


def latest_human_intervention_at(conn, ticket_id):
    (at,) = conn.execute(
        "SELECT COALESCE(MAX(i.at), 0) FROM interventions i"
        " JOIN runs r ON r.id = i.runId"
        " WHERE r.ticketId = ? AND i.source = 'human'",
        (ticket_id,)).fetchone()
    return at


def failed_attempts_since(conn, ticket_id, since):
    rows = conn.execute(
        "SELECT attempt, outcomeReason FROM runs r"
        " WHERE ticketId = ? AND outcome = 'failed' AND endedAt > ?"
        " AND outcomeClass = 'work'"
        " AND NOT EXISTS (SELECT 1 FROM interventions i"
        "                 WHERE i.runId = r.id AND i.source = 'human'"
        "                 AND i.\"action\" = 'close_out')"
        " ORDER BY attempt", (ticket_id, since)).fetchall()
    return [FailedAttempt(attempt=row[0], outcomeReason=row[1]) for row in rows]


@dataclass(frozen=True)
class RecentFailedRun:
    id: int
    linearIdentifier: str
    outcomeReason: str | None
    endedAt: int
    ticketStatus: str
    lastRunId: int | None
    activeRunId: int | None
    attempt: int = 0
    prUrl: str | None = None
    ticketUrl: str | None = None
    boardState: str | None = None
    triage: dict | None = None
    title: str | None = None


FAILED_RUN_COLUMNS = (
    "SELECT r.id, t.linearIdentifier, r.outcomeReason, r.endedAt,"
    " t.status, r.attempt, r.prUrl, t.lastRunId, t.activeRunId, t.url, t.boardState,"
    " t.title, (SELECT e.payload FROM runEvents e WHERE e.runId = r.id"
    "  AND e.kind = 'failure_triage' ORDER BY e.seq DESC LIMIT 1)"
    " FROM runs r JOIN tickets t ON t.id = r.ticketId")


def _failed_run(row):
    return RecentFailedRun(id=row[0], linearIdentifier=row[1],
                           outcomeReason=row[2], endedAt=row[3],
                           ticketStatus=row[4], attempt=row[5],
                           prUrl=row[6], lastRunId=row[7], activeRunId=row[8],
                           ticketUrl=row[9], boardState=row[10],
                           title=row[11],
                           triage=json.loads(row[12]) if row[12] else None)


def recent_failed_runs(conn, since_ms):
    rows = conn.execute(
        FAILED_RUN_COLUMNS + " WHERE r.outcome = 'failed' AND r.endedAt > ?"
        " ORDER BY r.endedAt, r.id", (since_ms,)).fetchall()
    return [_failed_run(row) for row in rows]


def stranded_runs(conn):
    """No window: a failed run's ticket waits in flight for a human however old."""
    rows = conn.execute(
        FAILED_RUN_COLUMNS + " WHERE t.status = 'in_flight' AND t.activeRunId IS NULL"
        " AND r.id = t.lastRunId AND r.outcome = 'failed'"
        " ORDER BY r.endedAt, r.id").fetchall()
    return [_failed_run(row) for row in rows]


@dataclass(frozen=True)
class StrandedTicket:
    linearIdentifier: str
    runId: int | None
    outcome: str | None
    outcomeReason: str | None
    endedAt: int | None


def stranded_tickets(conn):
    rows = conn.execute(
        "SELECT t.linearIdentifier, r.id, r.outcome, r.outcomeReason, r.endedAt"
        " FROM tickets t LEFT JOIN runs r ON r.id ="
        " (SELECT MAX(n.id) FROM runs n WHERE n.ticketId = t.id)"
        " WHERE t.status = 'in_flight' AND (r.id IS NULL OR r.endedAt IS NOT NULL)"
        " ORDER BY t.id").fetchall()
    return [StrandedTicket(*row) for row in rows]


def last_event_at(conn, run_ids):
    run_ids = tuple(run_ids)
    if not run_ids:
        return {}
    rows = conn.execute(
        "SELECT runId, MAX(at) FROM runEvents"
        f" WHERE runId IN ({', '.join('?' * len(run_ids))}) GROUP BY runId",
        run_ids).fetchall()
    return {**dict.fromkeys(run_ids), **dict(rows)}


@dataclass(frozen=True)
class EndedAttempt:
    id: int
    outcome: str | None
    failureKind: str | None
    outcomeReason: str | None


def last_ended_runs(conn, count):
    rows = conn.execute(
        "SELECT t.linearIdentifier, r.id, r.outcome, r.failureKind,"
        " r.outcomeReason FROM (SELECT id, ticketId, outcome, failureKind,"
        " outcomeReason, ROW_NUMBER() OVER (PARTITION BY ticketId"
        " ORDER BY id DESC) AS newest FROM runs WHERE endedAt IS NOT NULL) r"
        " JOIN tickets t ON t.id = r.ticketId"
        " WHERE r.newest <= ?"
        " AND t.status IN ('ready', 'in_flight', 'blocked_on_operator')"
        " ORDER BY t.id, r.id DESC", (count,)).fetchall()
    runs = {}
    for row in rows:
        runs.setdefault(row[0], []).append(EndedAttempt(*row[1:]))
    return runs
