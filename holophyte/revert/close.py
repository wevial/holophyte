import store.board
from holophyte.loop.runs import warn_on_run
from holophyte.redact import safe_print as print


def close_reverted(conn, run_id, provider):
    native = getattr(provider, "native", False) is True
    try:
        reopened = store.board.reopen_reverted(conn, run_id, native=native)
    except Exception as e:  # noqa: BLE001 - the revert has merged regardless
        warn_on_run(conn, run_id, f"could not return the ticket this run"
                                  f" reverts to Backlog; move it by hand: {e}")
        return
    if reopened is None:
        return
    key, note = reopened
    if native:
        print(f"[holo2] {key}: reverted; back in Backlog")
        return
    warn_on_run(conn, run_id, f"{key} still reads merged: only a native board"
                              f" is returned to Backlog by a revert; move it"
                              f" by hand with the note: {note}")
