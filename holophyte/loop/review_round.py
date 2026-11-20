"""The review rounds: verify, review and one fix turn per round, up to the cap."""
import json
from functools import partial
from time import time

import store
import store.council
import store.read
from holophyte import failure_reason, leak_guard
from holophyte.agents.fix_session import steer_turn
from holophyte.agents.review_workspace import review_base, review_refs
from holophyte.agents.roles import agent
from holophyte.board.projection import ledger
from holophyte.config.agent_settings import review_mode
from holophyte.config.config_tables import loop_config, verify_config
from holophyte.config.review_settings import review_config
from holophyte.loop import (
    contradiction,
    fix_self_check,
    stuck_review,
    witness_turns,
)
from holophyte.loop.claim import merge_conflicts
from holophyte.loop.follow_ups import capture_review
from holophyte.loop.gates import (
    InfraFailure,
    RunFailure,
    cached_pass,
    commands_launched,
    run_verify,
    sh,
    timed_out,
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
from holophyte.loop.steering import adopted_verify, reviewed_ticket
from holophyte.loop.steering import pending as steer_pending
from holophyte.loop.stop import boundary, stop_if_requested
from holophyte.loop.test_removal import with_removals
from holophyte.pr.pr_media import capture_spec_digest
from holophyte.redact import safe_print as print
from holophyte.review import adversary, consolidate, council
from holophyte.review.blast_radius import record_tier
from holophyte.review.briefs import (
    CORNER_CASE_RULE,
    ONE_PASS_RULE,
    criteria_brief,
    earlier_rounds_brief,
    evidence_brief,
    follow_up_place,
    scope_brief,
    scope_files,
    stale_approval_brief,
    tests_brief,
    trim_brief,
    verified_brief,
)
from holophyte.review.criterion_ids import numbered
from holophyte.review.json_reply import reply_brief
from holophyte.review.reply_parsing import (
    FINDING_FORMAT,
    _review_reply,
    cited_approval,
    criteria_findings,
    follow_up_lines,
    stale_approvals,
    without_follow_ups,
    without_refuted,
)
from holophyte.review.stale_approval import stale_again, stale_rereview
from holophyte.stages import record
from holophyte.tickets import ticket_template, witness_once


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


def _verify_findings(verify_cmd, ok, out):
    if ok or verify_timed_out(out):
        return []
    facts = getattr(out, "failure", None) or {}
    command = facts.get("command") or verify_cmd or "(no command)"
    status = facts.get("exit_status")
    title = (f"failing verify command: {command}"
             + (f" (exit {status})" if status is not None else ""))
    last = facts.get("last_output_line")
    return [{"path": "verify", "line": facts.get("command_index") or None,
             "severity": "p1", "title": title,
             "message": f"{title}\n{last}" if last else title}]


def _with_report(verdict, red, out):
    return f"{verdict}\n\n{out}" if red else verdict


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


def _evidence_brief(project, wt, task_id, ticket):
    parsed = ticket_template.parse(ticket)
    return evidence_brief(project, wt, task_id, parsed.evidence_states,
                          parsed.evidence_files, ui_change=parsed.ui_change)


def round_prompt(project, run_id, task_id, wt, base_sha, sha, ticket,
                 verify_cmd, criteria, mode, ok, out, stale=(), evidence=True,
                 witness="", earlier="", notes="", seated=False):
    schema_tail = (f" {ONE_PASS_RULE} {CORNER_CASE_RULE} Give such a case as a "
                   "guardrail follow_up.")
    brief = council.reply_brief(schema_tail) if seated else reply_brief(
        project,
        "Do not modify anything. End your reply with exactly one line:\n"
        "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
        "If REQUEST_CHANGES, list only concrete blockers, each as one list "
        f"item, {FINDING_FORMAT}. {ONE_PASS_RULE} {CORNER_CASE_RULE} Write "
        "such a case as a `FOLLOW_UP(guardrail): TEXT @ PATH:LINE` line "
        f"before {follow_up_place(criteria)}.",
        schema_tail)
    return (
        f"You are a READ-ONLY code reviewer. Review commit {sha} using "
        f"{review_refs(run_id)[0]} as the frozen base and "
        f"{review_refs(run_id)[1]} as the candidate "
        "in this repo against the ticket below. The ticket is the "
        "contract, acceptance criteria included: a candidate that "
        "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
        f"{ticket}\n\n"
        + _verify_brief(verify_cmd, ok, out)
        + criteria_brief(criteria, wt=wt)
        + witness_once.reviewer_brief(criteria)
        + witness
        + stale_approval_brief(stale)
        + earlier
        + verified_brief(mode)
        + tests_brief(wt)
        + scope_brief(wt, ticket, base_sha, sha)
        + trim_brief(wt, base_sha, sha)
        + (_evidence_brief(project, wt, task_id, ticket) if evidence else "")
        + notes
        + brief)


def _review(project, conn, run_id, provider, task_id, wt, beat_s, base_sha, sha,
            ticket, verify_cmd, criteria, mode, rnd, ok, out, stale, leaks,
            witness, branch, attempt):
    base_sha = review_base(wt, base_sha, sha)
    criteria = numbered(conn, run_id, criteria)
    round_started = int(time() * 1000)
    scope = scope_files(wt, ticket, base_sha, sha)
    _record_mode(conn, run_id, mode, rnd)
    record_tier(project, conn, run_id, wt, base_sha, sha, ticket, rnd)
    plan = adversary.planned(project, conn, run_id, wt, base_sha, sha, rnd)
    earlier = earlier_rounds_brief(conn, run_id, wt, rnd, criteria)
    seats = review_config(project).council
    prompt = round_prompt(project, run_id, task_id, wt, base_sha, sha, ticket,
                          verify_cmd, criteria, mode, ok, out, stale,
                          witness=witness,
                          earlier=earlier + stuck_review.settled(conn, run_id),
                          notes=record.notes_block(project, conn, run_id,
                                                   "review"),
                          seated=bool(seats))
    primary = (partial(council.run_round, project, conn, run_id, wt, base_sha,
                       sha, prompt, rnd, seats, criteria, agent,
                       lambda detail: attempt.note(council=detail))
               if seats else
               partial(_review_reply, project, prompt, wt, base_sha, sha, conn,
                       run_id, run_agent=agent, review_round=rnd))
    with heartbeat_while(conn, run_id, beat_s):
        (verdict, decision, first_reply), attacked = adversary.beside(
            conn, run_id, _decided(attempt, primary),
            plan and _ran(attempt, partial(adversary.attack, project, conn,
                                           run_id, wt, base_sha, ticket, plan,
                                           agent)))
    blocking = adversary.settle(project, conn, run_id, provider, task_id, plan,
                                attacked)
    if seats:
        verdict, decision = council.consolidated(
            project, conn, run_id, wt, base_sha, sha, rnd, verdict, criteria,
            consolidate.adversary_findings(conn, run_id, rnd) if plan else None,
            agent)
        attempt.note(council=council.detail(verdict))
    consolidated_by = getattr(verdict, "consolidated_by", None)
    if consolidated_by:
        blocking = []
    approved = cited_approval(verdict, wt, sha)
    red = _verify_findings(verify_cmd, ok, out)
    leak_guard.record(conn, run_id, leaks)
    private = leak_guard.review_findings(leaks)
    record_round(project, conn, run_id, rnd, "review", verdict, verify_cmd,
                 ok, out,
                 started_at=round_started, criteria=criteria, root=wt,
                 prior_reply=first_reply, approved_range=approved, scope=scope,
                 route=getattr(verdict, "council_route", None),
                 structured_findings=getattr(verdict, "council_findings", None),
                 adversary=blocking + red + private,
                 then=partial(stuck_review.claim, project, conn, run_id, rnd))
    if consolidated_by and conn is not None:
        store.council.record_consolidator(conn, run_id, rnd, consolidated_by)
    attempt.note(decision=decision, findings=len(consolidate.primary_findings(
        verdict, decision, blocking + red + private)))
    if decision == "REQUEST_CHANGES" and conn is not None:
        capture_review(conn, run_id, sha, follow_up_lines(verdict))
    if decision == "MALFORMED":
        reason = "reviewer returned no verdict line twice"
        print(f"[holo2] round {rnd}: {reason}")
        raise InfraFailure(f"{reason}; candidate preserved at {sha}",
                           "review_route")
    adversary.require_done(plan, attacked)

    unwitnessed = criteria_findings(verdict, criteria, wt,
                                    approved_range=approved, scope=scope)
    if unwitnessed:
        print(f"[holo2] round {rnd}: {len(unwitnessed)} criteria not "
              "witnessed; treating as REQUEST_CHANGES")
    findings = unwitnessed + blocking + red + private
    attempt.note(findings=len(consolidate.primary_findings(verdict, decision,
                                                           findings)))
    fixing = consolidate.fixing(decision, findings, ok, out)
    ruling = fixing and stuck_review.adjudicated(
        project, conn, run_id, provider, task_id, branch, wt, beat_s, base_sha,
        sha, ticket, criteria, earlier) or None
    adversarial = stuck_review.kept_findings(
        consolidate.adversary_findings(conn, run_id, rnd) if plan else None,
        ruling)
    primary = stuck_review.kept_findings(consolidate.primary_findings(
        verdict, decision, unwitnessed + red + private), ruling)
    handed = (adversarial is not None and (adversarial or not seats)
              and not consolidated_by and consolidate.handed_on(
                  project, conn, run_id, provider, task_id, rnd, primary,
                  adversarial, fixing,
                  None if seats else partial(
                      agent, project, "consolidate", cwd=wt,
                      base_sha=base_sha, candidate_sha=sha,
                      timeout=consolidate.TIMEOUT, conn=conn, run_id=run_id)))
    if consolidated_by and verdict.council_concerns:
        consolidate.handed_on(project, conn, run_id, provider, task_id, rnd, [],
                              verdict.council_concerns, False, None)
    if ruling is not None and ruling.settled_all:
        return None, decision, findings
    red, private = (stuck_review.kept_findings(red, ruling),
                    stuck_review.kept_findings(private, ruling))
    held = council.held_here(project, verdict)
    asked = consolidate.primary_findings(verdict, decision, unwitnessed)
    superseding = (getattr(verdict, "description_ruled", False)
                   or any(map(council.described, asked)))
    kept = stuck_review.kept_findings(asked, ruling)
    described = [finding for finding in kept
                 if held and council.described(finding)]
    undescribed = [finding for finding in kept if finding not in described]
    verdict = _with_report(
        handed or stuck_review.without_dropped(verdict, undescribed, ruling),
        red, out)
    fixed = ruling.fixed if ruling is not None else []
    if held:
        fixed, ruled = council.split_fixes(fixed)
        verdict = council.held_description(
            conn, run_id, verdict + stuck_review.fix_brief(fixed), described,
            ruled, superseding)
        if (described or ruled) and not (red or private or undescribed):
            decision, findings = "APPROVE", []
            attempt.note(decision=decision)
    else:
        verdict += stuck_review.fix_brief(fixed)
    return leak_guard.with_findings(verdict, private), decision, findings


def _recorded_review(project, conn, run_id, provider, task_id, wt, beat_s,
                     base_sha, sha, ticket, verify_cmd, criteria, mode, rnd, ok,
                     out, stale, leaks, witness, branch):
    adversary_on = review_config(project).adversary
    with record.Attempt(conn, run_id, "review", sha,
                        {"round": rnd, "adversary": "not_planned"
                         if adversary_on else "off"}) as attempt:
        verdict, decision, findings = _review(
            project, conn, run_id, provider, task_id, wt, beat_s, base_sha, sha,
            ticket, verify_cmd, criteria, mode, rnd, ok, out, stale, leaks,
            witness, branch, attempt)
    return verdict, decision, findings, attempt


def _decided(attempt, primary):
    def run():
        verdict, decision, first_reply = primary()
        attempt.note(decision=decision, findings=len(
            consolidate.primary_findings(verdict, decision, [])))
        return verdict, decision, first_reply
    return run


def _ran(attempt, attack):
    def run():
        try:
            return attack()
        finally:
            attempt.note(adversary="ran")
    return run


def _rereview(conn, run_id, provider, task_id, branch, sha, rnd, stale,
              pending, ok, out, verify_cmd, witness):
    if pending.get("stale"):
        stale_again(branch, sha, stale)
    stale_rereview(conn, run_id, provider, task_id, sha, rnd, stale)
    boundary(conn, run_id, "verifying", rnd=rnd + 1, stale=stale)
    return {"phase": "reviewing", "ok": ok, "out": out, "stale": stale,
            "verify_cmd": verify_cmd, "witness": witness}


def _steer_at_top(project, conn, run_id, beat_s, wt, budget_min, ticket, sha,
                  pending):
    steered = steer_turn(project, conn, run_id, beat_s, wt, budget_min, ticket,
                         sha, timed=_timed, check_cap=_check_run_cap)
    return (sha, pending) if steered is None else (steered, {})


def _steer_approved(project, conn, run_id, provider, task_id, wt, beat_s, sha,
                    ticket, budget_min, rnd, verdict):
    print(f"[holo2] round {rnd}: APPROVE with a steer note pending; steer turn"
          f" before round {rnd + 1}")
    ledger(conn, run_id, task_id, "round",
           f"Round {rnd}: APPROVE, steer note pending -> steer turn\n"
           f"Reviewer verdict:\n{verdict}", provider)
    boundary(conn, run_id, "verifying", rnd=rnd + 1)
    return steer_turn(project, conn, run_id, beat_s, wt, budget_min, ticket, sha,
                      timed=_timed, check_cap=_check_run_cap) or sha


def _round_contract(conn, run_id, ticket, claimed, pending):
    verify_cmd = adopted_verify(conn, run_id, claimed)
    kept = pending if pending.get("verify_cmd", claimed) == verify_cmd else {}
    return reviewed_ticket(conn, run_id, ticket), verify_cmd, kept


def _verify(project, conn, run_id, beat_s, wt, base_sha, ticket, verify_cmd,
            contracts, rnd):
    head = record.head(conn, run_id, wt)
    if record.verified_last(conn, run_id, head) and cached_pass(
            run_id, verify_cmd, wt, verify_config(project).timeout_sec):
        launched = commands_launched()
        with record.Deferred(conn, run_id, "verify", head, {"round": rnd},
                             wt=wt) as rerun:
            ok, out = _checked(project, conn, run_id, beat_s, wt, base_sha,
                               ticket, verify_cmd, contracts, rnd)
            if not ok or commands_launched() != launched:
                _close_verify(rerun.opened(), ok, out)
        return ok, out
    with record.Attempt(conn, run_id, "verify", head, {"round": rnd},
                        wt=wt) as attempt:
        ok, out = _checked(project, conn, run_id, beat_s, wt, base_sha, ticket,
                           verify_cmd, contracts, rnd)
        _close_verify(attempt, ok, out)
    return ok, out


def _close_verify(attempt, ok, out):
    attempt.close_at_head("pass" if ok else "fail", "next",
                          timed_out=not ok and (timed_out(out)
                                                or verify_timed_out(out)))


def _checked(project, conn, run_id, beat_s, wt, base_sha, ticket, verify_cmd,
             contracts, rnd):
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts,
                             verify_config(project).timeout_sec,
                             conn=conn, run_id=run_id, project=project)
        ok, out = with_baseline(project, wt, verify_cmd, ok, out, conn, run_id)
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    ok, out = with_removals(wt, review_base(wt, base_sha, sha), sha, ticket,
                            ok, out, conn, run_id)
    if ok:
        print(f"[holo2] verify ok before round {rnd}")
    else:
        print(f"[holo2] verify FAILED before round {rnd}:\n{out}")
    return ok, out


