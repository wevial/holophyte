"""Adapt private store instructions to the babysitter's addressed threads."""
import re
from dataclasses import replace

from holophyte.findings import decode_findings
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


# The findings a requeued attempt is shown, in total characters (KO-718).
FINDINGS_CAP = 1500
REQUEUE_PREFIX = "human requeue: "


def requeue_context(conn, run_id):
    """The implement turn's opening after a requeue (KO-718), or "".

    The ticket's run before `run_id`, when it ended `failed` and carries a
    `requeue` intervention: the operator's note (read back from the event
    `record_intervention()` wrote, the only place it is kept), then the
    findings of its last review round unless that round passed, their
    messages capped at `FINDINGS_CAP` characters in total. A findings row
    that does not decode to a list is skipped, so the note still arrives.
    """
    if conn is None or run_id is None:
        return ""
    prev = conn.execute(
        "SELECT p.id FROM runs p JOIN runs r ON r.ticketId = p.ticketId"
        " WHERE r.id = ? AND p.id < r.id AND p.outcome = 'failed'"
        " AND EXISTS (SELECT 1 FROM interventions i WHERE i.runId = p.id"
        " AND i.\"action\" = 'requeue')"
        " AND NOT EXISTS (SELECT 1 FROM runs q WHERE q.ticketId = r.ticketId"
        " AND q.id > p.id AND q.id < r.id)", (run_id,)).fetchone()
    if prev is None:
        return ""
    (prev,) = prev
    event = conn.execute(
        "SELECT summary FROM runEvents WHERE runId = ? AND kind = 'intervention'"
        " AND summary LIKE ? ORDER BY seq DESC LIMIT 1",
        (prev, REQUEUE_PREFIX + "%")).fetchone()
    note = event[0][len(REQUEUE_PREFIX):] if event else ""
    block = (f"Context from the previous attempt (run {prev}):\n"
             f"Operator's requeue note:\n{note}\n")
    last = conn.execute(
        "SELECT round, verdict, findings FROM reviewRounds WHERE runId = ?"
        " ORDER BY round DESC LIMIT 1", (prev,)).fetchone()
    if last is not None and last[1] != "pass":
        messages = "\n".join(f"- {f.get('message', '')}"
                              for f in decode_findings(last[2]) or ()
                              if isinstance(f, dict))
        if messages:
            cut = len(messages) > FINDINGS_CAP
            messages = messages[:FINDINGS_CAP]
            block += (f"Unresolved findings of its review round {last[0]}:\n"
                      f"{messages}\n"
                      + (f"[findings cut to {FINDINGS_CAP:,} characters]\n"
                         if cut else ""))
    return (block + "This is context from the previous attempt, not a change"
            " to the contract: the ticket below stays the contract.\n\n")


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
