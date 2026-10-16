"""An acceptance criterion the implementer declares unmeetable as written."""
import json
import re

import store
import store.read
from holophyte.board.projection import block_ticket
from holophyte.loop.gates import MergeParked, sh
from holophyte.redact import safe_print as print

DECLARED_RE = re.compile(
    r"^[ \t>*`_-]*CONTRADICTION:[*`_ \t]*criterion[*`_ \t]*(\d+)[*`_ \t]*"
    r"[—–:-]+[*_ \t]*(.*?)[*_ \t]*$", re.IGNORECASE | re.MULTILINE)
BRIEF = ("\n\nWhen an acceptance criterion cannot be met as written (the data "
         "or contract it names lacks a field, two criteria contradict each "
         "other, or a repository pin or rule forbids it), do not substitute "
         "different behaviour or wording, and do not write a test that "
         "asserts a substitute. Do the rest of the ticket, commit it, and end "
         "the reply with one line per such criterion, numbered as the ticket "
         "lists its acceptance criteria from 1, with a one-line reason naming "
         "what is missing or what contradicts it:\n"
         "CONTRADICTION: criterion N — reason")
ANSWERS = ("Amend the ticket (holo file --update {key} FILE), then holo requeue"
           " {key} -n NOTE; or holo requeue {key} -n NOTE saying how to meet it"
           " as written; or cancel {key} on the board.")


def declared(reply):
    pairs = {}
    for number, reason in DECLARED_RE.findall(str(reply or "")):
        reason = reason.strip()
        if reason.count("`") % 2:
            if reason.startswith("`"):
                reason = reason[1:]
            elif reason.endswith("`"):
                reason = reason[:-1]
        pairs[(int(number), reason)] = None
    return list(pairs)


def park(conn, run_id, provider, task_id, wt, turn, reply, timed_out):
    criteria = declared(reply)
    if timed_out or not criteria:
        return
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    first = "implementer: " + "; ".join(
        f"criterion {number} cannot be met as written: {reason}"
        for number, reason in criteria)
    asked = f"{first}\n{ANSWERS.format(key=task_id)}"
    print(f"[holo2] {first}; parked at {head[:12]} for the operator")
    if conn is None or run_id is None:
        print(f"[holo2] no store to park the run in:\n{asked}")
        raise MergeParked(first)
    store.record_event(conn, run_id, "contradiction", first, level="detail",
                       payload=json.dumps({
                           "turn": turn, "head": head,
                           "criteria": [{"criterion": number, "reason": reason}
                                        for number, reason in criteria]}))
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    if not block_ticket(conn, ticket_id, provider, asked,
                        park_kind="contradiction"):
        print(f"[holo2] {task_id} could not be moved to blocked_on_operator;"
              " parking the run anyway")
    store.park(conn, run_id, "blocked_on_operator", asked, candidate_sha=head,
               park_kind="contradiction")
    raise MergeParked(first)
