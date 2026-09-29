"""Approving a planned story: the baseline at main's tip, then one freeze."""
from contextlib import closing

import store
import store.board
import store.stories
import store.tickets
from holophyte.witness import main_tip, run_witnesses

TODO_STATE = "Todo"
CLOSED_STATUSES = ("merged", "abandoned")


class ApprovalRefused(ValueError):
    """The story was not approved; `lines` says why."""

    def __init__(self, lines):
        self.lines = list(lines)
        super().__init__(self.lines[0])


def approve(board, project, identifier, revision, note, green=(),
            exception=()):
    """Approve story `identifier` at its parent's `revision`; answer the
    lines to print, or raise `ApprovalRefused`."""
    from holophyte.runs import open_store
    with closing(open_store(project)) as conn:
        project_id = store.tickets.ensure_project(conn, board.team,
                                                  project.path)
        parent_id, found = _planned(conn, project_id, identifier, revision)
        _check_overrides(found, identifier, green, exception)
        sha = main_tip(project)
        rows = run_witnesses(project, conn, parent_id, sha, "baseline",
                             copy_files=True)
        lines = [f"baseline at {sha}: " + ", ".join(
            _verdict(row) for row in rows)]
        problems = _baseline_problems(rows, sha, green, exception)
        if problems:
            raise ApprovalRefused([*lines, *problems,
                                   f"story {identifier} stays planned"])
        try:
            with store.transaction(conn):
                store.stories.approve_story(
                    conn, parent_id, revision, "cli",
                    _recorded_note(note, green, exception))
                released = _release(conn, board, project_id, parent_id,
                                    identifier)
        except ValueError as refused:
            raise ApprovalRefused([*lines, *getattr(
                refused, "problems", [str(refused)])]) from None
    column = "Ready" if getattr(board, "native", False) else \
        f"{TODO_STATE}, queued for the board"
    return [*lines, f"approved story {identifier} at revision {revision}",
            *(f"released {child} to {column}" for child in released)]


def _planned(conn, project_id, identifier, revision):
    row = conn.execute("SELECT id, revision FROM tickets WHERE projectId = ?"
                       " AND linearIdentifier = ?",
                       (project_id, identifier)).fetchone()
    found = row and store.stories.story(conn, row[0])
    if not found or found.ticketId != row[0]:
        raise ApprovalRefused([f"{identifier} is not a story's parent in this"
                               " project"])
    if found.state != "planned":
        raise ApprovalRefused([f"story {identifier} is {found.state}, not"
                               " planned"])
    if row[1] != revision:
        raise ApprovalRefused([f"{identifier} is at revision {row[1]}, not"
                               f" {revision}; nothing changed"])
    other = conn.execute(
        "SELECT t.linearIdentifier, s.state FROM stories s"
        " JOIN tickets t ON t.id = s.ticketId WHERE t.projectId = ?"
        " AND s.ticketId != ? AND s.state IN (?, ?) ORDER BY t.id LIMIT 1",
        (project_id, row[0], *store.stories.OPEN_STATES)).fetchone()
    if other is not None:
        raise ApprovalRefused([f"story {other[0]} of this project is"
                               f" {other[1]}; one story is open at a time"])
    return row[0], found


def _check_overrides(found, identifier, green, exception):
    keys = {witness.key for witness in found.witnesses}
    for key in (*green, *exception):
        if key not in keys:
            raise ApprovalRefused([f"story {identifier} has no witness {key};"
                                   f" its witnesses are {', '.join(sorted(keys))}"])


def _verdict(row):
    if row.verdict == "red":
        return f"{row.witnessKey} red ({row.redKind})"
    return f"{row.witnessKey} {row.verdict}"


def _baseline_problems(rows, sha, green, exception):
    problems = []
    for row in rows:
        key = row.witnessKey
        if row.verdict == "green" and key not in green:
            problems.append(f"{key} is green at {sha}: it witnesses nothing"
                            f" new; --baseline-green {key} approves it anyway")
        elif row.verdict == "red" and row.redKind == "exception" \
                and key not in exception:
            problems.append(f"{key} is red by exception at {sha}, not by an"
                            " assertion; --baseline-red-kind exception"
                            f" {key} approves it anyway")
        elif row.verdict not in ("green", "red"):
            problems.append(f"{key} is {row.verdict} at {sha}; see"
                            f" {row.evidencePath or 'the ledger'}")
    return problems


def _recorded_note(note, green, exception):
    overrides = [*(f"{key} green" for key in green),
                 *(f"{key} red by exception" for key in exception)]
    if not overrides:
        return note
    return f"{note} (baseline overrides: {', '.join(overrides)})"


def _release(conn, board, project_id, parent_id, identifier):
    children = conn.execute(
        "SELECT id, linearIdentifier, revision, boardColumn FROM tickets"
        " WHERE id IN (SELECT ticketId FROM storyChildren WHERE storyId = ?)"
        " AND status NOT IN (?, ?) AND COALESCE(boardColumn, '') != 'canceled'"
        " ORDER BY id", (parent_id, *CLOSED_STATUSES)).fetchall()
    released = []
    for ticket_id, child, revision, column in children:
        if not getattr(board, "native", False):
            if store.record_push(conn, ticket_id, TODO_STATE):
                released.append(child)
        elif column != "ready":
            store.board.move_ticket(
                conn, project_id, child, "ready", revision,
                note=f"Released to Ready by story {identifier}'s approval.")
            released.append(child)
    return released
