import store.board
import store.read
from holophyte.loop.runs import warn_on_run
from holophyte.redact import safe_print as print

BACKLOG = "Backlog"


def close_reverted(conn, run_id, provider):
    if conn is None:
        return
    try:
        ticket_id = store.board.reopen_reverted(conn, run_id)
        if ticket_id is None:
            return
        ticket = store.read.ticket_by_id(conn, ticket_id)
        print(f"[holo2] {ticket.linearIdentifier}: reverted; back in Backlog")
        if provider is not None \
                and getattr(provider, "native", False) is not True:
            provider.set_state(ticket.linearIssueId, BACKLOG)
    except Exception as e:  # noqa: BLE001 - the revert has merged regardless
        warn_on_run(conn, run_id, f"could not return the ticket this run"
                                  f" reverts to Backlog: {e}")
