"""A council review round: every seat on one prompt, merged strictest-wins."""
import contextvars
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from functools import partial
from time import monotonic, time

import store
import store.council
from holophyte.agents.agent_routes import routes
from holophyte.agents.agent_turns import argv_label, family_label
from holophyte.babysit.description_review import (
    hold_for_pr_text,
    without_description,
)
from holophyte.config.agent_settings import agent_command, review_route
from holophyte.config.config_tables import merge_config
from holophyte.config.reader import adversary_credential
from holophyte.loop.gates import InfraFailure
from holophyte.redact import redact_document
from holophyte.redact import safe_print as print
from holophyte.review import consolidate, review_runner
from holophyte.review.json_reply import (
    CONSOLIDATED_SCHEMA_FILE,
    COUNCIL_SCHEMA_FILE,
    REPLY_BRIEF,
    RenderedReply,
    SchemaBrief,
    candidate_path,
    event_reply,
    finding_line,
    kept_line,
    message_line,
    rendered,
    reply_object,
    retry_brief,
)
from holophyte.review.reply_parsing import (
    cited_approval,
    criteria_statuses,
    unparsed_path,
)
from store.working import working

QUORUM = 2
SCORE_BRIEF = (" Also give score: your score for the candidate as it stands, an "
               "integer from 0 (not mergeable) to 10 (ready to merge).")
UNREADABLE = "reply failed the council reply schema twice"
UNCONSOLIDATED = "reply failed the consolidated reply schema twice"
SHARED = "raised by two or more seats at the same path and line"
UNREASONED = "dropped without a reason"
DESCRIBED = "PR description"
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
    down = routes(project).council_down.get(number)
    if down is not None:
        return ended("missing", reason=f"startup probe failed: {down}")
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
    with review_runner.removing_on_signal():
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
              criteria, run_agent, noted=None):
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
    if noted is not None:
        noted(seat_detail(seated, None))
    answered = [seat for seat in seated if seat.status == "answered"]
    if len(answered) < QUORUM:
        raise InfraFailure(
            f"council review had {len(answered)} of {len(seated)} seats "
            f"answer, under the quorum of {QUORUM}: {'; '.join(absent)}; "
            f"candidate preserved at {sha}", "review_route")
    value = merged([seat.value for seat in answered], criteria, wt, sha)
    return council_reply(value, seated), value["verdict"], ""


def council_reply(value, seated):
    reply = RenderedReply(rendered(value))
    reply.json_reply = value
    reply.council_findings = stored_findings(value["findings"])
    reply.council_route = "council " + ", ".join(
        seat.route for seat in seated if seat.status == "answered")
    reply.council_seats = seated
    return reply


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


def stored_findings(findings):
    stored = []
    for finding in findings:
        message = message_line(finding["message"])
        path = (DESCRIBED if finding["target"] == "description" else
                candidate_path(kept_line(finding["path"])))
        entry = {"path": path or unparsed_path(message),
                 "severity": finding["severity"], "message": message,
                 "category": finding["category"]}
        if finding["line"] is not None and finding["line"] > 0:
            entry["line"] = finding["line"]
        if "raised_by" in finding:
            entry["raised_by"] = finding["raised_by"]
        stored.append(entry)
    return redact_document(stored)


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


def consolidator_route(project):
    argv = agent_command(project, "consolidate", "")
    if argv is not None:
        return None, argv_label(argv[:-1])
    model, effort = review_route(project)
    route = {"harness": "codex", "model": model, "effort": effort,
             "credential": None}
    return route, family_label(route)


def consolidated(project, conn, run_id, wt, base_sha, sha, rnd, verdict,
                 criteria, adversarial, run_agent):
    seated = verdict.council_seats
    answered = [seat for seat in seated if seat.status == "answered"]
    found = consolidate.council_inputs(answered, adversarial or [])
    route, label = consolidator_route(project)
    goal = consolidate.council_brief(seated, found, criteria, sha)
    value, why = consolidator_reply(project, conn, run_id, wt, base_sha, sha,
                                    goal, route, run_agent)
    if value is None:
        summary = (f"round {rnd}: council not consolidated, {why}; the round "
                   "takes the strictest-wins merge of its seats")
        print(f"[holo2] {summary}")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "council_unconsolidated", summary)
        return verdict, verdict.json_reply["verdict"]
    final, concerns = ruled(conn, run_id, rnd, value, answered, found,
                            criteria, wt, sha)
    reply = council_reply(final, seated)
    reply.consolidated_by = label
    reply.council_concerns = concerns
    return reply, final["verdict"]


def consolidator_reply(project, conn, run_id, wt, base_sha, sha, goal, route,
                       run_agent):
    for _ in range(2):
        try:
            reply = run_agent(project, "consolidate",
                              goal if route else str(goal), wt,
                              base_sha=base_sha, candidate_sha=sha,
                              timeout=consolidate.TIMEOUT, conn=conn,
                              run_id=run_id, family_route=route)
        except (InfraFailure, OSError, subprocess.SubprocessError) as failure:
            if not getattr(failure.__cause__, "schema_refused", False):
                return None, f"the consolidator's route failed: {failure}"
            reply = ""
        if getattr(reply, "timed_out", False) or getattr(reply, "exit_code", 0):
            return None, ("the consolidator's route failed: "
                          + ("timed out" if getattr(reply, "timed_out", False)
                             else f"exited {reply.exit_code}"))
        text = event_reply(str(reply), CONSOLIDATED_SCHEMA_FILE) or str(reply)
        value = reply_object(text, CONSOLIDATED_SCHEMA_FILE)
        if value is not None:
            return value, None
        goal = retry_brief(goal)
    return None, f"the consolidator's {UNCONSOLIDATED}"


