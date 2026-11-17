"""The terminal adjudication: one bare verdict once the review rounds are spent."""
import json
from functools import partial
from time import time

import store
import store.read
from holophyte import failure_reason
from holophyte.agents.fallback import failure_line
from holophyte.agents.review_workspace import review_base, review_refs
from holophyte.agents.roles import agent
from holophyte.board.projection import ledger
from holophyte.config.config_tables import verify_config
from holophyte.loop.gates import (
    InfraFailure,
    RunFailure,
    record_unreviewed_verification,
    run_verify,
    with_baseline,
)
from holophyte.loop.review_round import _verify_brief
from holophyte.loop.runs import heartbeat_while, record_round, set_phase
from holophyte.loop.steering import adopted_verify, reviewed_ticket
from holophyte.loop.stop import boundary, stop_if_requested
from holophyte.redact import safe_print as print
from holophyte.review import review_runner
from holophyte.review.briefs import (
    criteria_brief,
    left_open_brief,
    test_name_listing,
    tests_brief,
)
from holophyte.review.criterion_ids import numbered, parse
from holophyte.review.reply_parsing import (
    DEFAULT_SEVERITY,
    MISSING_WITNESS_NOTE,
    _inside,
    criteria_block,
    criteria_findings,
    criterion_accounts,
    finding_message,
    missing_witnesses,
    test_references,
)


def _findings_across_resumes(conn, run_id, review_round):
    runs = conn.execute(
        "SELECT old.id, old.outcome FROM runs old JOIN runs current"
        " ON old.ticketId = current.ticketId WHERE current.id = ?"
        " AND old.id <= current.id ORDER BY old.id DESC", (run_id,)).fetchall()
    for each, outcome in runs:
        if each != run_id and outcome != "paused":
            break
        for ended in store.read.rounds_of(conn, each):
            if ended.round == review_round and ended.endedAt is not None:
                return json.loads(ended.findings)
    return []


def _left_open(conn, run_id, review_round, criteria):
    if conn is None:
        return []
    numbers = dict(numbered(conn, run_id, criteria))
    return sorted({finding["line"] for finding
                   in _findings_across_resumes(conn, run_id, review_round)
                   if finding.get("path") == store.CRITERIA_PATH
                   and finding.get("line") in numbers})


def _criteria_gate(reply, criteria, wt, left_open):
    findings = criteria_findings(reply, criteria, wt)
    flagged = {finding["line"] for finding in findings}
    block = criteria_block(reply)
    texts = dict(numbered(None, None, criteria))
    for n in left_open:
        if n in flagged or test_references(block[n][1]):
            continue
        message = (f"CRITERION C{n}: unwitnessed \u2014 the review round left "
                   "it unmet or unwitnessed; only a named test overturns that"
                   f"\n{texts[n]}")
        findings.append({"path": store.CRITERIA_PATH, "line": n,
                         "severity": DEFAULT_SEVERITY,
                         "message": finding_message(message)})
    return sorted(findings, key=lambda finding: finding["line"])


def _refusal(refused):
    lines = "".join(f"Refused: {finding['message'].splitlines()[0]}\n"
                    for finding in refused)
    return f"{lines}\n" if lines else ""


def _mistyped(refused, reply, wt):
    block, paths = criteria_block(reply), []
    for finding in refused:
        n = finding["line"]
        status, note = block.get(n, (None, ""))
        prefix = f"CRITERION C{n}: unwitnessed \u2014 {MISSING_WITNESS_NOTE}"
        if status != "met" or not finding["message"].startswith(prefix):
            return []
        missing = [ref for ref in test_references(note)
                   if missing_witnesses([ref], wt)]
        if not missing or any((file := _inside(wt, ref[0])) is None
                              or not file.is_file() for ref in missing):
            return []
        paths += [ref[0] for ref in missing]
    return paths


def _criterion_lines(reply, numbers):
    return {parse(match["n"]): match[0].strip()
            for match in criterion_accounts(reply)
            if parse(match["n"]) in numbers}


