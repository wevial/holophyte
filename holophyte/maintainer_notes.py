"""Adapt private store instructions to the babysitter's addressed threads."""
import re
from dataclasses import replace

from holophyte.pr import Thread
from store import operator_notes

PREFIX = "Maintainer's instruction (amends the ticket where they conflict):"


def is_note(thread):
    return thread.author_kind == "maintainer" and thread.id.startswith("operator_note:")


def event_id(thread):
    return int(thread.id.partition(":")[2])


def amended_ticket(conn, run_id, ticket, url):
    amendments = operator_notes.notes(conn, run_id, pr_url=url)
    if not amendments:
        return ticket
    return ticket + "".join(f"\n\n{PREFIX}\noperator_note event {n['event_id']} "
                            f"by {n['author']}:\n{n['note']}" for n in amendments)


def pending_state(conn, run_id, state, url):
    threads = tuple(Thread(f"operator_note:{n['event_id']}", "", None,
                           n["author"], n["note"], "", author_kind="maintainer")
                    for n in operator_notes.notes(
                        conn, run_id, pending=True, pr_url=url))
    return replace(state, threads=state.threads + threads)


def instruction(thread):
    return (f"{PREFIX}\n{thread.body}\n"
            f"Cite operator_note event {event_id(thread)} in the fix commit message.")


def start_fix(conn, run_id, addressed):
    ids = [event_id(t) for _, t, _ in addressed if is_note(t)]
    if ids:
        rnd = conn.execute("SELECT MAX(round) FROM reviewRounds WHERE runId = ?",
                           (run_id,)).fetchone()[0]
        operator_notes.consume(conn, run_id, ids, rnd)


def cite_commits(wt, sha, fixed, addressed, sh):
    """Ensure the final fix commit carries references even if the agent omits them."""
    refs = [f"operator_note event {event_id(t)}" for _, t, _ in addressed if is_note(t)]
    if not refs:
        return fixed
    messages = sh(["git", "log", "--format=%B", f"{sha}..{fixed}"], cwd=wt)
    missing = [ref for ref in refs
               if re.search(rf"{re.escape(ref)}(?!\d)", messages,
                            re.IGNORECASE) is None]
    if missing:
        message = sh(["git", "log", "-1", "--format=%B"], cwd=wt)
        sh(["git", "commit", "--amend", "--allow-empty", "-m",
            message + "\n\n" + "\n".join(missing)], cwd=wt)
        return sh(["git", "rev-parse", "HEAD"], cwd=wt)
    return fixed
