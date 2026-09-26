"""holophyte.board_diff: `--board-diff`, the store's copy of the ready
queue held against the board's (KO-738).

Phase 3 flips a project to store mode only when the two agree, and this
is the command that says whether they do. It lists the board once, opens
the store read-only, and prints one line per difference in a board-owned
field (title, body, priority, labels, column), one per listed issue the
store has no row for, and one per store row `ready` for claiming that the
listing no longer names, then a summary line. It repairs nothing: the
next mirror pass does that.
"""
import json
import sys

import store.read
from holophyte.board import board_owned_labels, mirror_key
from holophyte.claim_store import store_mode

# The board-owned fields a task carries, named alike in the task dict and
# `tickets`; the column is compared separately, since a task does not carry it.
TASK_FIELDS = ("title", "body", "priority", "labels")


def board_diff(target, board, out=None):
    """Print every difference between `board`'s ready listing and the
    store's rows for it; return 0 when there are none and 1 otherwise.

    The project row is the one whose `linearTeamId` is the board's `team`,
    as the supervisor's board fallback finds it, and each task is matched
    to its row by `mirror_key()`. A field is compared only when the task
    carries it (a file-board task has no `priority`); labels are compared
    as `board_owned_labels()`, and every listed task's column is expected
    to be `ready`. A `ready` row with no live run that the listing does not
    name is one the board took out of the queue behind the store's back.
    """
    out = out or sys.stdout
    if not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return 1
    tasks = board.ready_issues()
    conn = store.read.open_readonly(target.store_path)
    try:
        lines = diff_lines(conn, board.team, tasks,
                           store_mode=store_mode(target))
    finally:
        conn.close()
    for line in lines:
        print(line, file=out)
    count = len(lines)
    print("[holo2] board diff: " + (
        "no differences" if not count else
        "1 difference" if count == 1 else f"{count} differences"), file=out)
    return 1 if lines else 0


def diff_lines(conn, team, tasks, store_mode=False):
    """The difference lines for `tasks`, the board's ready listing, against
    the store's rows under `team`'s project. In store mode (Phase 3 stage
    3) a `ready` row the listing does not name is a difference only while
    its column is `ready`: a ticket moved to Backlog leaves the queue by
    its column, and the store already says so."""
    row = conn.execute("SELECT id FROM projects WHERE linearTeamId = ?",
                       (team,)).fetchone()
    project = row[0] if row is not None else None
    lines, listed = [], set()
    for task in tasks:
        key = mirror_key(task)
        listed.add(key)
        stored = conn.execute(
            "SELECT title, body, priority, labels, boardColumn FROM tickets"
            " WHERE projectId = ? AND linearIssueId = ?",
            (project, key)).fetchone()
        if stored is None:
            lines.append(f"{task['id']}: on the board's ready listing, not in"
                         " the store")
            continue
        lines.extend(field_lines(task, stored))
    column = " AND boardColumn = 'ready'" if store_mode else ""
    for key, identifier in conn.execute(
            "SELECT linearIssueId, linearIdentifier FROM tickets"
            " WHERE projectId = ? AND status = 'ready' AND activeRunId IS NULL"
            + column + " ORDER BY linearIdentifier", (project,)):
        if key not in listed:
            lines.append(f"{identifier}: ready in the store, not on the"
                         " board's ready listing")
    return lines


def field_lines(task, stored):
    """One line per board-owned field `task` carries that its stored row,
    `(title, body, priority, labels, boardColumn)`, holds otherwise."""
    lines = []
    for index, name in enumerate(TASK_FIELDS):
        board_value = task.get(name)
        if board_value is None:
            continue
        store_value = stored[index]
        if name == "labels":
            board_value = board_owned_labels(board_value)
            store_value = json.loads(store_value or "[]")
        if board_value == store_value:
            continue
        if name == "body":
            lines.append(f"{task['id']}: {name} differs")
        else:
            lines.append(f"{task['id']}: {name} differs (store"
                         f" {store_value!r}, board {board_value!r})")
    if stored[4] != "ready":
        lines.append(f"{task['id']}: boardColumn differs (store"
                     f" {stored[4]!r}, board 'ready')")
    return lines
