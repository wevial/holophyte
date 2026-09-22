"""Read-only mention answers and attributed thread replies."""
import store
from holophyte import maintainer_notes, pr, thread_mentions
from holophyte.agents import agent_route
from holophyte.conversation_comments import ASK_REPLY_MARKER, quote_request
from holophyte.gates import InfraFailure
from holophyte.redact import known_secrets, outbound
from holophyte.runs import heartbeat_while
from store.instructions import record_instruction_reply


def answer_asks(target, conn, run_id, provider, task_id, branch, wt, sha,
                beat_s, pull, threads, ticket, reviewed):
    from holophyte import babysitter
    from holophyte.loop import agent, sh
    from holophyte.pullrequest import _park_on_pr
    threads = tuple(thread_mentions.triaged(threads, ticket, target.config()))
    asks = tuple(t for t in threads if not maintainer_notes.is_note(t)
                 and t.classification == "MENTIONED"
                 and t.intent == "ask")
    for thread in asks:
        prompt = (f"Answer the question on {pull.url}. Read the checkout and ticket. "
                  "Do not modify anything. Do not commit, push, or request review. "
                  "Give a direct answer citing files and lines. End with whether "
                  "a change seems warranted, as advice only.\n\n"
                  f"Ticket:\n{ticket}\n\nThread:\n{babysitter.quoted(thread)}"
                  f"\n\nQuestion:\n{thread.request}")
        with heartbeat_while(conn, run_id, beat_s):
            reply = agent(target, "adjudicate", prompt, wt, conn=conn,
                          base_sha=sh(["git", "merge-base", "main", sha], cwd=wt),
                          candidate_sha=sha, run_id=run_id)
        from holophyte.stop import stop_if_requested
        stop_if_requested(conn, run_id, "merge_gate")
        if (getattr(reply, "timed_out", False)
                or getattr(reply, "exit_code", 0) != 0 or not reply.strip()):
            raise InfraFailure("ask adjudicator failed or returned an empty answer")
        header = babysitter.COMMENT_HEADER.format(
            model=agent_route(target, "adjudicate"))
        body = f"{header}\n\n{ASK_REPLY_MARKER}\n{reply}"
        if thread.triage is not None:
            body += "\n\n" + thread_mentions.FIX_HINT
        post(target, conn, run_id, beat_s, pull, thread, body, resolve=True,
             instruction=dict(kind="instruction", path=thread.path or "(no file)",
                              line=thread.line, author=thread.comments[-1].author,
                              request=thread.request, url=thread.url,
                              **({"triage": thread.triage} if thread.triage else {})))
    remaining = tuple(t for t in threads if t not in asks)
    if asks and not remaining:
        why = previous_park_reason(conn, run_id, branch)
        if why is not None:
            _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                        why, (), reviewed=reviewed)
    return remaining


def previous_park_reason(conn, run_id, branch):
    """The phase event survives release, which clears the ticket question."""
    if conn is not None and run_id is not None:
        row = conn.execute(
            "SELECT e.summary FROM runEvents e JOIN runs r ON r.id = e.runId "
            "WHERE r.ticketId = (SELECT ticketId FROM runs WHERE id = ?) "
            "AND e.kind = 'phase_change' "
            "AND e.summary LIKE '% -> awaiting_merge_approval:%' "
            "ORDER BY e.id DESC LIMIT 1", (run_id,)).fetchone()
        if row:
            note = row[0].split(" -> awaiting_merge_approval: ", 1)[1]
            return note.rsplit("; " + branch + " at ", 1)[0]
    return None


def post(target, conn, run_id, beat_s, pull, thread, body, resolve, instruction=None):
    """Reply and optionally resolve a review thread; record each landed call."""
    from holophyte import babysitter
    secrets = known_secrets(target.config())
    body = outbound(body, secrets)
    safe_instruction = (
        {key: outbound(value, secrets) if isinstance(value, str) else value
         for key, value in instruction.items()}
        if instruction is not None else None
    )
    with heartbeat_while(conn, run_id, beat_s):
        if thread.kind == "conversation":
            pr.comment_on_pull(target, pull, outbound(
                f"{quote_request(thread)}\n\n{body}", secrets))
        else:
            pr.reply_thread(target, pull, thread.id, body)
        if thread.classification == "MENTIONED":
            record_instruction_reply(conn, run_id, thread.url,
                                     "asked" if thread.intent == "ask" else "changed",
                                     body, instruction=safe_instruction)
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "pull_request",
                               f"replied on thread {thread.url}:"
                               f" {babysitter.gist(body.splitlines()[-1])}")
        if resolve and thread.kind != "conversation":
            pr.resolve_thread(target, pull, thread.id)
            if conn is not None and run_id is not None:
                store.record_event(conn, run_id, "pull_request",
                                   f"resolved thread {thread.url}")
