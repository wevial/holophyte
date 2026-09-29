"""Store-mode rows following the board; the one sender of their pushes and notes."""
import contextlib
from datetime import datetime, timezone

import store
import store.read
import store.tickets
from holophyte import deadline
from holophyte.board.board import MIRROR_STATES
from holophyte.config.config_tables import board_mode
from holophyte.loop.claim_store import store_mode, sync_board
from holophyte.loop.stop import abort_requested, abort_run
from holophyte.review.freshness import BACKLOG_STATE
from provider import GONE

CLOSED_ANSWERS = ("canceled", "completed")


def observe_board(target, conn, project, board, now, out, asked, ask_ms):
    if board is None or board_mode(target).mode != "store":
        return
    # A native board is the store: asking it writes revisions for nothing.
    if getattr(board, "native", False) is True:
        return
    asked_at = asked.get(project)
    if asked_at is not None and now - asked_at < ask_ms:
        _deliver_queued(target, conn, project, board, now, out, ask_ms)
        return
    tickets = [*store.read.open_tickets(conn, project),
               *_closed_to_ask(conn, project)]
    if not tickets:
        return
    deadline.check("the board's ticket states")
    previous = asked.get(project)
    asked[project] = now
    try:
        answers = board.states([t.linearIdentifier for t in tickets])
    except Exception as e:
        print(f"[holo2] the board could not be asked its tickets' states"
              f" ({e}); a later pass asks again", file=out)
        return
    finally:
        # A sweep deadline cut the ask short: the next pass asks again.
        if deadline.spent() and previous is None:
            del asked[project]
        elif deadline.spent():
            asked[project] = previous
    _apply(target, conn, board, tickets, answers, now, out, ask_ms)
    _deliver(conn, project, board, out)


def _deliver_queued(target, conn, project, board, now, out, ask_ms):
    tickets = _pushes_to_settle(conn, project)
    if tickets:
        deadline.check("the board's states of queued pushes")
        try:
            answers = board.states([t.linearIdentifier for t in tickets])
        except Exception as e:
            print(f"[holo2] the board could not be asked the states of"
                  f" queued pushes ({e}); a later pass asks again", file=out)
            return
        _apply(target, conn, board, tickets, answers, now, out, ask_ms)
    _deliver(conn, project, board, out)


def _apply(target, conn, board, tickets, answers, now, out, ask_ms):
    sends = []
    for ticket in tickets:
        answer = answers.get(ticket.linearIdentifier)
        if answer is None:
            continue
        send, run = _record(conn, ticket, answer, now, out, ask_ms)
        if run is not None:
            _abort_canceled(target, conn, board, ticket, run, out)
            send = _rederive(conn, ticket.id, answer, now)
        if send is not None:
            sends.append((ticket.id, ticket.linearIdentifier, *send))
    _send(conn, board, sends, out)


def owed(target, conn, project, provider, now, out, knobs):
    if not store_mode(target):
        return store.read.ready_tickets(conn, project)
    if provider is not None and project == _board_project(conn, provider):
        deadline.check("the board's ready listing")
        with contextlib.redirect_stdout(out):
            sync_board(target, conn, project, provider, now=now,
                       min_interval_ms=knobs.board_ask_ms)
    return [(row.id, row.lastRunId)
            for row in store.read.claimable(conn, project)]


def _board_project(conn, provider):
    row = conn.execute("SELECT id FROM projects WHERE linearTeamId = ?",
                       (provider.team,)).fetchone()
    return row[0] if row is not None else None


def _deliver(conn, project, board, out):
    for note in store.read.pending_notes(conn, project):
        deadline.check(f"the post of note {note.id} on {note.identifier}")
        body = (f"**{_utc(note.at)}**\n\n{note.text}\n\n"
                f"holophyte-note: {note.id}")
        # A response lost after the board kept the comment posts it again; the id
        # line is what makes the duplicate recognisable.
        try:
            board.comment(note.issueId, body)
        except Exception as e:
            store.mark_note_failed(conn, note.id, str(e) or type(e).__name__)
            print(f"[holo2] note {note.id} on {note.identifier} could not be"
                  f" posted ({e}); it waits for the next pass", file=out)
            return
        store.mark_note_posted(conn, note.id)


def _send(conn, board, sends, out):
    for ticket_id, identifier, issue_id, state in sends:
        # A worker may have moved the ticket on since its push was settled.
        current = store.read.ticket_by_id(conn, ticket_id)
        if (current is None or current.pushState != state
                or not _pushes(current.status, state)):
            continue
        try:
            board.set_state(issue_id, state)
        except Exception as e:
            print(f"[holo2] the queued push of {identifier} to {state}"
                  f" failed ({e}); it waits for the next pass", file=out)
            continue
        # A push queued before the next ask is queued from the state just sent.
        store.set_board_state(conn, ticket_id, state)


