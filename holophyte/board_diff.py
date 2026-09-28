"""`--board-diff`: the board's ready listing against the store's; it repairs nothing."""
import json
import sys

import store.read
from holophyte.board import board_owned_labels, mirror_key
from holophyte.claim_store import store_mode

TASK_FIELDS = ("title", "body", "priority", "labels")


def board_diff(target, board, out=None):
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
    # In store mode a row moved off the ready column has left the queue by it.
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
