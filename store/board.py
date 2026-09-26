"""store.board: filing, editing, moving and canceling a ticket on a board
the store owns.

A native board is the store itself, so filing and editing a ticket are
store writes (KO-750). `file_ticket()` numbers a ticket `KEY-n` from
`projects.ticketSeq`; `edit_ticket()` replaces its body at the revision it
was read at. Both validate as `--file-ticket` does (`ticket_problems()`),
resolve `Depends on:` to the project's own tickets, and write the row
through `mirror_ticket()`, so the status routing and revision rules are
the mirror's.

`move_ticket()` and `cancel_ticket()` change the column at the revision it
was read at, each with a note carrying the person's words (KO-753); a
cancel is the one board event that reaches a live run. `resolve_dependencies()`
ends a dependency wait once every dependency has merged.
"""
from __future__ import annotations

import json
import time

import ticket_template

from . import RevisionMoved
from .notes import record_note
from .operate import abort
from .revisions import record_board_fields
from .schema import _transaction
from .tickets import mirror_ticket, transition, walk_ticket
from .writes import set_board_state

# The board state each native column reads as, as a Linear board's would.
_BOARD_STATES = {"backlog": "Backlog", "ready": "Ready", "canceled": "Canceled"}


class FilingRefused(ValueError):
    """A ticket was not filed or edited, and nothing was written.

    `problems` lists every reason, the first of them the message.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__(self.problems[0])


def ticket_problems(text, repo):
    """What filing refuses in `text`, checked against `repo`: the blocking
    template violations plus the advisories `filing_refusals()` names."""
    return ticket_template.filing_refusals(
        ticket_template.validate(ticket_template.parse(text), repo=repo))


def file_ticket(conn, project_id, key, text, column="ready", priority=None,
                author="cli", now=None):
    """File `text` as project `project_id`'s next ticket; answer `KEY-n`.

    The number is `projects.ticketSeq` plus one, bumped in the insert's own
    `BEGIN IMMEDIATE`, so two filers never share one and a refused filing
    uses none. The ticket's board id is its identifier, and its first
    revision is authored `author`.

    A body with problems (`ticket_problems()`) is refused unless `column`
    is `backlog`, where it is saved as a draft with its contract withheld,
    so it lands `needs_spec`. A `Depends on:` naming a ticket the project
    does not hold is refused; one naming an unmerged ticket parks a `ready`
    ticket at `blocked_on_deps`. A refusal raises `FilingRefused`.
    """
    if now is None:
        now = int(time.time() * 1000)
    problems = ticket_problems(text, _repo_path(conn, project_id))
    if problems and column != "backlog":
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
    """Replace ticket `identifier`'s body with `text`; answer its revision.

    `expected_revision` is the revision the editor read. When the ticket
    has moved past it, `RevisionMoved` is raised with `current` set and
    nothing is written. The body is judged as `file_ticket()` judges it,
    by the ticket's column; `priority` and `labels`, None, keep the row's.
    """
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
        if problems and column != "backlog":
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
    """Move ticket `identifier` to column `column`, `ready` or `backlog`;
    answer its new revision.

    Refused at a stale `expected_revision` as `edit_ticket()` refuses one,
    and with `FilingRefused` for a canceled or closed ticket, a ticket
    already in `column`, or a move to `ready` of a body `ticket_problems()`
    refuses, so a draft stays in Backlog. The column and its board state
    are recorded as a revision authored `author`, with a `move` note
    reading `note`. A live run on a ticket moved to Backlog continues.
    """
    if column not in ("ready", "backlog"):
        raise ValueError(f"a ticket moves to ready or backlog, not {column!r}")
    if now is None:
        now = int(time.time() * 1000)
    repo = _repo_path(conn, project_id)
    with _transaction(conn):
        ticket_id, _status, _run, text = _open_ticket(
            conn, project_id, identifier, expected_revision)
        (current,) = conn.execute("SELECT boardColumn FROM tickets"
                                  " WHERE id = ?", (ticket_id,)).fetchone()
        if current == column:
            raise FilingRefused([f"{identifier} is already in {column}"])
        if column == "ready":
            problems = ticket_problems(text, repo)
            if problems:
                raise FilingRefused(problems)
        return _record_column(conn, ticket_id, column, "move",
                              note or f"Moved to {_BOARD_STATES[column]}.",
                              author, now)


def cancel_ticket(conn, project_id, identifier, expected_revision, note,
                  author="cli", now=None):
    """Cancel ticket `identifier`; answer its new revision.

    Refused as `move_ticket()` refuses. The column `canceled` and board
    state `Canceled` are recorded as a revision authored `author`, with a
    `cancel` note reading `note`, and in the same transaction the ticket's
    work is stopped as a Linear cancel stops it (KO-660, KO-741): a live
    run gets `abort()` from source `human`, trigger `manual`, and its
    worker ends it `abandoned` at its next safe point; a ticket
    `blocked_on_operator` is left for the reconcile's `_close_canceled()`
    to finish, parked run first; any other ticket is walked `abandoned`.
    """
    if now is None:
        now = int(time.time() * 1000)
    with _transaction(conn):
        ticket_id, status, run_id, _text = _open_ticket(
            conn, project_id, identifier, expected_revision)
        revision = _record_column(conn, ticket_id, "canceled", "cancel", note,
                                  author, now)
        if run_id is not None:
            abort(conn, run_id, note, source="human", now=now,
                  trigger="manual")
        elif status != "blocked_on_operator":
            walk_ticket(conn, ticket_id, "abandoned")
    return revision


def resolve_dependencies(conn, project_id):
    """Walk each of the project's `blocked_on_deps` tickets whose
    dependencies have all merged back to `ready`; answer their identifiers.

    A dependency is merged as `pickable()` judges one: a sibling of the
    same project at status `merged`; one the project does not hold keeps
    the ticket waiting.
    """
    with _transaction(conn):
        rows = conn.execute(
            "SELECT id, linearIdentifier, linearIssueId, status, dependsOn"
            " FROM tickets WHERE projectId = ? ORDER BY id",
            (project_id,)).fetchall()
        status_of = {row[2]: row[3] for row in rows}
        resolved = []
        for ticket_id, identifier, _, status, depends in rows:
            if status == "blocked_on_deps" and all(
                    status_of.get(dep) == "merged"
                    for dep in json.loads(depends)):
                transition(conn, ticket_id, "ready")
                resolved.append(identifier)
    return resolved


def _open_ticket(conn, project_id, identifier, expected_revision):
    """Ticket `identifier`'s id, status, live run and body, inside the
    caller's transaction, when it is at `expected_revision` and still open
    on the board; `RevisionMoved` or `FilingRefused` otherwise."""
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
    """Set the ticket's column and board state, record them as its next
    revision and write a `kind` note keyed on it; answer the revision."""
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
    """`text`'s `Depends on:` as the project's board ids, and whether any
    of them is unmerged; `FilingRefused` names each one it does not hold."""
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
    """Write `text` as the ticket's row through `mirror_ticket()`, its
    contract withheld when it is not `specced`."""
    from holophyte.board import task_contract
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
    """Move a `ready` ticket that waits on an unmerged one to
    `blocked_on_deps`."""
    (status,) = conn.execute("SELECT status FROM tickets WHERE id = ?",
                             (ticket_id,)).fetchone()
    if waiting and status == "ready":
        transition(conn, ticket_id, "blocked_on_deps")