def _closed_to_ask(conn, project):
    return [store.read.ticket_by_id(conn, ticket_id) for (ticket_id,) in
            conn.execute(
                "SELECT id FROM tickets t WHERE projectId = ? AND status IN"
                " ('merged', 'abandoned') AND (pushState IS NOT NULL OR"
                " (goneSince IS NULL AND EXISTS (SELECT 1 FROM ticketNotes n"
                " WHERE n.ticketId = t.id AND n.postedAt IS NULL)))"
                " ORDER BY linearIdentifier", (project,))]


def _pushes_to_settle(conn, project):
    return [store.read.ticket_by_id(conn, ticket_id) for (ticket_id,) in
            conn.execute(
                "SELECT id FROM tickets WHERE projectId = ? AND pushState"
                " IS NOT NULL AND goneSince IS NULL"
                " ORDER BY linearIdentifier", (project,))]


def _record(conn, ticket, answer, now, out, ask_ms):
    with store.transaction(conn):
        row = conn.execute(
            "SELECT status, activeRunId, lastRunId, goneSince, pushState"
            " FROM tickets WHERE id = ?", (ticket.id,)).fetchone()
        if row is None:
            return None, None
        closed = row[0] in ("merged", "abandoned")
        if answer["state"] == GONE:
            if not closed and _gone(conn, ticket, row[:4], now, out, ask_ms):
                _rederive(conn, ticket.id, answer, now)
            return None, None
        if closed and row[4] is None:
            return None, None
        store.set_board_state(conn, ticket.id, answer["name"],
                              column=answer["column"])
        run = None
        if not closed:
            store.set_gone_since(conn, ticket.id, None)
            if answer["state"] != "completed":
                store.record_board_fields(conn, ticket.id, author="board",
                                          now=now)
            if answer["state"] == "canceled":
                run = _abortable(conn, row[1])
        return _settle(conn, ticket.id, answer, now), run


def _rederive(conn, ticket_id, answer, now):
    with store.transaction(conn):
        ticket = store.read.ticket_by_id(conn, ticket_id)
        if ticket.pushState is None:
            return None
        if (ticket.status in ("merged", "abandoned")
                or answer["state"] in CLOSED_ANSWERS):
            store.clear_push(conn, ticket_id)
            return None
        if answer["state"] == GONE:
            return None
        return _settle(conn, ticket_id, answer, now)


def _settle(conn, ticket_id, answer, now):
    ticket = store.read.ticket_by_id(conn, ticket_id)
    wanted, seen = ticket.pushState, answer["name"]
    if wanted is None:
        return None
    if seen == wanted or (ticket.pushFrom is not None
                          and seen != ticket.pushFrom):
        store.clear_push(conn, ticket_id)
        return None
    if answer["state"] in CLOSED_ANSWERS:
        # Sending from a closed column would reopen a completed or canceled issue.
        store.clear_push(conn, ticket_id)
        return None
    if not _pushes(ticket.status, wanted):
        store.clear_push(conn, ticket_id)
        return None
    if ticket.pushFrom is None:
        store.record_push(conn, ticket_id, wanted, now)
    return ticket.linearIssueId, wanted


def _pushes(status, state):
    return (MIRROR_STATES.get(status) == state
            or (status == "needs_spec" and state == BACKLOG_STATE))


def _gone(conn, ticket, row, now, out, ask_ms):
    if row[3] is None:
        store.set_gone_since(conn, ticket.id, now)
    # A ticket parked on its pull request or a question waits on its person.
    elif now - row[3] >= ask_ms and row[0] != "blocked_on_operator":
        _retire(conn, ticket, row, now, out)
        return True
    return False


def _abortable(conn, run_id):
    if run_id is None or abort_requested(conn, run_id):
        return None
    (phase,) = conn.execute("SELECT phase FROM runs WHERE id = ?",
                            (run_id,)).fetchone()
    # A run parked on its pull request is reconcile's _close_canceled() to end.
    return None if phase in store.PARKED_PHASES else run_id


def _abort_canceled(target, conn, board, ticket, run_id, out):
    note = f"{ticket.linearIdentifier} was canceled on the board"
    try:
        ended = abort_run(target, conn, run_id, note, provider=board,
                          source="supervisor", trigger="linear_cancelled")
    except ValueError as refused:
        print(f"[holo2] run {run_id} could not be aborted: {note}"
              f" ({refused})", file=out)
        return
    when = "ended abandoned" if ended else "ends at its worker's next heartbeat"
    print(f"[holo2] aborted run {run_id}: {note}; it {when}", file=out)


def _retire(conn, ticket, row, now, out):
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
