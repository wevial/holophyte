"""store.board: filing, editing, moving and canceling a native board's tickets."""
from __future__ import annotations

import json
import time

import ticket_template

from . import RevisionMoved
from .notes import record_note
from .operate import abort, record_intervention, release
from .revisions import record_board_fields
from .schema import _transaction
from .stories import abandon_story, story
from .tickets import mirror_ticket, transition, walk_ticket
from .writes import set_board_state

_BOARD_STATES = {"backlog": "Backlog", "ready": "Ready", "canceled": "Canceled"}


class FilingRefused(ValueError):
    """A ticket was not filed or edited, and nothing was written."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(self.problems[0])


def ticket_problems(text, repo):
    ticket = ticket_template.parse(text)
    problems = ticket_template.filing_refusals(
        ticket_template.validate(ticket, repo=repo))
    if repo:
        # Deferred: holophyte imports this module.
        from holophyte.config.project import Project
        from holophyte.leak_guard import ticket_problems as private_problems
        from holophyte.pr.pr_media import project_problems
        problems += project_problems(Project.locate(repo, adopt=False), ticket)
        problems = private_problems(repo, text, problems)
    return problems


def _blocks_filing(problems, column):
    from holophyte.leak_guard import KEY
    return bool(problems) and (column != "backlog"
                               or any(KEY in problem for problem in problems))


def file_ticket(conn, project_id, key, text, column="ready", priority=None,
                author="cli", now=None):
    """File `text` as the next `KEY-n`; a refused filing uses no number."""
    if now is None:
        now = int(time.time() * 1000)
    problems = ticket_problems(text, _repo_path(conn, project_id))
    if _blocks_filing(problems, column):
        raise FilingRefused(problems)
    with _transaction(conn):
        depends, waiting = _dependencies(conn, project_id, text)
        conn.execute("UPDATE projects SET ticketSeq = ticketSeq + 1"
                     " WHERE id = ?", (project_id,))
        (seq,) = conn.execute("SELECT ticketSeq FROM projects WHERE id = ?",
                              (project_id,)).fetchone()
        identifier = f"{key}-{seq}"
        if conn.execute("SELECT 1 FROM tickets WHERE linearIssueId = ?",
                        (identifier,)).fetchone():
            raise FilingRefused([f"ticket {identifier} already exists"])
        ticket_id = _write(
            conn, project_id, identifier, identifier, text, not problems,
            author, now, depends_on=depends, priority=priority,
            board_column=column, filed_at=now)
        _park(conn, ticket_id, waiting)
    return identifier


def edit_ticket(conn, project_id, identifier, text, expected_revision,
                author="cli", priority=None, labels=None, now=None):
    if now is None:
        now = int(time.time() * 1000)
    problems = ticket_problems(text, _repo_path(conn, project_id))
    with _transaction(conn):
        row = conn.execute(
            "SELECT id, linearIssueId, revision, boardColumn, url, boardState,"
            " affinity FROM tickets WHERE projectId = ? AND linearIdentifier = ?",
            (project_id, identifier)).fetchone()
        if row is None:
            raise FilingRefused([f"no ticket {identifier} in this project"])
        ticket_id, issue_id, revision, column, url, state, affinity = row
        if revision != expected_revision:
            raise RevisionMoved(identifier, expected_revision, revision)
        if _blocks_filing(problems, column):
            raise FilingRefused(problems)
        depends, waiting = _dependencies(conn, project_id, text, ticket_id)
        _write(conn, project_id, issue_id, identifier, text, not problems,
               author, now, depends_on=depends, priority=priority,
               labels=labels, url=url, board_state=state, affinity=affinity,
               expected_revision=expected_revision)
        _park(conn, ticket_id, waiting)
        (revision,) = conn.execute("SELECT revision FROM tickets WHERE id = ?",
                                   (ticket_id,)).fetchone()
    return revision


def move_ticket(conn, project_id, identifier, column, expected_revision,
                author="cli", note=None, now=None):
    """A live run on a ticket moved to Backlog continues."""
    if column not in ("ready", "backlog"):
        raise ValueError(f"a ticket moves to ready or backlog, not {column!r}")
    if now is None:
        now = int(time.time() * 1000)
    repo = _repo_path(conn, project_id)
    with _transaction(conn):
        ticket_id, status, _run, text = _open_ticket(
            conn, project_id, identifier, expected_revision)
        (current,) = conn.execute("SELECT boardColumn FROM tickets"
                                  " WHERE id = ?", (ticket_id,)).fetchone()
        if current == column:
            raise FilingRefused([f"{identifier} is already in {column} at"
                                 f" status {status}; nothing changed"])
        if column == "ready":
            problems = ticket_problems(text, repo)
            if problems:
                raise FilingRefused(problems)
        return _record_column(conn, ticket_id, column, "move",
                              note or f"Moved to {_BOARD_STATES[column]}.",
                              author, now)


def cancel_ticket(conn, project_id, identifier, expected_revision, note,
                  author="cli", now=None):
    """Cancel as a board does; a run awaiting merge approval ends abandoned."""
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        ticket_id, status, run_id, _text = _open_ticket(
            conn, project_id, identifier, expected_revision)
        _refuse_merged_park(conn, ticket_id, identifier)
        revision = _record_column(conn, ticket_id, "canceled", "cancel", note,
                                  author, now)
        if run_id is not None:
            abort(conn, run_id, note, source="human", now=now,
                  trigger="board_cancelled")
        elif _close_parked_run(conn, ticket_id, identifier, now):
            walk_ticket(conn, ticket_id, "abandoned")
        elif status != "blocked_on_operator" \
                or not _parked_on_pull_request(conn, ticket_id):
            walk_ticket(conn, ticket_id, "abandoned")
        found = story(conn, ticket_id)
        if found is not None and found.ticketId == ticket_id:
            abandon_story(conn, ticket_id, note, author, now=now)
    return revision


def resolve_dependencies(conn, project_id):
    """A wait with no dependencies, or a draft's, stays blocked."""
    with _transaction(conn):
        rows = conn.execute(
            "SELECT id, linearIdentifier, linearIssueId, status, dependsOn,"
            " acceptanceCriteria, verificationCommands"
            " FROM tickets WHERE projectId = ? ORDER BY id",
            (project_id,)).fetchall()
        status_of = {row[2]: row[3] for row in rows}
        resolved = []
        for (ticket_id, identifier, _, status, depends, criteria,
             commands) in rows:
            if status != "blocked_on_deps":
                continue
            depends = json.loads(depends)
            if (depends and json.loads(criteria) and json.loads(commands)
                    and all(status_of.get(dep) == "merged"
                            for dep in depends)):
                transition(conn, ticket_id, "ready")
                resolved.append(identifier)
    return resolved