def _recite(project, conn, run_id, wt, beat_s, base_sha, sha, reply, refused,
            paths):
    lines = _criterion_lines(reply, {finding["line"] for finding in refused})
    with heartbeat_while(conn, run_id, beat_s):
        recite = agent(project, "adjudicate",
            f"Your terminal adjudication of commit {sha} replied VERDICT: "
            "PASS, but the loop refused these CRITERION lines, as each names "
            "a test its file does not define:\n"
            + "".join(f"{line}\n" for line in lines.values()) + "\n"
            + _refusal(refused)
            + "The files they name define exactly these tests:\n"
            + test_name_listing(wt, paths) + "\n"
            "Restate only those CRITERION lines, one line each in the same "
            "form, naming each test copied verbatim from this list. Do not "
            "modify anything and write nothing else.", wt,
            base_sha=base_sha, candidate_sha=sha, conn=conn, run_id=run_id)
    failed = failure_line(recite)
    if failed:
        raise InfraFailure(f"terminal adjudication re-cite turn failed: {failed}")
    return recite


def _recited_lines(reply, recite, refused):
    numbers = {finding["line"] for finding in refused}
    first, restated = (_criterion_lines(reply, numbers),
                       _criterion_lines(recite, numbers))
    return "Re-cited after a refused test name:\n" + "".join(
        f"Cited: {first.get(n, '(no line)')}\n"
        f"Restated: {restated.get(n, '(no line)')}\n"
        for n in sorted(numbers)) + "\n", "\n".join(restated.values())


def _recited(project, conn, run_id, wt, beat_s, base_sha, sha, reply, refused,
             criteria, left_open, recorded, record):
    paths = _mistyped(refused, reply, wt)
    if not paths:
        return None, "", refused
    try:
        recite = recorded if recorded is not None else _recite(
            project, conn, run_id, wt, beat_s, base_sha, sha, reply, refused,
            paths)
    except InfraFailure:
        if record:
            record(structured_findings=refused)
        raise
    recited, restated = _recited_lines(reply, recite, refused)
    return recite, recited, _criteria_gate(f"{reply}\n{restated}", criteria,
                                           wt, left_open)


