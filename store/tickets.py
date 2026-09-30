"""store.tickets: the ticket state machine and §2's pickability predicate."""
from __future__ import annotations

import collections
import json
import time
from pathlib import Path

from . import _json_list
from . import enums as _enums
from .enums import TicketStatus as _Status
from .project_paths import canonical_projects
from .revisions import record_board_fields
from .schema import _transaction


def ensure_project(conn, linear_team_id, repo_path, default_branch="main",
                   autonomy_profile="personal"):
    """The projects row for `linear_team_id`; an existing row is never re-pointed."""
    path = str(Path(repo_path).resolve())
    with _transaction(conn):
        canonical_projects(conn)
        row = conn.execute(
            "SELECT id FROM projects WHERE linearTeamId = ?", (linear_team_id,)
        ).fetchone()
        if row is not None:
            return row[0]
        return conn.execute(
            "INSERT INTO projects"
            " (linearTeamId, repoPath, defaultBranch, autonomyProfile)"
            " VALUES (?, ?, ?, ?)",
            (linear_team_id, path, default_branch, autonomy_profile),
        ).lastrowid


def register_project(conn, linear_team_id, repo_path):
    """Adopt the row `ensure_project()` wrote for this team and path, recorded once."""
    path = str(Path(repo_path).resolve())
    with _transaction(conn):
        paths = canonical_projects(conn)
        row = next((row for row in conn.execute(
            "SELECT id, repoPath, linearTeamId FROM projects ORDER BY id")
            if row[2] == linear_team_id or paths[row[0]] == path), None)
        if row and row[2] == linear_team_id and paths[row[0]] == path:
            project = row[0]
            if conn.execute(
                    "SELECT 1 FROM interventions WHERE projectId = ?"
                    " AND action = 'register_project'", (project,)).fetchone():
                return project
        elif row:
            raise ValueError(f"project {row[0]} already registered: {row[1]}")
        else:
            project = ensure_project(conn, linear_team_id, path)
        conn.execute(
            'INSERT INTO interventions (projectId, source, "trigger", action, note, at)'
            " VALUES (?, 'human', 'manual', 'register_project', ?, ?)",
            (project, f"registered {path}", int(time.time() * 1000)))
        return project


def list_projects(conn):
    rows = conn.execute(
        "SELECT id, repoPath, admission, holdNote, "
        "(SELECT id FROM runs WHERE projectId = projects.id "
        "ORDER BY startedAt DESC, id DESC LIMIT 1) FROM projects").fetchall()
    return sorted(rows, key=lambda row: (Path(row[1]).name, row[1], row[0]))


def set_admission(conn, project_id, admission, note):
    from .operate import _set_admission
    actions = {"enabled": "release_hold", "held": "hold", "disabled": "disable"}
    if admission not in actions:
        raise ValueError(f"unknown admission {admission!r}")
    return _set_admission(conn, project_id, note, admission, actions[admission])


# State-model §3's diagram plus `in_flight -> blocked_on_operator`, the escalation
# edge. No status is in its own set, so a no-op write is refused.
TICKET_TRANSITIONS = {
    _Status.NEEDS_SPEC.value: frozenset({_Status.READY.value}),
    _Status.READY.value: frozenset({
        _Status.IN_FLIGHT.value,
        _Status.BLOCKED_ON_DEPS.value}),
    _Status.IN_FLIGHT.value: frozenset({
        _Status.MERGED.value, _Status.ABANDONED.value,
        _Status.BLOCKED_ON_OPERATOR.value}),
    _Status.BLOCKED_ON_DEPS.value: frozenset({
        _Status.READY.value,
        _Status.BLOCKED_ON_OPERATOR.value}),
    _Status.BLOCKED_ON_OPERATOR.value: frozenset({_Status.BLOCKED_ON_DEPS.value}),
    _Status.MERGED.value: frozenset(),
    _Status.ABANDONED.value: frozenset(),
}

TICKET_STATUSES = tuple(e.value for e in _enums.TicketStatus)


def render_state_graph(transitions):
    """Deterministic: a test compares README's copy of the output byte for byte."""
    lines = ["stateDiagram-v2"]
    lines.extend(f"    {state}" for state in sorted(transitions))
    lines.extend(f"    {src} --> {dst}"
                 for src in sorted(transitions)
                 for dst in sorted(transitions[src]))
    return "\n".join(lines) + "\n"


