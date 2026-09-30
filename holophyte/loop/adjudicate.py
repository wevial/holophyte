"""The terminal adjudication: one bare verdict once the review rounds are spent."""
from time import time

import review_runner
from holophyte import failure_reason
from holophyte.agents.review_workspace import review_refs
from holophyte.agents.roles import agent
from holophyte.board.projection import ledger
from holophyte.loop.gates import (
    RunFailure,
    record_unreviewed_verification,
    run_verify,
    with_baseline,
)
from holophyte.loop.review_round import _verify_brief
from holophyte.loop.runs import heartbeat_while, record_round, set_phase
from holophyte.loop.stop import boundary, stop_if_requested
from holophyte.redact import safe_print as print
from holophyte.review.briefs import criteria_brief, tests_brief


def _terminal_adjudication(project, conn, run_id, provider, task_id, task,
                           branch, wt, beat_s, base_sha, sha, ticket,
                           verify_cmd, contracts, cap, criteria=(), resume=None):
    """No fix round follows: anything but PASS preserves the branch and stops."""
    pending = resume if resume and resume.get("terminal") else {}
    if pending:
        set_phase(conn, run_id, "verifying", "resume terminal adjudication")
        ok, out = pending["ok"], pending["out"]
    else:
        set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
        with heartbeat_while(conn, run_id, beat_s):
            ok, out = run_verify(verify_cmd, wt, contracts, conn=conn,
                                         run_id=run_id,
                                 project=project)
            ok, out = with_baseline(project, wt, verify_cmd, ok, out,
                                   conn, run_id)
    boundary(conn, run_id, "reviewing", terminal=True, rnd=cap + 1,
             ok=ok, out=str(out), reply=pending.get("reply"))
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
    if pending.get("reply"):
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
                + criteria_brief(criteria)
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
        record_round(project, conn, run_id, cap + 1, "adjudicate", reply,
                     verify_cmd, ok, out, started_at=round_started)
    try:
        decision = review_runner.terminal_verdict(
            reply, review_runner.ADJUDICATION_VERDICTS)
    except review_runner.ReviewBoundaryError:
        decision = "MALFORMED"
    if decision != "PASS":
        boundary(conn, run_id, "reviewing", terminal=True, rnd=cap + 1,
                 ok=ok, out=str(out), reply=str(reply))
        print(f"[holo2] terminal adjudication: {decision}; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human. Task: {task}")
        ledger(conn, run_id, task_id, "adjudication",
               f"Terminal adjudication after {cap} review "
               f"rounds: {decision}; branch {branch} preserved at "
               f"{sha}\n\nAdjudicator reply:\n{reply}", provider)
        raise RunFailure(failure_reason.adjudication(
            reply, criteria, decision,
            f"branch {branch} preserved at {sha[:12]}"))
    stop_if_requested(conn, run_id, "merge_gate")
    print("[holo2] terminal adjudication: PASS")
    ledger(conn, run_id, task_id, "adjudication",
           f"Terminal adjudication after {cap} review "
           f"rounds: PASS\n\nAdjudicator reply:\n{reply}", provider)
