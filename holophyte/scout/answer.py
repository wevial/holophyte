import json
from functools import partial
from typing import NamedTuple

import store
from holophyte.agents.harness import SCOUT_LEAD
from holophyte.agents.probes import PROBE_TIMEOUT
from holophyte.redact import safe_print as print
from holophyte.scout.lead import Lead, asked, fenced_json
from holophyte.scout.proposals import (
    card_line,
    post_cards,
    proposals_goal,
    read_proposals,
)
from holophyte.scout.scout_config import scout_config
from holophyte.scout.scout_probes import in_critic_workspace, probe_scout_route
from store.scout_thread import ANSWER, ANSWER_STARTED, messages, post_message
from store.scouts import (
    ANSWERING,
    DONE,
    ScoutRefused,
    move_scout,
    record_scout_event,
    scout,
)

ANSWER_TURN_S = 300
ANSWER_OPENING = "Scout answer turn"
ANSWER_SHAPE = '{"answer": "...", "refs": ["F2", "A1"]}'
NO_ANSWER = "the lead's answer turn returned no JSON object"
EMPTY_ANSWER = "the lead's answer is empty"
ANSWERED = "scout_answered"
ANSWER_FAILED = "scout_answer_failed"
ANSWER_STOPPED = "scout_answer_stopped"


class Outcome(NamedTuple):
    ask: int | None
    text: str | None = None
    refs: tuple = ()
    failure: str | None = None
    proposals: tuple = ()
    dropped: tuple = ()


def labels(reports, actions):
    """Maps F1, F2... across passes in pass order, and A plus each item's
    position, to the labelled item's thread block."""
    labelled, number = {}, 0
    for pass_number, report in reports:
        for index in range(1, len(report["findings"]) + 1):
            number += 1
            labelled[f"F{number}"] = {"type": "finding", "pass": pass_number,
                                      "index": index}
    for position, _ in actions:
        labelled[f"A{position}"] = {"type": "action", "position": position}
    return labelled


def stored(conn, scout_id):
    reports = conn.execute("SELECT pass, report FROM scoutReports"
                           " WHERE scoutId = ? ORDER BY pass", (scout_id,))
    actions = conn.execute("SELECT position, item FROM scoutActions"
                           " WHERE scoutId = ? ORDER BY position", (scout_id,))
    return ([(number, json.loads(report)) for number, report in reports],
            [(position, json.loads(item)) for position, item in actions])


def current_ask(conn, scout_id):
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE scoutId = ? AND kind = ?"
        " ORDER BY seq DESC LIMIT 1", (scout_id, ANSWER_STARTED)).fetchone()
    return None if row is None else json.loads(row[0])["message"]


def taken_ask(conn, scout_id, ask):
    if ask is None:
        raise RuntimeError(f"scout {scout_id} has taken no ask")
    thread = messages(conn, scout_id)
    (index,) = [index for index, message in enumerate(thread)
                if message.id == ask]
    return thread[index], thread[:index]


def report_text(reports, actions):
    labelled = iter(labels(reports, actions))
    parts = []
    for pass_number, report in reports:
        parts.append(f"## Pass {pass_number}\n\n{report['summary']}\n")
        parts += [f"{next(labelled)} {finding['title']}\n"
                  f"  {json.dumps(finding)}" for finding in report["findings"]]
        rest = {key: value for key, value in report.items()
                if key not in ("summary", "findings", "actions")}
        if rest:
            parts.append(f"\nThe rest of pass {pass_number}'s report:"
                         f" {json.dumps(rest)}")
    parts.append("\n## Action items\n")
    parts += [f"{next(labelled)} {item['title']}\n  {json.dumps(item)}"
              for _, item in actions] or ["none"]
    return "\n".join(parts)


def answer_goal(row, question, earlier, reports, actions, fresh):
    thread = "\n".join(f"- {message.author} ({message.mode}): {message.text}"
                       for message in earlier) or "none"
    return (
        f"{ANSWER_OPENING}\n\n"
        + (f"{asked(row)}\n\n" if fresh else "")
        + "Your scout has finished. A person asks about its report:\n\n"
        f"Question: {question.text}\n\n"
        + card_line(earlier, question) +
        "Answer from what you have already read. You may read and search "
        "the checkout, but do not edit files, commit or push, and start no "
        "helpers or subagents.\n\n"
        "The scout's reports, oldest pass first, findings labelled F1, F2 "
        "and on across passes and action items A with their position:\n\n"
        f"{report_text(reports, actions)}\n\n"
        f"The thread's earlier messages, oldest first:\n{thread}\n\n"
        "End your answer with one fenced json block in this shape, refs "
        "naming the labels your answer rests on:\n\n"
        f"```json\n{ANSWER_SHAPE}\n```\n" + proposals_goal())