STATE_GRAPHS = (
    ("state-graph: tickets", "TICKET_TRANSITIONS"),
    ("state-graph: runs", "RUN_PHASE_TRANSITIONS"),
)


def render_state_graph_section(name, transitions):
    return (f"<!-- {name} -->\n```mermaid\n{render_state_graph(transitions)}"
            f"```\n<!-- end {name} -->\n")


class IllegalTransition(ValueError):
    """A refused edge, unknown status or unknown ticket; nothing was written."""

    def __init__(self, run_id, previous=None, phase=None):
        if previous is None:
            super().__init__(run_id)
        else:
            super().__init__(
                f"run {run_id}: illegal phase transition {previous} -> {phase}")
            self.run_id, self.previous, self.phase = run_id, previous, phase


def transition(conn, ticket_id, to_status):
    """Move `ticket_id` along one §3 edge; return the status it came from."""
    with _transaction(conn):
        row = conn.execute(
            "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise IllegalTransition(f"ticket {ticket_id} does not exist")
        (from_status,) = row
        if to_status not in TICKET_TRANSITIONS.get(from_status, frozenset()):
            raise IllegalTransition(
                f"ticket {ticket_id}: {from_status} -> {to_status} is not a"
                " transition the state-model §3 diagram draws"
            )
        conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ?", (to_status, ticket_id)
        )
        if from_status == "blocked_on_operator":
            conn.execute("UPDATE runs SET parkKind = NULL WHERE ticketId = ?",
                         (ticket_id,))
        if to_status == "merged" and not _story_advanced(conn, ticket_id):
            conn.execute(
                "UPDATE stories SET generation = generation + 1"
                " WHERE ticketId IN (SELECT DISTINCT storyId FROM storyChildren"
                " WHERE ticketId = ?)", (ticket_id,))
    return from_status


STORY_ADVANCED = "story_advanced"


def _story_advanced(conn, ticket_id):
    return conn.execute(
        "SELECT 1 FROM runEvents e JOIN runs r ON r.id = e.runId"
        " WHERE r.ticketId = ? AND e.kind = ?",
        (ticket_id, STORY_ADVANCED)).fetchone() is not None


def _status_path(from_status, to_status):
    frontier, seen = [(from_status, ())], {from_status}
    while frontier:
        status, path = frontier.pop(0)
        for nxt in sorted(TICKET_TRANSITIONS[status]):
            if nxt == to_status:
                return (*path, nxt)
            if nxt not in seen:
                seen.add(nxt)
                frontier.append((nxt, (*path, nxt)))
    return None


def walk_ticket(conn, ticket_id, to_status):
    """Move `ticket_id` along the shortest §3 path; return the path taken."""
    if to_status not in TICKET_TRANSITIONS:
        raise IllegalTransition(f"unknown status {to_status!r}")
    with _transaction(conn):
        row = conn.execute("SELECT status FROM tickets WHERE id = ?",
                           (ticket_id,)).fetchone()
        if row is None:
            raise IllegalTransition(f"ticket {ticket_id} does not exist")
        (from_status,) = row
        if from_status == to_status:
            return ()
        path = _status_path(from_status, to_status)
        if path is None:
            raise IllegalTransition(
                f"ticket {ticket_id}: no §3 path from {from_status}"
                f" to {to_status}")
        for status in path:
            transition(conn, ticket_id, status)
    return path


