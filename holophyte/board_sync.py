"""holophyte.board_sync: a store-mode project's rows following the board.

In store mode the store is where tickets are claimed from, so its rows
must follow the board's columns and survive the board losing an issue
(KO-739). `observe_board()` is the host sweep's step: once per
`board_ask_sec` it asks the board's `states()` about every open ticket of
the project, live ones included, and records what it heard. An open or
canceled answer writes the state name and the column (a column change is
a new revision authored `board`); a completed one writes the name, and
walking a closed ticket stays `_reconcile_mirror()`'s. A gone answer
stamps `goneSince`; a second one at least `board_ask_sec` later retires
the ticket -- an idle one walked `abandoned` under a `reconcile` row, a
live run asked to pause on the question. Seeing the issue again clears the
stamp. A board that cannot be asked is no evidence and changes nothing.

It is also the one sender of a store-mode status push (KO-740), which
`mirror_push()` queues on the ticket rather than sends. Each answer `S`
settles the queued push by three rows: `S` the wanted state is landed, and
the push is cleared; `S` the state it was queued from is not landed, and it
is sent again while the ticket's status still maps to it; any other `S` is
a person's move, already recorded above, and the push is dropped so the
move stands. A push queued before the row was ever observed is sent from
`S`, unless `S` is already the wanted state. A gone answer leaves the push
alone, and a closed ticket is still asked while it has one queued. The
sends are made after each row's transaction commits; a raise is one
printed line and the push waits for the next ask.
"""
from datetime import datetime, timezone

import store
import store.read
import store.tickets
from holophyte import deadline
from holophyte.board import MIRROR_STATES
from holophyte.config_tables import board_mode
from provider import GONE


def observe_board(target, conn, project, board, now, out, asked, ask_ms):
    """Ask `board` the state of each of `project`'s open tickets and record
    the answers; nothing unless `target`'s `[board] mode` is `store`, a
    board is given and the project was not asked within `ask_ms`. `asked`
    is the store's `ReconcileMemory.states_asked`, by project id. A raise
    from the board is one printed line to `out` and no write."""
    if board is None or board_mode(target).mode != "store":
        return
    asked_at = asked.get(project)
    if asked_at is not None and now - asked_at < ask_ms:
        return
    tickets = [*store.read.open_tickets(conn, project),
               *_closed_with_push(conn, project)]
    if not tickets:
        return
    deadline.check("the board's ticket states")
    previous = asked.get(project)
    asked[project] = now
    try:
        answers = board.states([t.linearIdentifier for t in tickets])
    except Exception as e:  # noqa: BLE001 - no evidence, never the pass
        print(f"[holo2] the board could not be asked its tickets' states"
              f" ({e}); a later pass asks again", file=out)
        return
    finally:
        # A sweep deadline cut the ask short: the next pass asks again.
        if deadline.spent() and previous is None:
            del asked[project]
        elif deadline.spent():
            asked[project] = previous
    sends = []
    for ticket in tickets:
        answer = answers.get(ticket.linearIdentifier)
        if answer is not None:
            send = _record(conn, ticket, answer, now, out, ask_ms)
            if send is not None:
                sends.append((ticket.linearIdentifier, *send))
    for identifier, issue_id, state in sends:
        try:
            board.set_state(issue_id, state)
        except Exception as e:  # noqa: BLE001 - the push waits, never the pass
            print(f"[holo2] the queued push of {identifier} to {state}"
                  f" failed ({e}); it waits for the next ask", file=out)


def _closed_with_push(conn, project):
    """The project's closed tickets that still have a push queued: a merge
    or abandonment is pushed like any other status."""
    return [store.read.ticket_by_id(conn, ticket_id) for (ticket_id,) in
            conn.execute("SELECT id FROM tickets WHERE projectId = ? AND"
                         " status IN ('merged', 'abandoned') AND pushState"
                         " IS NOT NULL ORDER BY linearIdentifier", (project,))]


def _record(conn, ticket, answer, now, out, ask_ms):
    """One ticket's answer, written under one transaction that re-reads
    the row; a row closed since the open read is left alone but for its
    queued push. Answer the push to send, as `(issue id, state)`, or None."""
    with store.transaction(conn):
        row = conn.execute(
            "SELECT status, activeRunId, lastRunId, goneSince, pushState"
            " FROM tickets WHERE id = ?", (ticket.id,)).fetchone()
        if row is None:
            return None
        closed = row[0] in ("merged", "abandoned")
        if answer["state"] == GONE:
            if not closed:
                _gone(conn, ticket, row[:4], now, out, ask_ms)
            return None
        if closed and row[4] is None:
            return None
        store.set_board_state(conn, ticket.id, answer["name"],
                              column=answer["column"])
        if not closed:
            store.set_gone_since(conn, ticket.id, None)
            if answer["state"] != "completed":
                store.record_board_fields(conn, ticket.id, author="board",
                                          now=now)
        return _settle(conn, ticket.id, answer["name"], now)


def _settle(conn, ticket_id, seen, now):
    """The three-row rule on the ticket's queued push, the board having
    answered `seen`; the push to send, as `(issue id, state)`, or None."""
    ticket = store.read.ticket_by_id(conn, ticket_id)
    wanted = ticket.pushState
    if wanted is None:
        return None
    if seen == wanted or (ticket.pushFrom is not None
                          and seen != ticket.pushFrom):
        # Landed, or a person's move: either way nothing is left to send.
        store.clear_push(conn, ticket_id)
        return None
    if MIRROR_STATES.get(ticket.status) != wanted:
        # The status has moved on to one that does not push this state.
        store.clear_push(conn, ticket_id)
        return None
    if ticket.pushFrom is None:
        # Never observed when queued: `seen` is the state it is sent from.
        store.record_push(conn, ticket_id, wanted, now)
    return ticket.linearIssueId, wanted


def _gone(conn, ticket, row, now, out, ask_ms):
    """A gone answer: stamp the first sighting, retire on a later one."""
    if row[3] is None:
        store.set_gone_since(conn, ticket.id, now)
    # A ticket parked on its pull request or a question is left as it
    # is: the person it waits on decides it.
    elif now - row[3] >= ask_ms and row[0] != "blocked_on_operator":
        _retire(conn, ticket, row, now, out)


def _retire(conn, ticket, row, now, out):
    """The second gone sighting: pause a live run on the question, or walk
    an idle ticket `abandoned`, the `reconcile` row recorded first."""
    status, active_run, last_run, gone_since = row
    identifier = ticket.linearIdentifier
    question = f"the board no longer has {identifier}"
    if active_run is not None:
        store.pause(conn, active_run, question, source="supervisor", now=now)
        print(f"[holo2] asked run {active_run} to pause: {question}",
              file=out)
        return
    line = (f"[holo2] reconciled {identifier}: {status} -> abandoned"
            f" ({question})")
    if last_run is not None:
        store.record_intervention(
            conn, last_run, "reconcile",
            f"{question}: seen gone at {_utc(gone_since)} and again at"
            f" {_utc(now)}; walked {status} -> abandoned",
            source="supervisor", trigger="manual", now=now)
    else:
        line += "; no run to record the intervention against"
    store.tickets.walk_ticket(conn, ticket.id, "abandoned")
    print(line, file=out)


def _utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
