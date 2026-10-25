"""An acceptance criterion the implementer declares unmeetable as written."""
import json
import posixpath
import re

import store
import store.criteria
import store.read
import store.steer_notes
from holophyte.board.projection import block_ticket
from holophyte.loop.gates import MergeParked, sh
from holophyte.redact import safe_print as print
from holophyte.review import criterion_ids
from holophyte.tickets import ticket_template
from holophyte.tickets.ticket_criteria import amendment_edits

DECLARED_RE = re.compile(
    r"^[ \t>*`_-]*CONTRADICTION:[*`_ \t]*criterion[*`_ \t]*([Cc]?\d+)[*`_ \t]*"
    r"[—–:-]+[*_ \t]*(.*?)[*_ \t]*$", re.IGNORECASE | re.MULTILINE)
ANSWERS = ("Amend the ticket (holo file --update {key} FILE), then holo requeue"
           " {key} -n NOTE; or holo requeue {key} -n NOTE saying how to meet it"
           " as written; or cancel {key} on the board.")
PLACEHOLDER = "<one-line reason>"
UNFILLED = frozenset({"", "reason", PLACEHOLDER})
PROPOSED_RE = re.compile(r"^[ \t>*`_-]*PROPOSED_AMENDMENT:[*`_ \t]*(.*?)\s*$",
                         re.MULTILINE)
FACTORY = "factory"


def brief(criteria):
    pairs = criterion_ids.numbered(None, None, criteria)
    if not pairs:
        return ""
    listed = "\n".join(f"C{n}. {text}" for n, text in pairs)
    return ("\n\nAcceptance criterion ids:\n" + listed
            + "\n\nWhen an acceptance criterion cannot be met as written (the data "
            "or contract it names lacks a field, two criteria contradict each "
            "other, or a repository pin or rule forbids it), do not substitute "
            "different behaviour or wording, and do not write a test that "
            "asserts a substitute. Do the rest of the ticket, commit it, and end "
            "the reply with one line per such criterion, using its id above, "
            "with a one-line reason naming what is missing or what contradicts it:\n"
            f"CONTRADICTION: criterion C<id> — {PLACEHOLDER}\n"
            "When the criterion can be met by a reworded criterion inside the "
            "ticket's In scope, add on the next line the criterion's full text "
            "as the next run should read it:\n"
            "PROPOSED_AMENDMENT: REPLACE C<id>: <the criterion as it would read>")


def _balanced(text):
    text = text.strip()
    if text.count("`") % 2:
        if text.startswith("`"):
            return text[1:]
        if text.endswith("`"):
            return text[:-1]
    return text


def declared(reply):
    pairs = {}
    for number, reason in DECLARED_RE.findall(str(reply or "")):
        reason = _balanced(reason)
        if reason.lower() in UNFILLED:
            continue
        pairs[(criterion_ids.parse(number), reason)] = None
    return list(pairs)


def proposed(reply):
    edits = {}
    for line in PROPOSED_RE.findall(str(reply or "")):
        try:
            found, _ = amendment_edits(_balanced(line))
        except ValueError:
            continue
        edits.update(dict.fromkeys(found))
    return list(edits)


def _unmatched(numbers, edits):
    ids = [number for _, number, _ in edits]
    for number in numbers:
        if number not in ids:
            return f"C{number} has no proposal"
        if ids.count(number) > 1:
            return f"C{number} has {ids.count(number)} proposals"
    stray = next((n for n in ids if n is not None and n not in numbers), None)
    return None if stray is None else f"C{stray} was not declared"


def _outside(path, scope, replaced):
    if path.startswith(("../", "/")) or path == "..":
        return True
    return path not in replaced and not any(
        path == root or path.startswith(root + "/") for root in scope)


def _paths(texts):
    return {posixpath.normpath(path) for text in texts
            for _, path in ticket_template._prose_paths(text)}


def held(conn, ticket_id, numbers, edits, ticket):
    if unmatched := _unmatched(numbers, edits):
        return unmatched
    if verb := next((verb for verb, _, _ in edits if verb != "REPLACE"), None):
        return f"{'an' if verb == 'ADD' else 'a'} {verb} waits for the operator"
    entries = ticket_template.parse(ticket).in_scope
    scope = _paths(entries)
    texts = {row["criterion"]: row["text"]
             for row in store.criteria.current(conn, ticket_id)}
    for _, number, text in edits:
        replaced = _paths([texts.get(number, "")]) if entries else set()
        path = next((path for path in sorted(_paths([text]))
                     if _outside(path, scope, replaced)), None)
        if path:
            return f"names `{path}`, outside In scope"
    if any(note.author == FACTORY
           for note in store.steer_notes.amendments(conn, ticket_id)):
        return "the ticket already carries a factory amendment"
    return None


def _apply(conn, ticket_id, task_id, edits):
    note = "\n".join(f"REPLACE C{number}: {text}" for _, number, text in edits)
    try:
        with store.transaction(conn):
            store.requeue(conn, ticket_id,
                          f"implementer proposed an amendment to {task_id};"
                          " amended and requeued", source=FACTORY)
            steered = store.steer_notes.steer(conn, ticket_id, note, FACTORY,
                                              source=FACTORY)
    except (store.RequeueRefused, store.steer_notes.SteerRefused) as exc:
        return str(exc) or type(exc).__name__, None
    return None, steered.id


def _amend(conn, run_id, ticket_id, task_id, criteria, reply, ticket):
    edits = proposed(reply)
    if not edits:
        return
    reason = held(conn, ticket_id, {number for number, _ in criteria}, edits,
                  ticket)
    note_id = None
    if reason is None:
        reason, note_id = _apply(conn, ticket_id, task_id, edits)
    summary = (f"factory amended {task_id} and requeued it" if note_id
               else f"proposed amendment held for the operator: {reason}")
    print(f"[holo2] {summary}")
    store.record_event(conn, run_id, "amendment_proposed", summary,
                       level="detail", payload=json.dumps({
                           "edits": [{"verb": verb, "id": number, "text": text}
                                     for verb, number, text in edits],
                           "applied": note_id is not None, "held": reason,
                           "steer_note": note_id}))


def park(conn, run_id, provider, task_id, wt, turn, reply, timed_out,
         ticket=""):
    criteria = declared(reply)
    if timed_out or not criteria:
        return
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    first = "implementer: " + "; ".join(
        f"criterion C{number} cannot be met as written: {reason}"
        for number, reason in criteria)
    asked = f"{first}\n{ANSWERS.format(key=task_id)}"
    print(f"[holo2] {first}; parked at {head[:12]} for the operator")
    if conn is None or run_id is None:
        print(f"[holo2] no store to park the run in:\n{asked}")
        raise MergeParked(first)
    store.record_event(conn, run_id, "contradiction", first, level="detail",
                       payload=json.dumps({
                           "turn": turn, "head": head,
                           "criteria": [{"criterion": number, "id": f"C{number}",
                                        "reason": reason}
                                        for number, reason in criteria]}))
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    if not block_ticket(conn, ticket_id, provider, asked,
                        park_kind="contradiction"):
        print(f"[holo2] {task_id} could not be moved to blocked_on_operator;"
              " parking the run anyway")
    store.park(conn, run_id, "blocked_on_operator", asked, candidate_sha=head,
               park_kind="contradiction")
    _amend(conn, run_id, ticket_id, task_id, criteria, reply, ticket)
    raise MergeParked(first)
