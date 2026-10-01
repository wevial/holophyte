"""The review rounds: verify, review and one fix turn per round, up to the cap."""
import json
from time import time

import store
import store.read
import ticket_template
from holophyte import failure_reason
from holophyte.agents.review_workspace import review_refs
from holophyte.agents.roles import agent
from holophyte.board.projection import ledger
from holophyte.config.agent_settings import review_mode
from holophyte.config.config_tables import loop_config
from holophyte.loop.claim import merge_conflicts
from holophyte.loop.gates import (
    InfraFailure,
    RunFailure,
    run_verify,
    sh,
    verify_timed_out,
    with_baseline,
)
from holophyte.loop.implement import _check_run_cap, _timed
from holophyte.loop.runs import (
    heartbeat_while,
    record_round,
    review_round_cap,
    set_phase,
)
from holophyte.loop.stop import boundary, stop_if_requested
from holophyte.redact import safe_print as print
from holophyte.review.briefs import (
    criteria_brief,
    evidence_brief,
    scope_brief,
    scope_files,
    stale_approval_brief,
    tests_brief,
    verified_brief,
)
from holophyte.review.reply_parsing import (
    _review_reply,
    cited_approval,
    criteria_findings,
    stale_approvals,
    without_refuted,
)
from holophyte.review.stale_approval import stale_again, stale_rereview


def _verify_brief(verify_cmd, ok, out):
    """Show ticket and baseline checks; omit the brief only if neither ran."""
    count = sum(row["source"] == "baseline"
                for row in getattr(out, "results", []))
    if not verify_cmd and not count:
        return ""
    return (f"The ticket's verification commands and the project's baseline "
            f"({count} commands) were run and "
            f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n"
            + ("The ticket's checks and the project's baseline passed at this "
               "commit; the full suite runs as a pull request check. Do not run "
               "the full suite, run only focused tests needed to check a "
               "specific concern.\n" if ok else ""))


def _changed_lines(wt):
    """Count changed lines against the merge base; binary files count as zero."""
    base = sh(["git", "merge-base", "main", "HEAD"], cwd=wt)
    total = 0
    for line in sh(["git", "diff", "--numstat", base, "HEAD"], cwd=wt).splitlines():
        added, removed, *_ = line.split("\t")
        total += sum(int(n) for n in (added, removed) if n.isdigit())
    return total


def _review_cap(project, conn, run_id, provider, task_id, wt):
    lines = _changed_lines(wt)
    cap = review_round_cap(lines, loop_config(project))
    print(f"[holo2] review cap {cap} for {lines} changed lines")
    if conn is not None and run_id is not None:
        store.set_review_round_cap(conn, run_id, cap)
    ledger(conn, run_id, task_id, "note",
           f"Review cap {cap} for {lines} changed lines", provider)
    return cap


def _record_mode(conn, run_id, mode, rnd):
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "review_mode", f"review mode: {mode}",
                           level="detail",
                           payload=json.dumps({"mode": mode, "round": rnd}))


