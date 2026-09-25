"""store.revisions: one `ticketRevisions` row per change of a ticket's
board-owned fields (KO-736).

The board owns a ticket's title, body, priority, labels and column; the
store keeps each version of them as a numbered revision, and
`tickets.revision` names the current one. `mirror_ticket()` is the one
writer: it heals a row an older build changed without a revision, writes
the board's fields, then records them. Nothing reads the revisions yet.
"""
from __future__ import annotations

import json
import time

from .schema import _transaction

# The fields a revision holds, in `tickets` and `ticketRevisions` alike.
BOARD_FIELDS = ("title", "body", "priority", "labels", "boardColumn")


def record_board_fields(conn, ticket_id, author="board", now=None):
    """Record ticket `ticket_id`'s board-owned fields as its next revision
    when they differ from its latest one; return the new revision number,
    or None when nothing changed.

    Compares the `tickets` row as it stands with its highest-numbered
    `ticketRevisions` row, so a ticket with none -- inserted by a build
    that wrote no revisions, at `revision` 0 -- records revision 1. Labels
    are compared as lists, not as JSON text. `author` names who made the
    change: `board` for a mirror's write, `unrecorded` for the healing
    pass over a write an older build made without a revision. `now` is
    epoch milliseconds for `at`, defaulting to the clock.

    Runs in one `_transaction()`, so it joins the caller's.
    """
    if now is None:
        now = int(time.time() * 1000)
    columns = ", ".join(BOARD_FIELDS)
    with _transaction(conn):
        current = conn.execute(
            f"SELECT {columns} FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if current is None:
            raise ValueError(f"ticket {ticket_id} does not exist")
        latest = conn.execute(
            f"SELECT revision, {columns} FROM ticketRevisions"
            " WHERE ticketId = ? ORDER BY revision DESC LIMIT 1",
            (ticket_id,),
        ).fetchone()
        if latest is not None and _same(latest[1:], current):
            return None
        revision = 1 if latest is None else latest[0] + 1
        conn.execute(
            "INSERT INTO ticketRevisions (ticketId, revision, at, author,"
            f" {columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, revision, now, author, *current),
        )
        conn.execute("UPDATE tickets SET revision = ? WHERE id = ?",
                     (revision, ticket_id))
    return revision


def _same(recorded, current):
    """Do two `BOARD_FIELDS` tuples hold the same fields?"""
    labels = BOARD_FIELDS.index("labels")
    return all(
        json.loads(a) == json.loads(b) if index == labels else a == b
        for index, (a, b) in enumerate(zip(recorded, current))
    )
