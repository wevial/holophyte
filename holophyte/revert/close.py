import store
import store.board
import store.read
from holophyte.loop.runs import warn_on_run
from holophyte.redact import safe_print as print

BACKLOG = "Backlog"


def close_reverted(conn, run_id, provider):
    if conn is None or run_id is None:
        return None
    try:
        ticket_id = store.board.reopen_reverted(conn, run_id)
        if ticket_id is None:
            return None
        ticket = store.read.ticket_by_id(conn, ticket_id)
        print(f"[holo2] {ticket.linearIdentifier}: reverted; back in Backlog")
        _push_backlog(ticket, provider)
    except Exception as e:  # noqa: BLE001 - the revert has merged regardless
        warn_on_run(conn, run_id, f"could not return the ticket this run"
                                  f" reverts to Backlog: {e}")
        return None
    return ticket.linearIdentifier


def _push_backlog(ticket, provider):
    if provider is None or getattr(provider, "native", False) is True:
        return
    provider.set_state(ticket.linearIssueId, BACKLOG)