def _review(project, conn, run_id, task_id, wt, beat_s, base_sha, sha, ticket,
            verify_cmd, criteria, mode, rnd, ok, out, stale=()):
    round_started = int(time() * 1000)
    scope = scope_files(wt, ticket, base_sha, sha)
    _record_mode(conn, run_id, mode, rnd)
    with heartbeat_while(conn, run_id, beat_s):
        verdict, decision, first_reply = _review_reply(project,
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            f"{review_refs(run_id)[0]} as the frozen base and "
            f"{review_refs(run_id)[1]} as the candidate "
            "in this repo against the ticket below. The ticket is the "
            "contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + _verify_brief(verify_cmd, ok, out)
            + criteria_brief(criteria)
            + stale_approval_brief(stale)
            + verified_brief(mode)
            + tests_brief(wt)
            + scope_brief(wt, ticket, base_sha, sha)
            + evidence_brief(project, wt, task_id,
                             ticket_template.parse(ticket).evidence_states)
            + "Do not modify anything. End your reply with exactly one "
            "line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha, sha, conn, run_id, run_agent=agent, review_round=rnd)
    approved = cited_approval(verdict, wt, sha)
    record_round(project, conn, run_id, rnd, "review", verdict, verify_cmd,
                 ok, out,
                 started_at=round_started, criteria=criteria, root=wt,
                 prior_reply=first_reply, approved_range=approved, scope=scope)
    if decision == "MALFORMED":
        reason = "reviewer returned no verdict line twice"
        print(f"[holo2] round {rnd}: {reason}")
        raise InfraFailure(f"{reason}; candidate preserved at {sha}",
                           "review_route")

    unwitnessed = criteria_findings(verdict, criteria, wt,
                                    approved_range=approved, scope=scope)
    if unwitnessed:
        print(f"[holo2] round {rnd}: {len(unwitnessed)} criteria not "
              "witnessed; treating as REQUEST_CHANGES")
    return verdict, decision, unwitnessed


def _rereview(conn, run_id, provider, task_id, branch, sha, rnd, stale,
              pending, ok, out):
    if pending.get("stale"):
        stale_again(branch, sha, stale)
    stale_rereview(conn, run_id, provider, task_id, sha, rnd, stale)
    return {"phase": "reviewing", "ok": ok, "out": out, "stale": stale}


def _review_rounds(project, conn, run_id, provider, task_id, branch, wt, beat_s,
                   base_sha, sha, ticket, verify_cmd, contracts, criteria,
                   budget_min, cap, resume=None):
    """Verify, review and fix up to `cap` rounds; return sha, round, approval."""
    pending = resume or {}
    mode = review_mode(project)
    rnd = pending.get("rnd", 1) - 1
    for rnd in range(pending.get("rnd", 1), cap + 1):
        set_phase(conn, run_id, "verifying", f"round {rnd}: verify before review")
        if rnd == 1:
            unresolved = merge_conflicts(wt)
            if unresolved:
                raise RunFailure(
                    f"preserved commits on {branch} conflict with a main"
                    f" that moved on and the implementer left the merge"
                    f" unresolved in {', '.join(unresolved)}; a human"
                    f" resolves the merge before this ticket is run again;"
                    f" branch {branch} preserved at {sha[:12]}")
        if pending.get("phase") in ("reviewing", "addressing"):
            ok, out = pending.get("ok", False), pending.get("out", "")
        else:
            with heartbeat_while(conn, run_id, beat_s):
                ok, out = run_verify(verify_cmd, wt, contracts, conn=conn,
                                     run_id=run_id,
                                 project=project)
                ok, out = with_baseline(project, wt, verify_cmd, ok, out,
                                       conn, run_id)
            if ok:
                print(f"[holo2] verify ok before round {rnd}")
            else:
                print(f"[holo2] verify FAILED before round {rnd}:\n{out}")

        boundary(conn, run_id, "reviewing", rnd=rnd, ok=ok, out=str(out),
                 **({"stale": pending["stale"]} if "stale" in pending else {}))
        set_phase(conn, run_id, "reviewing", f"round {rnd} review")
        if pending.get("phase") == "addressing":
            verdict = pending["verdict"]
        else:
            verdict, decision, unwitnessed = _review(
                project, conn, run_id, task_id, wt, beat_s, base_sha, sha,
                ticket, verify_cmd, criteria, mode, rnd, ok, out,
                pending.get("stale", ()))
            if ok and not unwitnessed and decision == "APPROVE":
                stop_if_requested(conn, run_id, "merge_gate")
                ledger(conn, run_id, task_id, "round",
                       f"Round {rnd}: APPROVE\nReviewer verdict:\n{verdict}",
                       provider)
                return sha, rnd, True
            if (not ok and not unwitnessed and decision == "APPROVE"
                    and verify_timed_out(out)):
                # A fix turn cannot shorten a command the ticket requires.
                head = str(out).splitlines()[0].removeprefix("[verify] FAILED: ")
                print(f"[holo2] round {rnd}: {head} and the review approved; "
                      f"no fix round. Leaving branch {branch} at {sha}.")
                ledger(conn, run_id, task_id, "round",
                       f"Round {rnd}: APPROVE, but the verify timed out; no fix "
                       f"round, branch {branch} preserved at {sha}\n\n{out}",
                       provider)
                raise RunFailure(failure_reason.verify(
                    out, verify_cmd, f"{head} and the review approved, so no "
                    f"fix round was run; branch {branch} preserved at "
                    f"{sha[:12]}"))
            stale = stale_approvals(decision, unwitnessed) if ok else []
            if stale:
                pending = _rereview(conn, run_id, provider, task_id, branch,
                                    sha, rnd, stale, pending, ok, out)
                continue

        _check_run_cap(project, conn, run_id, budget_min, sha)
        boundary(conn, run_id, "addressing", rnd=rnd, ok=ok,
                 out=str(out), verdict=verdict)
        pending = {}
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        from holophyte.agents.fix_session import fix_turn
        fixes, timed_out = fix_turn(
            project, conn, run_id, beat_s, wt, budget_min, ticket,
            without_refuted(verdict), sha, timed=_timed, check_cap=_check_run_cap)
        boundary(conn, run_id, "verifying", rnd=rnd + 1)
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
               f"Reviewer findings:\n{verdict}\n\n"
               f"Implementer response:\n{fixes}", provider)
        if timed_out or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            findings = store.read.rounds_of(conn, run_id)[-1].findings if conn else None
            pending = json.loads(findings) if findings else []
            raise RunFailure(failure_reason.fix_round(
                pending, timed_out, f"branch {branch} preserved at {sha[:12]}"))
        sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    return sha, rnd, False