def ruled(conn, run_id, rnd, value, answered, found, criteria, wt, sha):
    kept, covered = [], set()
    for finding in value["findings"]:
        ids = [name for name in dict.fromkeys(finding["raised_by"])
               if name in found]
        covered.update(ids)
        kept.append(dict(finding, raised_by=raisers(found, ids)))
    shared = shared_places(found)
    dropped, refuted = set(), []
    for entry in value["refuted"]:
        ids = [name for name in dict.fromkeys(entry["raised_by"])
               if name in found and name not in covered
               and spot(found[name][1]) not in shared]
        if ids and entry["reason"].strip():
            dropped.update(ids)
            refuted.append(dict(entry, raised_by=raisers(found, ids)))
    for at, ids in unaccounted(found, covered | dropped).items():
        held = next((finding for finding in kept
                     if at in shared and spot(finding) == at), None)
        if held is not None:
            held.update(severity=min(held["severity"], *(
                found[name][1]["severity"] for name in ids)),
                raised_by=list(dict.fromkeys(
                    held["raised_by"] + raisers(found, ids))))
            continue
        finding = restored(found, ids)
        kept.append(finding)
        record_kept(conn, run_id, rnd, finding,
                    SHARED if at in shared else UNREASONED)
    concerns = [found[name][3] for name in found
                if found[name][2] == "concern" and name not in covered]
    judged = ruled_criteria(value, answered, criteria, wt, sha)
    approve = not kept and all(entry["status"] == "met" for entry in judged)
    return {"verdict": "APPROVE" if approve else "REQUEST_CHANGES",
            "criteria": judged, "findings": kept,
            "follow_ups": value["follow_ups"],
            "scope": ruled_scope(value, answered), "refuted": refuted}, concerns


def unaccounted(found, accounted):
    left = {}
    for name, (_, finding, evidence, _) in found.items():
        if name not in accounted and evidence != "concern":
            left.setdefault(spot(finding) or name, []).append(name)
    return left


def restored(found, ids):
    findings = sorted((found[name][1] for name in ids),
                      key=lambda finding: finding["severity"])
    messages = dict.fromkeys(finding["message"] for finding in findings)
    return dict(findings[0], message="\n".join(messages),
                raised_by=raisers(found, ids))


def spot(finding):
    path = candidate_path(kept_line(finding["path"]))
    if not path or finding["line"] is None or finding["line"] <= 0:
        return None
    return path, finding["line"]


def raisers(found, ids):
    return list(dict.fromkeys(found[name][0] for name in ids))


def shared_places(found):
    seats = {}
    for who, finding, *_ in found.values():
        if who.startswith("seat") and (at := spot(finding)):
            seats.setdefault(at, set()).add(who)
    return {at for at, who in seats.items() if len(who) >= 2}


def record_kept(conn, run_id, rnd, finding, why):
    where = consolidate.location({"path": finding["path"] or "",
                                  "line": finding["line"]})
    summary = (f"round {rnd}: the council keeps {where} [{finding['severity']}]"
               f" {message_line(finding['message'])}: {why}")
    print(f"[holo2] {summary}")
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "council_kept", summary,
                           level="detail", payload=json.dumps(
                               {"round": rnd, "finding": finding,
                                "reason": why}))


def ruled_criteria(value, answered, criteria, wt, sha):
    seats = [checked(seat.value, criteria, wt, sha) for seat in answered]
    kept = []
    for own, *theirs in zip(checked(value, criteria, wt, sha), *seats):
        worst = min(theirs, key=lambda entry: RANKS[entry[1]])
        n, status, note, witness = (
            worst if worst[1] != "met" and not (own[1] == "met" and own[3])
            else own)
        kept.append({"id": f"C{n}", "status": status, "reason": note,
                     "witness": witness})
    return kept


def ruled_scope(value, answered):
    own = {entry["path"] for entry in value["scope"]}
    return value["scope"] + [
        entry for entry in merged_scope([seat.value for seat in answered])
        if entry["path"] not in own]


def detail(verdict):
    return seat_detail(verdict.council_seats,
                       getattr(verdict, "consolidated_by", None))


def seat_detail(seated, consolidated_by):
    return {"seats": [{"seat": seat.number, "route": seat.route,
                       "status": seat.status,
                       "score": (seat.value or {}).get("score")}
                      for seat in seated],
            "consolidated_by": consolidated_by}


def held_here(project, verdict):
    return (getattr(verdict, "consolidated_by", None) is not None
            and merge_config(project).mode == "pr")


def described(finding):
    return isinstance(finding, dict) and finding.get("path") == DESCRIBED


def held_description(conn, run_id, verdict, findings):
    hold_for_pr_text(conn, run_id, [finding_line(
        {"target": "description", "severity": finding["severity"],
         "message": finding["message"]}) for finding in findings])
    return without_description(verdict)[0]


def description_only(verdict, findings):
    value = verdict.json_reply
    return (not findings and bool(value["findings"])
            and all(finding["target"] == "description"
                    for finding in value["findings"])
            and all(entry["status"] == "met" for entry in value["criteria"]))
