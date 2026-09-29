"""store: the durable state store, one WAL-mode SQLite file."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import time

import ticket_template as _ticket_template
from holophyte.redact import redact_document as _redact_document
from holophyte.redact import redact_values as _redact_values

from . import enums as _enums
from .schema import (  # noqa: F401
    SCHEMA_VERSION,
    SchemaNewer,
    SchemaOlder,
    _transaction,
    init,
    open,
    transaction,
)
from .writes import (  # noqa: F401
    clear_merge_sha,
    clear_push,
    record_push,
    set_board_state,
    set_gone_since,
    set_outcome_reason,
    set_pull_request,
    set_question,
    stamp_board_ask,
)


class ClaimConflict(Exception):
    pass


class RevisionMoved(Exception):
    def __init__(self, identifier, expected, current):
        super().__init__(f"ticket {identifier} moved from revision {expected}"
                         f" to {current} since it was admitted")
        self.expected, self.current = expected, current


# The estimate is left out: `runs.timeBoxMs` already snapshots it.
CONTRACT_FIELDS = ("title", "acceptanceCriteria", "verificationCommands",
                   "evidenceStates")


def contract_snapshot(title, acceptance_criteria, verification_commands,
                      evidence_states=()):
    return json.dumps(
        {
            "title": title,
            "acceptanceCriteria": list(acceptance_criteria),
            "verificationCommands": list(verification_commands),
            "evidenceStates": list(evidence_states),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def run_contract(conn, run_id):
    row = conn.execute(
        "SELECT ticketSnapshot FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return row[0]


def contract_drift(before, after):
    if before is None or after is None:
        return ()
    was, is_now = json.loads(before), json.loads(after)
    was.setdefault("evidenceStates", [])
    is_now.setdefault("evidenceStates", [])
    return tuple(f for f in CONTRACT_FIELDS if was.get(f) != is_now.get(f))


def claim(conn, project_id, ticket_id, now=None, expected_revision=None):
    if now is None:
        now = int(time.time() * 1000)
    # IMMEDIATE takes the write lock before the lease is read, so concurrent
    # claimers serialize and the loser reads the lease held.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("SELECT admission FROM projects WHERE id = ?",
                        (project_id,)).fetchone() in (("held",), ("disabled",)):
            conn.commit()
            return None
        held = conn.execute(
            "SELECT activeRunId, linearIdentifier FROM tickets"
            " WHERE id = ? AND activeRunId IS NOT NULL",
            (ticket_id,),
        ).fetchone()
        if held is not None:
            raise ClaimConflict(
                f"ticket {held[1]}: lease already held by run {held[0]}"
            )
        if expected_revision is not None:
            _assert_admitted(conn, ticket_id, expected_revision)
        (prior,) = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE ticketId = ?", (ticket_id,)
        ).fetchone()
        ticket = conn.execute(
            "SELECT timeBoxMs, title, acceptanceCriteria, verificationCommands,"
            " body, revision FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        estimate = ticket[0] if ticket else None
        # Revision 0 is a row no revision was recorded for.
        revision = ticket[5] or None if ticket else None
        snapshot = None if ticket is None else contract_snapshot(
            ticket[1], json.loads(ticket[2]), json.loads(ticket[3]),
            _ticket_template.parse(ticket[4] or "").evidence_states)
        run_id = conn.execute(
            "INSERT INTO runs"
            " (ticketId, projectId, attempt, phase, startedAt, lastHeartbeat,"
            "  timeBoxMs, ticketSnapshot, host, workerPid, workingMs, verifyMs,"
            "  revision, storyGeneration)"
            " VALUES (?, ?, ?, 'claimed', ?, ?, ?, ?, ?, ?, 0, 0, ?,"
            " (SELECT generation FROM stories WHERE ticketId IN"
            "  (SELECT storyId FROM storyChildren WHERE ticketId = ?)))",
            (ticket_id, project_id, prior + 1, now, now, estimate, snapshot,
             socket.gethostname(), os.getpid(), revision, ticket_id),
        ).lastrowid
        updated = conn.execute(
            "UPDATE tickets SET activeRunId = ? WHERE id = ? AND projectId = ?",
            (run_id, ticket_id, project_id),
        ).rowcount
        if updated != 1:
            raise ClaimConflict(
                f"ticket {ticket_id} is not a ticket of project {project_id}"
            )
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return run_id


def _assert_admitted(conn, ticket_id, expected_revision):
    row = conn.execute(
        "SELECT revision, boardColumn, status, linearIdentifier, goneSince"
        " FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        return
    revision, column, status, identifier, gone_since = row
    if revision != expected_revision:
        raise RevisionMoved(identifier, expected_revision, revision)
    if column != "ready" or status != "ready":
        raise ClaimConflict(f"ticket {identifier} is {status} in column"
                            f" {column}, not ready")
    if gone_since is not None:
        raise ClaimConflict(f"ticket {identifier} was seen gone from the"
                            " board, not ready")


PHASES = tuple(e.value for e in _enums.RunPhase)


class RunEnded(ValueError):
    def __init__(self, run_id, outcome, reason):
        super().__init__(f"run {run_id} has ended ({outcome}: {reason})")
        self.run_id, self.outcome, self.reason = run_id, outcome, reason


def set_phase(conn, run_id, phase, note=None, now=None):
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT phase, endedAt, outcome, outcomeReason FROM runs"
            " WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        previous, ended_at, outcome, reason = row
        if ended_at is not None:
            raise RunEnded(run_id, outcome, reason)
        if phase != previous and phase not in RUN_PHASE_TRANSITIONS[previous]:
            raise IllegalTransition(run_id, previous, phase)
        conn.execute(
            "UPDATE runs SET phase = ?, lastHeartbeat = ? WHERE id = ?",
            (phase, now, run_id),
        )
        _append_event(
            conn, run_id, "narrative", "phase_change",
            f"{previous} -> {phase}" + (f": {note}" if note else ""), now)
    return previous


def set_branch(conn, run_id, branch):
    with _transaction(conn):
        row = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ended_at, outcome, reason = row
        if ended_at is not None:
            raise RunEnded(run_id, outcome, reason)
        conn.execute("UPDATE runs SET branch = ? WHERE id = ?",
                     (branch, run_id))


def set_review_round_cap(conn, run_id, cap):
    with _transaction(conn):
        row = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ended_at, outcome, reason = row
        if ended_at is not None:
            raise RunEnded(run_id, outcome, reason)
        conn.execute("UPDATE runs SET reviewRoundCap = ? WHERE id = ?",
                     (cap, run_id))


def heartbeat(conn, run_id, now=None):
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        cur = conn.execute(
            "UPDATE runs SET lastHeartbeat = ? WHERE id = ? AND endedAt IS NULL",
            (now, run_id))
    return cur.rowcount == 1


def run_phase(conn, run_id):
    row = conn.execute("SELECT phase FROM runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return row[0]


EVENT_LEVELS = tuple(e.value for e in _enums.EventLevel)


def _append_event(conn, run_id, level, kind, summary, at, payload=None):
    summary = _redact_values(summary)
    if payload is not None:
        try:
            document = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            payload = _redact_values(payload)
        else:
            payload = json.dumps(_redact_document(document))
    (seq,) = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM runEvents WHERE runId = ?",
        (run_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO runEvents (runId, seq, level, kind, summary, payload, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (run_id, seq, level, kind, summary, payload, at),
    )
    return seq


def record_event(conn, run_id, kind, summary, level="narrative", now=None,
                 payload=None):
    if level not in EVENT_LEVELS:
        raise ValueError(f"unknown event level {level!r}")
    if payload is not None and level != "detail":
        raise ValueError("payload is a detail-level field")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute("SELECT 1 FROM runs WHERE id = ?",
                        (run_id,)).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        return _append_event(conn, run_id, level, kind, summary, now,
                             payload=payload)


def record_agent_session(conn, run_id, session_id, role, route):
    with _transaction(conn):
        row = conn.execute(
            "SELECT endedAt, outcome, outcomeReason FROM runs WHERE id = ?",
            (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        ended_at, outcome, reason = row
        if ended_at is not None:
            raise RunEnded(run_id, outcome, reason)
        record_event(conn, run_id, "agent_session", f"{role} session: {session_id}",
                     level="detail", payload=json.dumps({
                         "session_id": session_id, "role": role, "route": route}))
        conn.execute("UPDATE runs SET providerSessionId = ? WHERE id = ?",
                     (session_id, run_id))


def park(conn, run_id, phase, note=None, candidate_sha=None, pr_url=None,
         now=None, approved_sha=None, pr_seen=None, park_kind="question"):
    if phase not in PARKED_PHASES:
        raise ValueError(f"{phase!r} is not a phase a run is parked in")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT ticketId FROM runs WHERE id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        (ticket_id,) = row
        set_phase(conn, run_id, phase, note=note, now=now)
        conn.execute("UPDATE runs SET parkKind = ? WHERE id = ?",
                     (park_kind, run_id))
        if candidate_sha is not None:
            conn.execute("UPDATE runs SET candidateSha = ? WHERE id = ?",
                         (candidate_sha, run_id))
        if pr_url is not None:
            conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?",
                         (pr_url, run_id))
        if approved_sha is not None:
            conn.execute("UPDATE runs SET approvedSha = ? WHERE id = ?",
                         (approved_sha, run_id))
        if pr_seen is not None:
            record_pr_seen(conn, run_id, pr_seen)
        conn.execute(
            "UPDATE tickets SET activeRunId = NULL, lastRunId = ?"
            " WHERE id = ? AND activeRunId = ?",
            (run_id, ticket_id, run_id),
        )


def record_pr_seen(conn, run_id, seen, parked_only=False, facts_only=False):
    updated_at, threads, checks, review, title = seen
    guard = " AND phase = 'awaiting_merge_approval'" if parked_only else ""
    with _transaction(conn):
        if facts_only:
            conn.execute("UPDATE runs SET prSeenChecks = ?, prSeenReview = ?,"
                         f" prSeenTitle = ? WHERE id = ?{guard}",
                         (checks, review, title, run_id))
            return
        conn.execute("UPDATE runs SET prSeenAt = ?, prSeenThreads = ?,"
                     " prSeenChecks = ?, prSeenReview = ?, prSeenTitle = ?"
                     f" WHERE id = ?{guard}",
                     (updated_at, threads, checks, review, title, run_id))


def _json_list(field, values):
    if isinstance(values, str):
        raise ValueError(f"{field} must be a list of strings, got {values!r}")
    items = list(values)
    bad = [v for v in items if not isinstance(v, str)]
    if bad:
        raise ValueError(f"{field} must contain only strings, got {bad[0]!r}")
    return json.dumps(items)


LEDGER_KINDS = tuple(e.value for e in _enums.LedgerKind)
LEDGER_SOURCES = tuple(e.value for e in _enums.LedgerSource)


def record_ledger(conn, run_id, kind, text, source="loop", now=None):
    if isinstance(text, str):
        text = _redact_values(text)
    if kind not in LEDGER_KINDS:
        raise ValueError(f"unknown ledger kind {kind!r}")
    if source not in LEDGER_SOURCES:
        raise ValueError(f"unknown ledger source {source!r}")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"text must be non-empty, got {text!r}")
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute("SELECT ticketId FROM runs WHERE id = ?",
                           (run_id,)).fetchone()
        if row is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            "INSERT INTO ledger (runId, ticketId, at, kind, text, source)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, row[0], now, kind, text, source))
    return cursor.lastrowid


# `reviewRounds.findings` is JSON, so no CHECK stands behind these.
SEVERITIES = ("p0", "p1", "p2", "nit")

# Not escaped: `_finding_keys()` rejects a path holding either, so a path
# cannot forge a record boundary and stored digests stay valid.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"

_NO_LINE = -1

# A literal: it is stored and compared across releases, so it cannot drift.
EMPTY_FINGERPRINT = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)


def _finding_keys(findings):
    keys = set()
    for finding in findings:
        try:
            key_finding = finding.get("fingerprint", finding)
            path = key_finding["path"]
            severity = key_finding["severity"]
        except (AttributeError, TypeError, KeyError) as exc:
            raise ValueError(
                f"finding must carry a path and a severity, got {finding!r}"
            ) from exc
        if not isinstance(path, str) or not path:
            raise ValueError(f"finding path must be a non-empty string, got {path!r}")
        if _FIELD_SEP in path or _RECORD_SEP in path:
            raise ValueError(
                "finding path must not contain the canonical form's ASCII"
                f" separators, got {path!r}"
            )
        if severity not in SEVERITIES:
            raise ValueError(
                f"finding severity must be one of {SEVERITIES}, got {severity!r}"
            )
        line = key_finding.get("line")
        if line is None:
            line = _NO_LINE
        elif isinstance(line, bool) or not isinstance(line, int):
            raise ValueError(
                f"finding line must be an integer or absent, got {line!r}"
            )
        elif line < 1:
            raise ValueError(
                f"finding line must be a positive integer or absent, got {line!r}"
            )
        if finding.get("evidence_only") is not True:
            keys.add((path, line, severity))
    return keys


def _message_digest(finding):
    return " ".join(str(finding.get("message", "")).split()).lower()


def findings_fingerprint(findings):
    canonical = _RECORD_SEP.join(
        _FIELD_SEP.join((path, str(line), severity))
        for path, line, severity in sorted(_finding_keys(findings))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def findings_overlap(earlier, later):
    earlier_keys = _finding_keys(earlier)
    later_keys = _finding_keys(later)
    earlier = [f for f in earlier if f.get("evidence_only") is not True]
    later = [f for f in later if f.get("evidence_only") is not True]
    union = earlier_keys | later_keys
    if not union:
        return 1.0
    # One coarse key can stand for two different complaints; compare messages.
    if len(union) == 1 and earlier and later:
        earlier_messages = {_message_digest(f) for f in earlier}
        later_messages = {_message_digest(f) for f in later}
        return 1.0 if earlier_messages == later_messages else 0.0
    return len(earlier_keys & later_keys) / len(union)


ROUND_VERDICTS = tuple(e.value for e in _enums.ReviewVerdict)


def _document_argument(label, value):
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ValueError(
        f"{label} must be a list of objects, got {type(value).__name__}")


def _json_document(label, value):
    try:
        return json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a JSON document: {exc}") from exc


def record_review_round(conn, run_id, round_number, verdict, reviewer_model,
                        findings=(), verification_results=(),
                        started_at=None, ended_at=None):
    if verdict not in ROUND_VERDICTS:
        raise ValueError(
            f"round verdict must be one of {ROUND_VERDICTS}, got {verdict!r}"
        )
    if (isinstance(round_number, bool) or not isinstance(round_number, int)
            or round_number < 1):
        raise ValueError(
            f"round must be a positive integer, got {round_number!r}"
        )
    findings = _document_argument("findings", findings)
    fingerprint = findings_fingerprint(findings)
    findings_json = _json_document("findings", findings)
    results_json = _json_document(
        "verification results",
        _redact_document(_document_argument(
            "verification results", verification_results)))
    if started_at is None:
        started_at = int(time.time() * 1000)
    with _transaction(conn):
        if conn.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone() is None:
            raise ValueError(f"no run {run_id}")
        cursor = conn.execute(
            "INSERT INTO reviewRounds (runId, round, verificationResults,"
            " verdict, findings, findingsFingerprint, reviewerModel,"
            " startedAt, endedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, round_number, results_json, verdict, findings_json,
             fingerprint, reviewer_model, started_at, ended_at),
        )
    return cursor.lastrowid


from .operate import (  # noqa: E402,F401 - re-export after the run API it calls
    APPROVED_RESUME_PHASE,
    ENDED_PHASES,
    FULL_SHA,
    GATE_CONFLICT_REASON,
    INTERVENTION_ACTIONS,
    INTERVENTION_SOURCES,
    INTERVENTION_TRIGGERS,
    OUTCOME_CLASSES,
    PARKED_PHASES,
    RESUMABLE_PHASES,
    RESUMABLE_WORK_PHASES,
    RUN_PHASE_TRANSITIONS,
    TERMINAL_PHASES,
    ApproveRefused,
    RepointRefused,
    RequeueRefused,
    ResumeRefused,
    _release_parked,
    abort,
    approve,
    babysit,
    hold,
    is_gate_conflict,
    latest_supervisor_heartbeat,
    pause,
    record_intervention,
    record_loop_restart,
    record_loop_return,
    record_project_intervention,
    record_strike,
    record_supervisor_heartbeat,
    release,
    release_hold,
    repair_references,
    repoint,
    requeue,
    resume,
    unreturned_loop_restarts,
)


class GuidanceNotAccepted(ResumeRefused):
    pass


from .notes import (  # noqa: E402,F401
    mark_note_failed,
    mark_note_posted,
    record_note,
)
from .revisions import record_board_fields  # noqa: E402,F401
from .tickets import (  # noqa: E402,F401 - re-export after `_json_list`
    STATE_GRAPHS,
    TICKET_STATUSES,
    TICKET_TRANSITIONS,
    IllegalTransition,
    Pickability,
    ensure_project,
    list_projects,
    mirror_ticket,
    pickable,
    pickable_tickets,
    register_project,
    render_state_graph,
    render_state_graph_section,
    set_admission,
    transition,
    walk_ticket,
)

if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--state-graph"]:
        sys.exit("usage: python3 store.py --state-graph")
    print("\n".join(render_state_graph_section(name, globals()[table])
                    for name, table in STATE_GRAPHS), end="")
