import store
import store.board
import store.read
from holophyte.loop.runs import warn_on_run
from holophyte.redact import safe_print as print

BACKLOG = "Backlog"


def close_reverted(conn, run_id, provider):
    if conn is None:
        return
    mirrored = provider is not None \
        and getattr(provider, "native", False) is not True
    reopened = _warned(conn, run_id, "could not return the ticket this run"
                       " reverts to Backlog, so it still reads merged; move"
                       " it by hand", _reopen, conn, run_id, provider,
                       mirrored)
    if reopened is None:
        return
    ticket, note_id, note = reopened
    print(f"[holo2] {ticket.linearIdentifier}: reverted; back in Backlog")
    if mirrored and note_id is not None:
        _warned(conn, run_id, f"{ticket.linearIdentifier}: back in Backlog,"
                " its note not posted", _post, conn, provider, ticket,
                note_id, note)


def _reopen(conn, run_id, provider, mirrored):
    with store.transaction(conn):
        reopened = store.board.reopen_reverted(conn, run_id)
        if reopened is None:
            return None
        ticket_id, note_id, note = reopened
        ticket = store.read.ticket_by_id(conn, ticket_id)
        if mirrored:
            provider.set_state(ticket.linearIssueId, BACKLOG)
    return ticket, note_id, note


def _post(conn, provider, ticket, note_id, note):
    provider.comment(ticket.linearIssueId, note)
    store.mark_note_posted(conn, note_id)


def _warned(conn, run_id, failure, step, *args):
    try:
        return step(*args)
    except Exception as e:  # noqa: BLE001 - the revert has merged regardless
        warn_on_run(conn, run_id, f"{failure}: {e}")
        return None
