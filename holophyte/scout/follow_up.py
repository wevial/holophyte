import json
from time import monotonic
from typing import NamedTuple

import store
from holophyte.redact import safe_print as print
from holophyte.scout.answer import labels, report_text, stored
from holophyte.scout.lead import (
    MALFORMED,
    NO_REPORT,
    PLAN_SHAPE,
    REPORT_TURN_S,
    Lead,
    asked,
    checked_report,
    fenced_json,
    lead_seat,
    offered_rows,
    parked_subject,
    report_goal,
    run_waves,
    scout_tasks,
    stamped,
    wave_tasks,
)
from holophyte.scout.proposals import (
    card_line,
    post_cards,
    proposals_goal,
    read_proposals,
)
from holophyte.scout.report import resolve_evidence
from holophyte.scout.run_helpers import reproduced
from holophyte.scout.scout_config import scout_config
from holophyte.scout.swarm import STOPPED, Swarm
from store.enums import ScoutTaskPhase
from store.scout_thread import messages
from store.scouts import (
    DONE,
    PASS_STARTED,
    PLANNING,
    SWARMING,
    SYNTHESIZING,
    ScoutRefused,
    end_scout_task,
    move_scout,
    record_scout_actions,
    record_scout_event,
    record_scout_report,
    scout,
)

PASS_OPENING = "Scout pass plan turn"
PASS_DONE = "scout_pass_done"
PASS_FAILED = "scout_pass_failed"
PASS_STOPPED = "scout_pass_stopped"
RUNNING = ScoutTaskPhase.RUNNING.value
PASS_PHASES = (PLANNING, SWARMING, SYNTHESIZING)


class PassOutcome(NamedTuple):
    number: int
    message: int
    report: dict | None = None
    failure: str | None = None


def current_pass(conn, scout_id):
    row = conn.execute(
        "SELECT payload FROM runEvents WHERE scoutId = ? AND kind = ?"
        " ORDER BY seq DESC LIMIT 1", (scout_id, PASS_STARTED)).fetchone()
    return None if row is None else json.loads(row[0])


def steer_text(conn, scout_id, message_id):
    thread = messages(conn, scout_id)
    (steer,) = [message for message in thread if message.id == message_id]
    return steer.text, card_line(thread, steer)


def pass_plan_goal(row, steer, card, number, reports, actions, total, fresh):
    return (
        f"{PASS_OPENING}\n\n"
        + (f"{asked(row)}\n\n" if fresh else "")
        + "Your scout has finished, and a person steers it to look further:"
        f"\n\nSteer: {steer}\n\n{card}"
        f"This is pass {number}. You may read and search the checkout, but "
        "do not edit files, commit or push.\n\n"
        "The scout's reports so far, oldest pass first, findings labelled "
        "F1, F2 and on across passes and action items A with their "
        f"position:\n\n{report_text(reports, actions)}\n\n"
        f"Plan this pass as at most {total} sub-tasks, run in one wave. Each "
        "is one question a helper answers by reading the code (mode "
        "\"read\") or by running commands in its own checkout (mode "
        "\"run\"), with the repository-relative paths it should start from. "
        "End your answer with one fenced json block in this shape, TEXT and "
        "PATH standing for strings:\n\n"
        f"```json\n{PLAN_SHAPE}\n```\n")


def pass_scope(steer, number):
    return (f"This is pass {number}, steered by: {steer}\n"
            "Report only what this pass found: the earlier passes' findings "
            "and action items stay as they are, so do not repeat them.\n"
            + proposals_goal() + "\n")