def _refuse_merged_park(conn, ticket_id, identifier):
    row = conn.execute(
        "SELECT r.id FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.id = ? AND r.endedAt IS NULL"
        " AND r.phase = 'blocked_on_operator'", (ticket_id,)).fetchone()
    if row is not None:
        raise FilingRefused([
            f"{identifier}'s run {row[0]} is parked blocked_on_operator after"
            " its merge landed and cannot end abandoned; nothing changed"])


def _close_parked_run(conn, ticket_id, identifier, now):
    row = conn.execute(
        "SELECT r.id, r.prUrl FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.id = ? AND r.endedAt IS NULL"
        " AND r.phase = 'awaiting_merge_approval'", (ticket_id,)).fetchone()
    if row is None:
        return False
    run_id, url = row
    kept = "" if url is None else f" and {url} left open"
    record_intervention(
        conn, run_id, "close_out",
        f"{identifier} canceled on the board; run {run_id} ended"
        f" abandoned{kept}", source="human", trigger="board_cancelled",
        now=now)
    release(conn, run_id, "abandoned", f"canceled on the board{kept}",
            now=now)
    return True


def _parked_on_pull_request(conn, ticket_id):
    return conn.execute(
        "SELECT 1 FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.id = ? AND r.phase IN ('awaiting_merge_approval', 'rejected')"
        " AND r.prUrl IS NOT NULL", (ticket_id,)).fetchone() is not None


def _open_ticket(conn, project_id, identifier, expected_revision):
    row = conn.execute(
        "SELECT id, revision, boardColumn, status, activeRunId, body"
        " FROM tickets WHERE projectId = ? AND linearIdentifier = ?",
        (project_id, identifier)).fetchone()
    if row is None:
        raise FilingRefused([f"no ticket {identifier} in this project"])
    ticket_id, revision, column, status, run_id, text = row
    if revision != expected_revision:
        raise RevisionMoved(identifier, expected_revision, revision)
    if column == "canceled":
        raise FilingRefused([f"{identifier} is canceled"])
    if status in ("merged", "abandoned"):
        raise FilingRefused([f"{identifier} is closed: {status}"])
    return ticket_id, status, run_id, text or ""


def _record_column(conn, ticket_id, column, kind, text, author, now):
    set_board_state(conn, ticket_id, _BOARD_STATES[column], column)
    revision = record_board_fields(conn, ticket_id, author, now)
    record_note(conn, ticket_id, kind, text, f"{kind}:{revision}",
                author=author, now=now)
    return revision


def _repo_path(conn, project_id):
    row = conn.execute("SELECT repoPath FROM projects WHERE id = ?",
                       (project_id,)).fetchone()
    if row is None:
        raise FilingRefused([f"project {project_id} does not exist"])
    return row[0]


def _dependencies(conn, project_id, text, self_id=None):
    named = ticket_template.parse(text).depends_on or []
    rows = {identifier: (issue_id, status) for identifier, issue_id, status in
            conn.execute("SELECT linearIdentifier, linearIssueId, status"
                         " FROM tickets WHERE projectId = ? AND id IS NOT ?",
                         (project_id, self_id))}
    unknown = [dep for dep in named if dep not in rows]
    if unknown:
        raise FilingRefused([f"'Depends on' names no ticket of this project:"
                             f" {dep}" for dep in unknown])
    return ([rows[dep][0] for dep in named],
            any(rows[dep][1] != "merged" for dep in named))


def _write(conn, project_id, issue_id, identifier, text, specced, author,
           now, **fields):
    from holophyte.board.projection import task_contract
    from provider import parse_body
    task = parse_body(identifier, text)
    title, criteria, commands, _states = task_contract(task)
    if not specced:
        criteria, commands = [], []
    return mirror_ticket(
        conn, project_id, linear_issue_id=issue_id,
        linear_identifier=identifier, title=title,
        acceptance_criteria=criteria, verification_commands=commands,
        time_box_ms=task["budget_min"] * 60 * 1000, body=text, now=now,
        board_updated_at=now, author=author, **fields)


def _park(conn, ticket_id, waiting):
    (status,) = conn.execute("SELECT status FROM tickets WHERE id = ?",
                             (ticket_id,)).fetchone()
    if waiting and status == "ready":
        transition(conn, ticket_id, "blocked_on_deps")