def _terminal_adjudication(project, conn, run_id, provider, task_id, task,
                           branch, wt, beat_s, base_sha, sha, ticket,
                           verify_cmd, contracts, cap, criteria=(), resume=None,
                           review_round=None):
    """No fix round follows: anything but PASS preserves the branch and stops."""
    base_sha = review_base(wt, base_sha, sha)
    criteria = numbered(conn, run_id, criteria)
    claimed, verify_cmd = verify_cmd, adopted_verify(conn, run_id, verify_cmd)
    pending = resume if resume and resume.get("terminal") else {}
    if pending.get("verify_cmd", claimed) != verify_cmd:
        pending = {}
    ticket = reviewed_ticket(conn, run_id, ticket)
    review_round = review_round or cap
    left_open = (pending["left_open"] if "left_open" in pending
                 else _left_open(conn, run_id, review_round, criteria))
    if pending:
        set_phase(conn, run_id, "verifying", "resume terminal adjudication")
        ok, out = pending["ok"], pending["out"]
    else:
        set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
        with heartbeat_while(conn, run_id, beat_s):
            ok, out = run_verify(verify_cmd, wt, contracts,
                                 verify_config(project).timeout_sec, conn=conn,
                                 run_id=run_id, project=project)
            ok, out = with_baseline(project, wt, verify_cmd, ok, out,
                                   conn, run_id)
    boundary(conn, run_id, "reviewing", terminal=True, rnd=cap + 1,
             ok=ok, out=str(out), reply=pending.get("reply"),
             recite=pending.get("recite"), left_open=left_open,
             review_round=review_round, verify_cmd=verify_cmd)
    if not ok:
        record_unreviewed_verification(conn, run_id, out)
        print(f"[holo2] verify FAILED before adjudication; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human:\n{out}")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED verify before terminal adjudication after "
               f"{cap} review rounds (the run's cap); branch {branch} preserved "
               f"at {sha}\n\n{out}", provider)
        raise RunFailure(failure_reason.verify(
            out, verify_cmd, f"before terminal adjudication; "
            f"branch {branch} preserved at {sha[:12]}"))
    print("[holo2] verify ok before adjudication")

    set_phase(conn, run_id, "reviewing", "terminal adjudication")
    recorded = bool(pending.get("reply"))
    if recorded:
        reply = pending["reply"]
    else:
        round_started = int(time() * 1000)
        with heartbeat_while(conn, run_id, beat_s):
            reply = agent(project, "adjudicate",
                f"You are a READ-ONLY final adjudicator. Judge commit {sha} "
                f"using {review_refs(run_id)[0]} as the frozen base and "
                f"{review_refs(run_id)[1]} as the candidate "
                "in this repo against the ticket below. The ticket is the "
                "contract, acceptance criteria included: a candidate that "
                "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
                f"{ticket}\n\n"
                + _verify_brief(verify_cmd, ok, out)
                + criteria_brief(criteria, "FAIL", wt)
                + left_open_brief(criteria, left_open)
                + tests_brief(wt)
                + "This candidate has already had its review rounds and their "
                "fixes; no further fix round exists. Your job is a verdict on "
                "the state as it stands, not a review.\n"
                "Do not modify anything. Do NOT list findings, request "
                "changes, or propose follow-up work — a reply that reads as a "
                "findings list is not a verdict and is treated as FAIL. Give "
                "at most one short paragraph of justification, then exactly "
                "one final line:\n"
                "VERDICT: PASS  or  VERDICT: FAIL\n"
                "PASS means the candidate is mergeable as it stands.", wt,
                base_sha=base_sha, candidate_sha=sha, conn=conn, run_id=run_id)
        failed = failure_line(reply)
        if failed:
            raise InfraFailure(f"terminal adjudication turn failed: {failed}")
    try:
        decision = review_runner.terminal_verdict(
            reply, review_runner.ADJUDICATION_VERDICTS)
    except review_runner.ReviewBoundaryError:
        decision = "MALFORMED"
    if (not recorded and decision == "PASS" and criteria
            and not criteria_block(reply)):
        raise InfraFailure("terminal adjudication PASS named no criterion")
    refused = (_criteria_gate(reply, criteria, wt, left_open)
               if decision == "PASS" else [])
    record = None if recorded else partial(
        record_round, project, conn, run_id, cap + 1, "adjudicate", reply,
        verify_cmd, ok, out, started_at=round_started)
    recite, recited, refused = _recited(
        project, conn, run_id, wt, beat_s, base_sha, sha, reply, refused,
        criteria, left_open, pending.get("recite"), record)
    if record:
        record(structured_findings=refused or None)
    if recite is not None and not pending.get("recite"):
        boundary(conn, run_id, "reviewing", terminal=True, rnd=cap + 1,
                 ok=ok, out=str(out), reply=str(reply), recite=recite,
                 left_open=left_open, review_round=review_round,
                 verify_cmd=verify_cmd)
    if refused:
        decision = "FAIL"
    if decision != "PASS":
        boundary(conn, run_id, "reviewing", terminal=True, rnd=cap + 1,
                 ok=ok, out=str(out), reply=str(reply), recite=recite,
                 left_open=left_open, review_round=review_round,
                 verify_cmd=verify_cmd)
        print(f"[holo2] terminal adjudication: {decision}; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human. Task: {task}")
        ledger(conn, run_id, task_id, "adjudication",
               f"Terminal adjudication after {cap} review "
               f"rounds: {decision}; branch {branch} preserved at "
               f"{sha}\n\n{_refusal(refused)}{recited}"
               f"Adjudicator reply:\n{reply}",
               provider)
        raise RunFailure(failure_reason.adjudication(
            refused or criteria_findings(reply, criteria), criteria, decision,
            f"branch {branch} preserved at {sha[:12]}"))
    stop_if_requested(conn, run_id, "merge_gate")
    print("[holo2] terminal adjudication: PASS")
    ledger(conn, run_id, task_id, "adjudication",
           f"Terminal adjudication after {cap} review "
           f"rounds: PASS\n\n{recited}Adjudicator reply:\n{reply}", provider)