def _verified(project, conn, run_id, provider, task_id, beat_s, wt, base_sha,
              sha, ticket, verify_cmd, contracts, criteria, budget_min, rnd):
    checks = partial(_verify, project, conn, run_id, beat_s, wt, base_sha,
                     ticket, verify_cmd, contracts, rnd)
    ok, out = checks()
    return witness_turns.before_review(
        project, conn, run_id, provider, task_id, beat_s, wt, base_sha, sha,
        ticket, verify_cmd, criteria, budget_min, ok, out, checks, rnd)


def _prepared(project, conn, run_id, provider, task_id, beat_s, wt, base_sha,
              sha, ticket, verify_cmd, contracts, criteria, budget_min, rnd,
              pending):
    phase = pending.get("phase")
    if phase not in ("reviewing", "addressing"):
        return _verified(project, conn, run_id, provider, task_id, beat_s, wt,
                         base_sha, sha, ticket, verify_cmd, contracts, criteria,
                         budget_min, rnd)
    ok, out = pending.get("ok", False), pending.get("out", "")
    if phase == "addressing" or "witness" in pending:
        return sha, ok, out, pending.get("witness", "")
    checks = partial(_verify, project, conn, run_id, beat_s, wt, base_sha,
                     ticket, verify_cmd, contracts, rnd)
    return witness_turns.before_review(
        project, conn, run_id, provider, task_id, beat_s, wt, base_sha, sha,
        ticket, verify_cmd, criteria, budget_min, ok, out, checks, rnd)