def read_answer(ask, turn, cap):
    if turn.timed_out:
        return Outcome(ask, failure=f"the answer turn was cut at its time"
                                    f" limit of {cap}s")
    found = fenced_json(turn.answer)
    if not isinstance(found, dict):
        return Outcome(ask, failure=NO_ANSWER)
    text, refs = found.get("answer"), found.get("refs")
    if not isinstance(text, str) or not text.strip():
        return Outcome(ask, failure=EMPTY_ANSWER)
    refs = refs if isinstance(refs, list) else []
    proposals, dropped = read_proposals(found)
    return Outcome(ask, text, tuple(dict.fromkeys(
        ref for ref in refs if isinstance(ref, str))), None,
        tuple(proposals), tuple(dropped))


def answer(conn, project, row, ask, worktree, stopping, cap=ANSWER_TURN_S):
    question, earlier = taken_ask(conn, row.id, ask)
    config = scout_config(project)
    name = row.lead_route or config.lead
    seat = config.leads.get(name)
    if seat is None:
        return Outcome(question.id, failure=f"lead route {name} is down:"
                                            f" no [scout.leads.{name}]")
    probe = probe_scout_route(project, f"{SCOUT_LEAD}:{name}", seat,
                              PROBE_TIMEOUT,
                              partial(in_critic_workspace,
                                      run=stopping.run_capped))
    if not probe.ok:
        return Outcome(question.id,
                       failure=f"lead route {name} is down: {probe.reason()}")
    lead = Lead(conn, project, row, seat, worktree, stopping,
                config.lead_prices.get(name))
    lead.session = row.lead_session
    reports, actions = stored(conn, row.id)
    goal = answer_goal(row, question, earlier, reports, actions,
                       lead.session is None)
    held(conn, row.id, ask)
    return read_answer(question.id, lead.resume(goal, cap), cap)


def held(conn, scout_id, ask):
    current, phase = current_ask(conn, scout_id), scout(conn, scout_id).phase
    if current != ask:
        raise ScoutRefused(scout_id, phase,
                           f"it answers message {current}, not {ask}")
    if phase != ANSWERING:
        raise ScoutRefused(scout_id, phase,
                           f"its answer to message {ask} has already ended")


def return_done(conn, scout_id, ask, kind, summary, payload):
    with store.transaction(conn):
        held(conn, scout_id, ask)
        record_scout_event(conn, scout_id, kind, summary, level="detail",
                           payload=json.dumps({"message": ask, **payload}))
        move_scout(conn, scout_id, DONE)


def fail_answer(conn, scout_id, ask, reason):
    return_done(conn, scout_id, ask, ANSWER_FAILED, f"answer failed: {reason}",
                {"reason": reason})


def stop_answer(conn, scout_id, ask, reason, author, note):
    return_done(conn, scout_id, ask, ANSWER_STOPPED,
                f"answer stopped: {reason}",
                {"reason": reason, "author": author, "note": note})


def settle(conn, scout_id, outcome):
    if outcome.failure is not None:
        fail_answer(conn, scout_id, outcome.ask, outcome.failure)
        print(f"[holo2] scout {scout_id} answer failed: {outcome.failure}",
              flush=True)
        return 1
    labelled = labels(*stored(conn, scout_id))
    unknown = [ref for ref in outcome.refs if ref not in labelled]
    summary = f"answered message {outcome.ask}" + (
        f"; unknown labels: {', '.join(unknown)}" if unknown else "")
    with store.transaction(conn):
        held(conn, scout_id, outcome.ask)
        posted = post_message(conn, scout_id, "scout", ANSWER, outcome.text,
                              blocks=[labelled[ref] for ref in outcome.refs
                                      if ref in labelled],
                              answers_id=outcome.ask)
        cards = post_cards(conn, scout_id, outcome.ask, outcome.proposals)
        record_scout_event(conn, scout_id, ANSWERED, summary, level="detail",
                           payload=json.dumps({
                               "message": outcome.ask, "answer": posted,
                               "unknown_refs": unknown, "cards": cards,
                               "dropped_proposals": outcome.dropped}))
        move_scout(conn, scout_id, DONE)
    print(f"[holo2] scout {scout_id} {summary}", flush=True)
    return 0
