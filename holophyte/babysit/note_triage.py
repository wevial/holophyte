import re
from dataclasses import replace

import store
from holophyte.agents.roles import agent_route
from holophyte.babysit import devin_notes
from holophyte.babysit.thread_answers import post
from holophyte.babysit.thread_text import COMMENT_HEADER, gist, where
from holophyte.loop.gates import InfraFailure
from holophyte.loop.runs import heartbeat_while

TRIAGE_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]*)?THREAD[ \t]+(\d+)[ \t]*[:.)-][ \t]*(FINDING|NOTE)\b"
    r"[ \t]*(?:[-–—:,]+[ \t]*)?(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
UNPARSED = "the triage gave no verdict for this comment"
NO_REASON = "no reason given"
NOTE_WITHOUT_REASON = "the triage called it a note but gave no reason"


def untriaged(state, merge):
    if merge.bot_threads == "advisory":
        return ()
    return tuple(t for t in state.notes if devin_notes.triage(t) is None)


def triage_notes(target, conn, run_id, beat_s, wt, sha, pull, state, ticket, merge,
                 triaged):
    pending = tuple(t for t in untriaged(state, merge) if t.id not in triaged)
    if pending:
        _triage(target, conn, run_id, beat_s, wt, sha, pull, ticket, pending, triaged)
    findings = tuple(t for t in state.notes if triaged.get(t.id) == "FINDING")
    if not pending and not findings:
        return state
    return replace(state, threads=state.threads + findings,
                   notes=tuple(t for t in state.notes if t not in findings))


def _triage(target, conn, run_id, beat_s, wt, sha, pull, ticket, pending, triaged):
    from holophyte.loop.review_round import agent, sh
    from holophyte.loop.stop import stop_if_requested
    prompt = triage_prompt(pull, ticket, pending)
    with heartbeat_while(conn, run_id, beat_s):
        reply = agent(target, "adjudicate", prompt, wt, conn=conn,
                      base_sha=sh(["git", "merge-base", "main", sha], cwd=wt),
                      candidate_sha=sha, run_id=run_id)
    stop_if_requested(conn, run_id, "merge_gate")
    if getattr(reply, "timed_out", False) or getattr(reply, "exit_code", 0) != 0:
        raise InfraFailure("note triage failed")
    verdicts = parse(reply, len(pending))
    header = COMMENT_HEADER.format(model=agent_route(target, "adjudicate"))
    for n, thread in enumerate(pending, 1):
        kind, reason = verdicts[n]
        line = devin_notes.FINDING_LINE if kind == "FINDING" else devin_notes.NOTE_LINE
        post(target, conn, run_id, beat_s, pull, thread,
             f"{header}\n\n{line}: {reason}", resolve=False)
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "bot_finding",
                               f"{thread.url}: triaged a Devin analysis as"
                               f" {kind.lower()}: {gist(reason)}")
        triaged[thread.id] = kind


def parse(reply, count):
    verdicts = {}
    for m in TRIAGE_LINE_RE.finditer(str(reply or "")):
        n = int(m.group(1))
        kind, reason = m.group(2).upper(), m.group(3)
        if 1 <= n <= count and n not in verdicts:
            verdicts[n] = ((kind, reason or NO_REASON) if reason or kind == "FINDING"
                           else ("FINDING", NOTE_WITHOUT_REASON))
    return {n: verdicts.get(n, ("FINDING", UNPARSED)) for n in range(1, count + 1)}


def triage_prompt(pull, ticket, notes):
    comments = "\n\n".join(
        f"THREAD {n}: {where(t)} by @{t.author} ({t.url}):\n"
        + "\n".join(f"> {line}" for line in
                    devin_notes.MARKER_RE.sub("", t.body).strip().splitlines())
        for n, t in enumerate(notes, 1))
    return (f"Triage the review comments on {pull.url}. Read the checkout and"
            " ticket. Do not modify anything. Do not commit, push, or request"
            " review. GitHub is not reachable from here, so do not fetch it.\n\n"
            "For each comment answer FINDING if it names something in the"
            " candidate that is wrong, or that contradicts the ticket, and that"
            " the diff can change; answer NOTE if it informs and asks for"
            " nothing in the candidate. Answer one line per comment, exactly:\n"
            "THREAD n: FINDING -- reason\nTHREAD n: NOTE -- reason\n\n"
            f"Ticket:\n{ticket}\n\nComments:\n{comments}")