def _review_rounds(project, conn, run_id, provider, task_id, branch, wt, beat_s,
                   base_sha, sha, ticket, verify_cmd, contracts, criteria,
                   budget_min, cap, resume=None):
    """Verify, review and fix up to `cap` rounds; return sha, round, approval."""
    pending = resume or {}
    mode = review_mode(project)
    rnd = pending.get("rnd", 1) - 1
    claimed = verify_cmd
    for rnd in range(pending.get("rnd", 1), cap + 1):
        sha, pending = _steer_at_top(project, conn, run_id, beat_s, wt,
                                     budget_min, ticket, sha, pending)
        reviewed, verify_cmd, pending = _round_contract(
            conn, run_id, ticket, claimed, pending)
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
        sha, ok, out, witness = _prepared(
            project, conn, run_id, provider, task_id, beat_s, wt, base_sha,
            sha, reviewed, verify_cmd, contracts, criteria, budget_min, rnd,
            pending)
        leaks = leak_guard.branch_leaks(project, wt, sha)

        boundary(conn, run_id, "reviewing", rnd=rnd, ok=ok, out=str(out),
                 verify_cmd=verify_cmd,
                 **({"stale": pending["stale"]} if "stale" in pending else {}),
                 witness=witness)
        set_phase(conn, run_id, "reviewing", f"round {rnd} review")
        if pending.get("phase") == "addressing":
            verdict = pending["verdict"]
            attempt = record.Attempt(None, None, "review", sha, {})
        else:
            verdict, decision, unwitnessed, attempt = _recorded_review(
                project, conn, run_id, provider, task_id, wt, beat_s, base_sha, sha,
                reviewed, verify_cmd, criteria, mode, rnd, ok, out,
                pending.get("stale", ()), leaks, witness, branch)
            with attempt:
                if verdict is None:
                    attempt.close("fail", "again", sha)
                    ledger(conn, run_id, task_id, "round",
                           f"Round {rnd}: REQUEST_CHANGES, every finding settled by"
                           " the adjudicator -> review again", provider)
                    boundary(conn, run_id, "verifying", rnd=rnd + 1)
                    pending = {}
                    continue
                if ok and not unwitnessed and decision == "APPROVE":
                    if steer_pending(conn, run_id):
                        attempt.close("pass", "again", sha)
                        sha, pending = _steer_approved(
                            project, conn, run_id, provider, task_id, wt, beat_s,
                            sha, ticket, budget_min, rnd, verdict), {}
                        continue
                    attempt.close("pass", "next", sha)
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
                                        sha, rnd, stale, pending, ok, out,
                                        verify_cmd, witness)
                    attempt.close("fail", "again", sha)
                    continue

        with attempt:
            _check_run_cap(project, conn, run_id, budget_min, sha)
        attempt.close("fail", "on_fail", sha)
        boundary(conn, run_id, "addressing", rnd=rnd, ok=ok,
                 out=str(out), verdict=verdict, verify_cmd=verify_cmd)
        pending = {}
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        with record.Attempt(conn, run_id, "implement", sha, {"kind": "fix"},
                            wt=wt) as fixing:
            head = _fix_round(project, conn, run_id, provider, task_id, branch,
                              wt, beat_s, sha, ticket, reviewed, verdict,
                              criteria, budget_min, rnd, fixing)
            fixing.close_at_head("pass", "next")
        sha = fix_self_check.after_fix(
            project, conn, run_id, beat_s, wt, budget_min, reviewed, sha, head,
            verify_cmd, partial(_verify, project, conn, run_id, beat_s, wt,
                                base_sha, reviewed, verify_cmd, contracts,
                                rnd + 1))
    return sha, rnd, False


