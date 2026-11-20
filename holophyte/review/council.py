"""A council review round: every seat on one prompt, merged strictest-wins."""
import contextvars
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from functools import partial
from time import monotonic, time

import store.council
from holophyte.agents.agent_turns import family_label
from holophyte.config.reader import adversary_credential
from holophyte.loop.gates import InfraFailure
from holophyte.redact import safe_print as print
from holophyte.review.json_reply import (
    COUNCIL_SCHEMA_FILE,
    REPLY_BRIEF,
    RenderedReply,
    SchemaBrief,
    rendered,
    reply_object,
    retry_brief,
)
from holophyte.review.reply_parsing import cited_approval, criteria_statuses
from store.working import working

QUORUM = 2
SCORE_BRIEF = (" Also give score: your score for the candidate as it stands, an "
               "integer from 0 (not mergeable) to 10 (ready to merge).")
UNREADABLE = "reply failed the council reply schema twice"
RANKS = {"not_met": 0, "unwitnessed": 1, "met": 2}


@dataclass(frozen=True)
class Seat:
    number: int
    route: str
    status: str
    started_at: int
    ended_at: int
    seconds: float
    value: dict | None = None
    reason: str | None = None


def reply_brief(schema_tail):
    text = REPLY_BRIEF + SCORE_BRIEF + schema_tail
    return SchemaBrief(text, text, text)


def seat_route(project, seat):
    credential = adversary_credential(project) if seat.harness == "claude" else None
    return {"harness": seat.harness, "model": seat.model, "effort": seat.effort,
            "credential": credential}


def ask(project, conn, run_id, wt, base_sha, sha, prompt, run_agent, number,
        seat):
    route = seat_route(project, seat)
    started, clock = int(time() * 1000), monotonic()

    def ended(status, value=None, reason=None):
        return Seat(number, family_label(route), status, started,
                    int(time() * 1000), round(monotonic() - clock, 3), value,
                    reason)
    goal = prompt
    for _ in range(2):
        try:
            reply = run_agent(project, "review", goal, wt, base_sha=base_sha,
                              candidate_sha=sha, conn=conn, run_id=run_id,
                              family_route=route)
        except (InfraFailure, OSError, subprocess.SubprocessError) as failure:
            if not getattr(failure.__cause__, "schema_refused", False):
                return ended("missing", reason=str(failure))
            reply = ""
        value = reply_object(str(reply), COUNCIL_SCHEMA_FILE)
        if value is not None:
            return ended("answered", value)
        goal = retry_brief(goal)
    return ended("unreadable", reason=UNREADABLE)


def together(asks):
    results, errors = [None] * len(asks), []

    def run(at, context, turn):
        try:
            results[at] = context.run(turn)
        except BaseException as exc:  # noqa: BLE001 - re-raised after the join
            errors.append(exc)
    threads = [threading.Thread(target=run, daemon=True,
                                args=(at, contextvars.copy_context(), turn),
                                name=f"council-seat-{at + 1}")
               for at, turn in enumerate(asks)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return results


def record(conn, run_id, rnd, seat):
    if conn is None or run_id is None:
        return
    value = seat.value or {}
    store.council.record_seat(
        conn, run_id, rnd, seat.number, seat.route, seat.status,
        started_at=seat.started_at, ended_at=seat.ended_at,
        seconds=seat.seconds, verdict=value.get("verdict"),
        score=value.get("score"), reply=seat.value, reason=seat.reason)


def run_round(project, conn, run_id, wt, base_sha, sha, prompt, rnd, seats,
              criteria, run_agent):
    with working(conn, run_id):
        seated = together([partial(ask, project, conn, run_id, wt, base_sha,
                                   sha, prompt, run_agent, number, seat)
                           for number, seat in enumerate(seats, 1)])
    absent = []
    for seat in seated:
        record(conn, run_id, rnd, seat)
        if seat.status != "answered":
            absent.append(f"seat {seat.number} ({seat.route}) {seat.status}: "
                          f"{seat.reason}")
            print(f"[holo2] round {rnd}: council {absent[-1]}")
    answered = [seat for seat in seated if seat.status == "answered"]
    if len(answered) < QUORUM:
        raise InfraFailure(
            f"council review had {len(answered)} of {len(seated)} seats "
            f"answer, under the quorum of {QUORUM}: {'; '.join(absent)}; "
            f"candidate preserved at {sha}", "review_route")
    value = merged([seat.value for seat in answered], criteria, wt, sha)
    reply = RenderedReply(rendered(value))
    reply.json_reply = value
    reply.council_route = "council " + ", ".join(seat.route
                                                 for seat in answered)
    return reply, value["verdict"], ""


def merged(values, criteria, wt, sha):
    judged = merged_criteria(values, criteria, wt, sha)
    approve = (all(value["verdict"] == "APPROVE" for value in values)
               and all(entry["status"] == "met" for entry in judged))
    return {"verdict": "APPROVE" if approve else "REQUEST_CHANGES",
            "criteria": judged,
            "findings": union(values, "findings", lambda finding: (
                finding["path"], finding["line"], finding["severity"],
                finding["message"])),
            "follow_ups": union(values, "follow_ups"),
            "scope": merged_scope(values),
            "refuted": union(values, "refuted")}


def union(values, field, key=None):
    seen, kept = set(), []
    for value in values:
        for item in value[field]:
            mark = key(item) if key else json.dumps(item, sort_keys=True)
            if mark not in seen:
                seen.add(mark)
                kept.append(item)
    return kept


def checked(value, criteria, wt, sha):
    text = rendered(value)
    statuses = criteria_statuses(text, criteria, wt,
                                 approved_range=cited_approval(text, wt, sha))
    given = {f"C{int(number[0])}": entry for entry in value["criteria"]
             if (number := re.search(r"\d+", entry["id"]))}
    kept = []
    for entry in statuses:
        own = given.get(f"C{entry['n']}", {})
        status = entry["status"] if entry["status"] in RANKS else "unwitnessed"
        kept.append((entry["n"], status, own["reason"] if status == "met"
                     else entry["note"], own.get("witness")))
    return kept


def merged_criteria(values, criteria, wt, sha):
    seats = [checked(value, criteria, wt, sha) for value in values]
    kept = []
    for judged in zip(*seats):
        n, status, note, witness = min(
            judged, key=lambda entry: RANKS[entry[1]])
        kept.append({"id": f"C{n}", "status": status, "reason": note,
                     "witness": witness})
    return kept


def merged_scope(values):
    by_path = {}
    for value in values:
        for entry in value["scope"]:
            kept = by_path.get(entry["path"])
            if kept is None or (kept["status"], entry["status"]) == (
                    "needed", "tangent"):
                by_path[entry["path"]] = entry
    return list(by_path.values())
