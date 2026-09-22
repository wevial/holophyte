"""Route advisory review-bot threads before PR fix rounds."""
from dataclasses import replace

import store
from holophyte import maintainer_notes
from holophyte.agents import agent_route
from holophyte.thread_mentions import classify, refuse, refused


def route_bot_threads(target, conn, run_id, beat_s, pull, state, merge):
    """Note advisory bot findings; human follow-ups make them human threads."""
    if merge.mention_accounts and not (state.merged or state.closed):
        for thread in state.threads:
            if maintainer_notes.is_note(thread):
                continue
            if refused(thread, merge.mention_handle, merge.mention_accounts):
                refuse(target, pull, thread, conn, run_id, beat_s)
    if merge.bot_threads != "advisory" or state.merged or state.closed:
        return state
    from holophyte.babysitter import COMMENT_HEADER, _post

    threads = []
    for thread in state.threads:
        mentioned = classify(thread, merge.mention_handle, merge.mention_accounts)
        mentioned = mentioned.classification == "MENTIONED"
        if maintainer_notes.is_note(thread) or mentioned:
            threads.append(thread)
            continue
        is_bot = thread.author_kind == "bot" or thread.author in merge.bot_logins
        human_reply = any(
            c.author_kind != "bot" and c.author not in merge.bot_logins
            and not c.body.startswith("---- Comment by ")
            for c in thread.replies)
        if not is_bot:
            threads.append(thread)
        elif human_reply:
            threads.append(replace(thread, author_kind="user"))
        else:
            body = (COMMENT_HEADER.format(model=agent_route(target, "adjudicate"))
                    + "\nNoted as advisory for the maintainer;"
                    " not acted on by the factory.")
            _post(target, conn, run_id, beat_s, pull, thread, body, resolve=True)
            first_line = thread.body.splitlines()[0] if thread.body else ""
            if conn is not None and run_id is not None:
                store.record_event(conn, run_id, "bot_finding",
                                   f"{thread.url}: {first_line}")
    return replace(state, threads=tuple(threads))