def _fix_round(project, conn, run_id, provider, task_id, branch, wt, beat_s,
               sha, ticket, reviewed, verdict, criteria, budget_min, rnd,
               attempt):
    from holophyte.agents.fix_session import fix_turn
    states = ticket_template.parse(ticket).evidence_states
    spec = capture_spec_digest(project, wt, task_id, states)
    fixes, timed_out = fix_turn(
        project, conn, run_id, beat_s, wt, budget_min, reviewed,
        without_follow_ups(without_refuted(verdict)), sha, timed=_timed,
        check_cap=_check_run_cap,
        criteria=numbered(conn, run_id, criteria), attempt=attempt)
    ledger(conn, run_id, task_id, "round",
           f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
           f"Reviewer findings:\n{verdict}\n\n"
           f"Implementer response:\n{fixes}", provider)
    contradiction.park(conn, run_id, provider, task_id, wt, "fix", fixes,
                       timed_out, ticket)
    boundary(conn, run_id, "verifying", rnd=rnd + 1)
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if timed_out or (head == sha and capture_spec_digest(
            project, wt, task_id, states) == spec):
        usage = getattr(fixes, "usage", None) or {}
        exit_code = getattr(fixes, "exit_code", None)
        if (not timed_out and exit_code not in (None, 0)
                and usage.get("input_tokens") == 0
                and usage.get("output_tokens") == 0):
            raise InfraFailure(
                f"fix turn exited {exit_code} without using a token; "
                f"branch {branch} preserved at {sha}", "infra")
        print(f"[holo2] fix round timed out or made no progress; "
              f"leaving branch {branch} at {sha} for a human.")
        findings = store.read.rounds_of(conn, run_id)[-1].findings if conn else None
        pending = json.loads(findings) if findings else []
        raise RunFailure(failure_reason.fix_round(
            pending, timed_out, f"branch {branch} preserved at {sha[:12]}"))
    return head