def run_pass(conn, project, row, steered, worktree, budget_s, stopping):
    number, message = steered["pass"], steered["message"]
    config = scout_config(project)
    seat, down = lead_seat(project, row, config, stopping)
    if down is not None:
        return PassOutcome(number, message, failure=down)
    steer, card = steer_text(conn, row.id, message)
    lead = Lead(conn, project, row, seat, worktree, stopping,
                config.lead_prices.get(row.lead_route or config.lead))
    lead.session = row.lead_session
    deadline = monotonic() + budget_s
    rows = offered_rows(conn, row)
    parked = parked_subject(conn, row)
    goal = pass_plan_goal(row, steer, card, number, *stored(conn, row.id),
                          config.steer_helpers, lead.session is None)
    turn = (lead.resume(goal, budget_s) if lead.session
            else lead.start(goal, budget_s))
    with Swarm(conn, project, row, config, deadline, stopping, number,
               config.steer_helpers) as swarm:
        tasks, problem = wave_tasks(conn, swarm, turn, 1, "plan")
        run_waves(conn, lead, swarm, tasks, 1)
        turn = lead.resume(report_goal(row, swarm, problem,
                                       lead.session is None,
                                       pass_scope(steer, number),
                                       digest=rows, parked=parked),
                           REPORT_TURN_S)
        report = fenced_json(turn.answer)
        if not isinstance(report, dict):
            return PassOutcome(number, message, failure=NO_REPORT)
        report, problems = checked_report(lead, report,
                                          tasks=scout_tasks(conn, row.id),
                                          digest=rows, parked=parked)
        if problems:
            return PassOutcome(number, message,
                               failure=f"{MALFORMED}: {problems[0]}")
        report = reproduced(row, swarm, resolve_evidence(project.path,
                                                         row.base_sha, report))
        return PassOutcome(number, message, dict(report, cut=swarm.cut()))


def held_pass(conn, scout_id, steered):
    current, phase = current_pass(conn, scout_id), scout(conn, scout_id).phase
    if current is None or current["message"] != steered["message"]:
        raise ScoutRefused(
            scout_id, phase,
            f"it runs the pass message {current and current['message']}"
            f" steered, not message {steered['message']}'s")
    if phase not in PASS_PHASES:
        raise ScoutRefused(scout_id, phase,
                           f"pass {steered['pass']} has already ended")


def stop_running(conn, scout_id, number):
    for (task_id,) in conn.execute(
            "SELECT id FROM scoutTasks WHERE scoutId = ? AND pass = ?"
            " AND phase = ? ORDER BY id", (scout_id, number, RUNNING)
    ).fetchall():
        end_scout_task(conn, task_id, STOPPED)


def end_pass(conn, scout_id, steered, kind, summary, payload):
    number = steered["pass"]
    with store.transaction(conn):
        held_pass(conn, scout_id, steered)
        stop_running(conn, scout_id, number)
        record_scout_event(conn, scout_id, kind, summary, level="detail",
                           payload=json.dumps({"pass": number,
                                               "message": steered["message"],
                                               **payload}))
        move_scout(conn, scout_id, DONE)


def fail_pass(conn, scout_id, steered, reason):
    end_pass(conn, scout_id, steered, PASS_FAILED,
             f"pass {steered['pass']} failed: {reason}", {"reason": reason})


def stop_pass(conn, scout_id, steered, reason, author, note):
    end_pass(conn, scout_id, steered, PASS_STOPPED,
             f"pass {steered['pass']} stopped: {reason}",
             {"reason": reason, "author": author, "note": note})


def settle_pass(conn, scout_id, outcome):
    number = outcome.number
    steered = {"pass": number, "message": outcome.message}
    if outcome.failure is not None:
        fail_pass(conn, scout_id, steered, outcome.failure)
        print(f"[holo2] scout {scout_id} pass {number} failed:"
              f" {outcome.failure}", flush=True)
        return 1
    with store.transaction(conn):
        held_pass(conn, scout_id, steered)
        report = stamped(conn, scout(conn, scout_id), outcome.report)
        record_scout_report(conn, scout_id, number, report)
        positions = record_scout_actions(conn, scout_id, number,
                                         report["actions"])
        found = [label for label, block in labels(*stored(conn, scout_id))
                 .items() if block["type"] == "finding"
                 and block["pass"] == number]
        proposals, dropped = read_proposals(outcome.report)
        cards = post_cards(conn, scout_id, outcome.message, proposals)
        record_scout_event(
            conn, scout_id, PASS_DONE, f"pass {number} done", level="detail",
            payload=json.dumps({"pass": number, "message": outcome.message,
                                "findings": found, "actions": positions,
                                "cards": cards,
                                "dropped_proposals": dropped}))
        move_scout(conn, scout_id, DONE)
    print(f"[holo2] scout {scout_id} pass {number} done", flush=True)
    return 0