def mirror_ticket(
    conn,
    project_id,
    linear_issue_id,
    linear_identifier,
    title,
    acceptance_criteria=(),
    verification_commands=(),
    time_box_ms=None,
    affinity="any",
    depends_on=None,
    now=None,
    body="",
    url=None,
    board_state=None,
    priority=None,
    labels=None,
    board_column=None,
    filed_at=None,
    board_updated_at=None,
    expected_revision=None,
    author="board",
):
    """Upsert a board issue's mirror; a None field keeps what the row holds."""
    criteria = _json_list("acceptance_criteria", acceptance_criteria)
    commands = _json_list("verification_commands", verification_commands)
    depends = None if depends_on is None else _json_list("depends_on", depends_on)
    labels = None if labels is None else _json_list("labels", labels)
    specced = bool(json.loads(criteria)) and bool(json.loads(commands))
    derived = "ready" if specced else "needs_spec"
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        row = conn.execute(
            "SELECT id, status, revision FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (linear_issue_id, project_id),
        ).fetchone()
        if row is not None and expected_revision not in (None, row[2]):
            return row[0]
        if row is None:
            ticket_id = conn.execute(
                "INSERT INTO tickets"
                " (projectId, linearIssueId, linearIdentifier, title, body,"
                "  status, acceptanceCriteria, verificationCommands, timeBoxMs,"
                "  affinity, dependsOn, mirroredAt, url, boardState, priority,"
                "  labels, boardColumn, filedAt, boardUpdatedAt)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project_id, linear_issue_id, linear_identifier, title,
                    body, derived, criteria, commands, time_box_ms, affinity,
                    "[]" if depends is None else depends, now, url, board_state,
                    priority, "[]" if labels is None else labels, board_column,
                    filed_at, board_updated_at,
                ),
            ).lastrowid
        else:
            ticket_id, status, _ = row
            if conn.execute("SELECT 1 FROM stories WHERE ticketId = ?",
                            (ticket_id,)).fetchone():
                criteria, commands, derived = "[]", "[]", "needs_spec"
            # Only these follow the body; any other move is a `transition()`.
            if status in ("needs_spec", "ready"):
                status = derived
            record_board_fields(conn, ticket_id, "unrecorded", now)
            conn.execute(
                "UPDATE tickets SET linearIdentifier = ?, title = ?, body = ?,"
                " status = ?, acceptanceCriteria = ?, verificationCommands = ?,"
                " timeBoxMs = ?, affinity = ?,"
                " dependsOn = COALESCE(?, dependsOn), mirroredAt = ?,"
                " url = ?, boardState = ?, priority = COALESCE(?, priority),"
                " labels = COALESCE(?, labels),"
                " boardColumn = COALESCE(?, boardColumn),"
                " filedAt = COALESCE(?, filedAt),"
                " boardUpdatedAt = COALESCE(?, boardUpdatedAt)"
                " WHERE id = ?",
                (
                    linear_identifier, title, body, status, criteria, commands,
                    time_box_ms, affinity, depends, now, url, board_state,
                    priority, labels, board_column, filed_at, board_updated_at,
                    ticket_id,
                ),
            )
        record_board_fields(conn, ticket_id, author, now)
    return ticket_id


class Pickability(collections.namedtuple("Pickability", ("pickable", "reason"))):
    """`bool()` is the verdict, not a tuple's truth; `.reason` names the failure."""

    __slots__ = ()

    def __bool__(self):
        return self.pickable


def pickable(conn, ticket_id):
    """§2's claimability predicate, failing closed on anything unknown."""
    row = conn.execute(
        "SELECT projectId, status, activeRunId, acceptanceCriteria,"
        " verificationCommands, dependsOn FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        return Pickability(False, f"ticket {ticket_id} does not exist")
    project_id = row[0]

    def dep_status(dep):
        dep_row = conn.execute(
            "SELECT status FROM tickets"
            " WHERE linearIssueId = ? AND projectId = ?",
            (dep, project_id),
        ).fetchone()
        return None if dep_row is None else dep_row[0]

    return _pickability(row, dep_status)


def pickable_tickets(conn, project_id):
    """`pickable()` for every ticket of `project_id` in one read, by identifier."""
    rows = conn.execute(
        "SELECT projectId, status, activeRunId, acceptanceCriteria,"
        " verificationCommands, dependsOn, linearIssueId, linearIdentifier"
        " FROM tickets WHERE projectId = ?",
        (project_id,),
    ).fetchall()
    status_of = {row[6]: row[1] for row in rows}
    return {row[7]: _pickability(row[:6], status_of.get) for row in rows}


def _pickability(row, dep_status):
    project_id, status, active_run_id, criteria, commands, depends_on = row
    if status != "ready":
        return Pickability(False, f"status is {status}, not ready")
    if active_run_id is not None:
        return Pickability(False, f"run {active_run_id} is already active on it")
    if not json.loads(criteria):
        return Pickability(False, "it has no acceptance criteria")
    if not json.loads(commands):
        return Pickability(False, "it has no verification commands")
    for dep in json.loads(depends_on):
        dep_state = dep_status(dep)
        if dep_state is None:
            return Pickability(False, f"it depends on {dep}, which is not mirrored")
        if dep_state != "merged":
            return Pickability(
                False, f"it depends on {dep}, which is {dep_state}, not merged"
            )
    return Pickability(True, None)
