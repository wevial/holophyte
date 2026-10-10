import json
import subprocess
from dataclasses import replace
from time import monotonic, time

import store
import store.read
import ticket_template
from holophyte import failure_reason
from holophyte.agents.review_workspace import review_refs
from holophyte.agents.roles import agent_route
from holophyte.babysit import babysitter, maintainer_notes, thread_mentions
from holophyte.babysit.babysit_steps import record_step
from holophyte.babysit.bot_threads import route_bot_threads
from holophyte.babysit.check_fix import CheckFix, fix_checks_or_park
from holophyte.babysit.main_checkout import detached_main
from holophyte.babysit.thread_answers import answer_asks, post
from holophyte.babysit.thread_findings import thread_finding
from holophyte.babysit.thread_text import (  # noqa: F401
    COMMENT_HEADER,
    CONVENTIONS_CAP,
    VERDICTS,
    addressed_reply,
    adjudication_brief,
    conventions,
    conventions_paragraph,
    conversation,
    declined_reply,
    fix_brief,
    gist,
    open_threads_question,
    parse_summaries,
    parse_verdicts,
    people_paragraph,
    quoted,
    round_reply,
    route_of,
    thread_line,
    where,
)
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config, verify_config
from holophyte.loop.gates import (
    InfraFailure,
    RunFailure,
    VerificationOutput,
    drop_candidate_modules,
    record_unreviewed_verification,
    run_verify,
    sh,
    with_baseline,
)
from holophyte.loop.run import Run
from holophyte.loop.runs import heartbeat_while, record_round
from holophyte.loop.stop import boundary, fix_state, stop_if_requested
from holophyte.pr import github, merge_queue, pr_ready, pr_status
from holophyte.pr.missing_checks import Retrigger, unreported
from holophyte.pr.pr_head import _just_pushed_state, _pr_terminal
from holophyte.pr.pr_media import capture_spec_digest
from holophyte.redact import safe_print as print
from holophyte.review.briefs import (
    covering_scope,
    criteria_brief,
    evidence_brief,
    main_merge_base,
    pr_description_brief,
    scope_brief,
    scope_files,
    stale_approval_brief,
    tests_brief,
)
from holophyte.review.reply_parsing import (
    _review_reply,
    criteria_findings,
    parse_findings,
    stale_approvals,
)
from holophyte.review.stale_approval import stale_again, stale_rereview


def _merge_origin_main(project, conn, run_id, provider, task_id, branch, wt,
                       sha, beat_s, pull, budget_min, reviewed=None, refusal=None,
                       previous=None, refresh=None, verify_cmd=None, contracts=(),
                       ticket=""):
    from holophyte.loop.claim import merge_conflicts
    from holophyte.loop.implement import _timed
    from holophyte.loop.merge_gate import _is_ancestor, _merge_ref, merge_conflict_goal
    from holophyte.pr.pullrequest import _park_on_pr
    with heartbeat_while(conn, run_id, beat_s):
        fetched = subprocess.run(["git", "fetch", github.REMOTE], cwd=wt,
                                 capture_output=True, text=True)
    ref = f"{github.REMOTE}/{github.BASE}"
    if fetched.returncode != 0 or subprocess.run(
            ["git", "rev-parse", "--verify", "-q", ref], cwd=wt,
            capture_output=True).returncode != 0:
        raise InfraFailure(f"git fetch {github.REMOTE} did not deliver {ref}"
                           f" for the conflicting {pull.url}:"
                           f" {(fetched.stderr or fetched.stdout).strip()}"
                           f"; branch {branch} preserved at {sha[:12]}")
    # Pinned once: worktrees share `refs/remotes`, so a fetch elsewhere
    # during the turn may move the ref off the main the turn resolved.
    main_sha = sh(["git", "rev-parse", ref], wt)
    record_step(conn, run_id, "conflict_merge")
    before = _diff_identity(wt, ref)
    status, detail = _merge_ref(wt, main_sha)
    if status == "conflicted":
        _timed(project, conn, run_id, beat_s, wt, budget_min,
               merge_conflict_goal(branch, pull, detail))
        still = merge_conflicts(wt)
        if still or not _is_ancestor(wt, main_sha, "HEAD"):
            if still:
                subprocess.run(["git", "merge", "--abort"], cwd=wt,
                               capture_output=True, text=True)
            _park_on_pr(
                project, conn, run_id, provider, task_id, branch, sha, pull,
                (f"GitHub refused the merge: {refusal}; " if refusal else "")
                + f"GitHub reported the pull request conflicting; merging"
                f" {github.BASE} into {branch} stopped on"
                f" {', '.join(still or detail)} and the implementer turn"
                " left it unresolved", (), reviewed=reviewed)
        merged = sh(["git", "rev-parse", "HEAD"], wt)
    elif status == "ancestor":
        # Push anyway so GitHub recomputes a stale answer at the merged sha.
        merged = sha
    else:
        merged = detail
    with heartbeat_while(conn, run_id, beat_s):
        stop_if_requested(conn, run_id, "merge_gate")
        github.push_branch(project, branch)
    merged = sh(["git", "rev-parse", branch], wt)
    print(f"[holo2] pushed {branch} to {github.REMOTE} at {merged[:12]}"
          " after the conflict merge")
    if merged != sha:
        note = (f"Merged main into {branch} at {merged} (GitHub reported"
                " a conflict)")
        if conn is not None and run_id is not None:
            store.record_ledger(conn, run_id, "note", note)
    merged = _verify_main_refresh(
        project, conn, run_id, provider, task_id, branch, wt, merged, beat_s,
        pull, budget_min, verify_cmd, contracts, ticket, ref)
    state = _just_pushed_state(project, conn, run_id, provider, task_id, branch,
                               merged, beat_s, pull, reviewed)
    if merged != sha and before == _diff_identity(wt, ref):
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "pull_request",
                               f"main refreshed at {merged}; diff to main unchanged,"
                               " review and quiet carried forward")
        if reviewed == sha:
            reviewed = merged
        if previous is not None and refresh is not None:
            quiet_at = refresh.get((sha, previous.updated_at), previous.updated_at)
            refresh.clear()
            refresh[(merged, state.updated_at)] = quiet_at
    return merged, state, reviewed


def _refresh_verify(project, conn, run_id, beat_s, wt, sha, command, contracts):
    started = int(time() * 1000)
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(command, wt, contracts,
                             verify_config(project).timeout_sec, conn=conn,
                             run_id=run_id, project=project)
        ok, out = with_baseline(project, wt, command, ok, out, conn, run_id)
    out.results = [dict(row, output=f"Tree {sha}\n{row['output']}")
                   for row in out.results]
    record_round(project, conn, run_id, _next_round(conn, run_id), "review",
                 "VERDICT: " + ("APPROVE" if ok else "REQUEST_CHANGES"),
                 command, ok, VerificationOutput(f"Tree {sha}\n{out}", out.results),
                 started_at=started,
                 route="mechanical:main-refresh")
    return ok, out


def _verify_detached_main(project, conn, run_id, beat_s, wt, ref, command, contracts):
    sha = sh(["git", "rev-parse", ref], wt)
    with detached_main(project, conn, run_id, beat_s, wt, sha) as (
            detached, setup_failure):
        if setup_failure is not None:
            return sha, None, setup_failure
        command, skipped = drop_candidate_modules(command, wt, detached)
        if skipped and conn is not None and run_id is not None:
            store.record_event(
                conn, run_id, "verification",
                f"main-side verify at {sha[:12]} skipped"
                f" {', '.join(skipped)}: exists only on the candidate,"
                " not on main, so main cannot import it")
        ok, out = _refresh_verify(project, conn, run_id, beat_s, detached,
                                  sha, command, contracts)
    return sha, ok, out


def _verify_main_refresh(project, conn, run_id, provider, task_id, branch, wt,
                         sha, beat_s, pull, budget_min, command, contracts, ticket,
                         ref):
    from holophyte.pr.pullrequest import _park_on_pr
    ok, out = _refresh_verify(project, conn, run_id, beat_s, wt, sha, command,
                              contracts)
    if ok:
        return sha
    main_sha, main_ok, main_out = _verify_detached_main(
        project, conn, run_id, beat_s, wt, ref, command, contracts)
    if main_ok is False:
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    f"main is red at {main_sha}; verify command: {command}\n"
                    f"Merged tree:\n{out}\nMain:\n{main_out}\n"
                    "Fix main and send this run back through babysit.", ())
    main_state = ("passes. " if main_ok else "was not verified because its"
                  f" worktree setup failed:\n{main_out}\n")
    goal = (f"The merge of main introduced a verification failure on {pull.url}; "
            f"main at {main_sha} {main_state}Failing verify command: {command}\n"
            f"{out}\n"
            f"The ticket is the contract:\n{ticket}\n"
            "Fix this failure on this branch and commit; keep the ticket's verify "
            "commands passing. This is one implementer fix turn.")
    return _fix_threads(project, conn, run_id, provider, task_id, branch, wt, sha,
                        beat_s, pull, (), None, ticket, command, contracts,
                        budget_min, _next_round(conn, run_id),
                        review_follows=_fixes_reviewed(merge_config(project)),
                        goal=goal)


def _diff_identity(wt, ref):
    diff = subprocess.run(["git", "diff", f"{ref}...HEAD"], cwd=wt,
                          capture_output=True, check=True).stdout
    return subprocess.run(["git", "patch-id", "--stable"], cwd=wt, input=diff,
                          capture_output=True, check=True).stdout


def _babysit(run, *args, **kwargs):
    legacy = not isinstance(run, Run)
    if legacy:
        (conn, run_id, provider, task_id, issue_id, task, branch, wt, sha,
         beat_s, url, ticket, verify_cmd, contracts, budget_min, *rest) = args
        run = Run(run, conn, run_id, provider, task_id, issue_id, task,
                  branch, wt, budget_min, monotonic(), sha=sha, pr_url=url)
        args = (beat_s, ticket, verify_cmd, contracts, *rest)
    result = _babysit_pass(run, *args, **kwargs)
    return result.merge_sha if legacy else result


def _pull_of(url, branch, sha):
    pull = pr_status.parse_pr_url(url)
    if pull is None:
        raise RunFailure(f"cannot read a pull request off {url!r};"
                         f" branch {branch} preserved at {sha[:12]}")
    return pull


def _babysit_pass(run, beat_s, ticket, verify_cmd, contracts, criteria=(),
                   approved=False, reviewed=None, verified=None, fix_note=None,
                   just_pushed=False):
    """Human approval covers only the released sha; later fixes are reviewed."""
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, issue_id = run.task_id, run.issue_id
    branch, wt, sha = run.branch, run.wt, run.sha
    budget_min, url = run.budget_min, run.pr_url
    from holophyte.pr.pullrequest import _park_on_pr
    merge = merge_config(project)
    pull = _pull_of(url, branch, sha)
    contract = ticket
    model = agent_route(project, "adjudicate")
    pushed_state = (_just_pushed_state(
        project, conn, run_id, provider, task_id, branch, sha, beat_s, pull,
        reviewed) if just_pushed else None)
    refresh = {}  # Only the known main-refresh update inherits the quiet clock.
    check_fix = CheckFix()  # One rerun, one fix per babysit: red cannot loop.
    pass_no = refreshes = 0
    marked, released = False, None
    while pass_no < merge.pr_rounds and refreshes < merge.pr_main_refreshes:
        pass_no += 1
        stop_if_requested(conn, run_id, "merge_gate")
        retrigger = Retrigger(run, beat_s, pull, sha, reviewed)
        state = _settled_or_park(
            project, conn, run_id, beat_s, pull, pushed_state, provider,
            task_id, branch, sha, reviewed, refresh, retrigger,
            park_ci=sha == run.sha and not check_fix.reran)
        sha, reviewed = retrigger.sha, retrigger.reviewed
        pushed_state = None
        ticket = maintainer_notes.amended_ticket(conn, run_id, contract, url)
        stop_if_requested(conn, run_id, "merge_gate")
        done = _pr_terminal(project, conn, run_id, provider, task_id, branch,
                            sha, pull, state, reviewed)
        if done is not None:
            return replace(run, sha=sha, merge_sha=done)
        state = replace(state, threads=answer_asks(
            project, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
            pull, thread_mentions.classified(state.threads, merge), ticket, reviewed))
        if state.mergeable == "CONFLICTING":
            # UNKNOWN is not a conflict.
            sha, pushed_state, reviewed = _merge_origin_main(
                project, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
                pull, budget_min, reviewed=reviewed, previous=state, refresh=refresh,
                verify_cmd=verify_cmd, contracts=contracts, ticket=ticket)
            pass_no, refreshes = pass_no - 1, refreshes + 1
            continue
        rnd = _next_round(conn, run_id)
        if state.threads:
            sha = _answer_threads(project, conn, run_id, provider, task_id,
                                  branch, wt, sha, beat_s, pull, state, rnd,
                                  pass_no, model, ticket, verify_cmd,
                                  contracts, budget_min, reviewed=reviewed,
                                  criteria=criteria)
            pushed_state = (_just_pushed_state(
                project, conn, run_id, provider, task_id, branch, sha,
                beat_s, pull, reviewed) if sha != state.head_sha else None)
            continue
        reply = babysitter.round_reply(pull, pass_no, (), {}, state.checks, sha)
        record_round(project, conn, run_id, rnd, "review", reply, None, True,
                     "", started_at=int(time() * 1000),
                     route=babysitter.route_of(()))
        ledger(conn, run_id, task_id, "round",
               f"Babysit pass {pass_no} over {pull.url}: no unresolved"
               f" threads, checks {state.checks}", provider)
        if state.checks != "success":
            sha, pushed_state = fix_checks_or_park(
                replace(run, sha=sha), beat_s, pull, state, ticket, verify_cmd,
                contracts, pass_no, reviewed, check_fix)
            pull = replace(pull, awaited=check_fix.awaited)
            continue
        print(f"[holo2] {pull.url} is ready to merge: checks green, no"
              " unresolved threads")
        fixed = sha if sha == reviewed else _review_fix(
            project, conn, run_id, provider, task_id, branch, wt, sha, reviewed,
            beat_s, pull, ticket, verify_cmd, contracts, criteria, fix_note,
            budget_min)
        if fixed != sha:
            fix_note = None  # One fix allowance per babysit, past the cap too.
            sha = reviewed = fixed
            pushed_state = _just_pushed_state(
                project, conn, run_id, provider, task_id, branch, sha,
                beat_s, pull, reviewed)
            continue
        # An `--approve` covered the release, not these reviewed fixes.
        released, reviewed, approved = ((released if marked else sha,
                                         reviewed, approved)
                                        if sha == reviewed else (reviewed, sha, False))
        if state.draft:
            marked = pr_ready.mark_or_park(replace(run, sha=sha), pull, state,
                                           reviewed, marked)
            pushed_state, pass_no = None, pass_no - 1
            continue
        if merge.approve == "auto" or approved:
            try:
                merge_sha = merge_queue.verified_merge(
                    project, conn, run_id, provider, task_id, issue_id, branch,
                    wt, sha, beat_s, pull, reviewed, verified, verify_cmd,
                    contracts, ticket, budget_min, merge.approve == "auto")
                if merge_sha is None:
                    continue
                return replace(run, sha=sha, merge_sha=merge_sha)
            except merge_queue.QueueRemoved as removed:
                sha, pushed_state = fix_checks_or_park(
                    replace(run, sha=sha), beat_s, pull, replace(
                        state, checks="failure", failed_checks=removed.failed),
                    ticket, verify_cmd, contracts, pass_no, reviewed,
                    check_fix, removed.group)
                continue
            except github.MergeRefused as refused:
                verified = sha
                sha, pushed_state, reviewed = _merge_origin_main(
                    project, conn, run_id, provider, task_id, branch, wt, sha,
                    beat_s, pull, budget_min, reviewed=reviewed,
                    refusal=refused, previous=state, refresh=refresh,
                    verify_cmd=verify_cmd, contracts=contracts, ticket=ticket)
                pass_no, refreshes = pass_no - 1, refreshes + 1
                continue
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    _ready(released, sha), (), reviewed=reviewed)
    retrigger = Retrigger(run, beat_s, pull, sha, reviewed)
    state = _settled_or_park(
        project, conn, run_id, beat_s, pull, pushed_state, provider,
        task_id, branch, sha, reviewed, refresh, retrigger)
    sha, reviewed = retrigger.sha, retrigger.reviewed
    _pr_terminal(project, conn, run_id, provider, task_id, branch, sha,
                 pull, state, reviewed)
    cap = (f"pr_main_refreshes = {merge.pr_main_refreshes} main refreshes made"
           if refreshes == merge.pr_main_refreshes
           else f"pr_rounds = {merge.pr_rounds} passes made")
    _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                f"[merge] {cap}; the babysitter stops here", state.threads,
                reviewed=reviewed)


def _fixes_reviewed(merge):
    return merge.approve == "auto" or merge.review_fixes


def _moved(sha, reviewed):
    if reviewed is None:
        return (f"no approval on record covers the candidate at {sha[:12]}"
                " (the last review asked for changes, or the park recorded"
                " none)")
    return (f"the fix rounds moved the candidate from {reviewed[:12]} to"
            f" {sha[:12]}; the release covered {reviewed[:12]}")


def _ready(released, sha):
    covered = ""
    if released != sha:
        covered = (f"fix commits since {released[:12]} reviewed at {sha[:12]}; "
                   if released else f"the candidate reviewed at {sha[:12]}; ")
    return (f"ready to merge; {covered}waiting for a human to say merge"
            " ([merge] approve = \"human\")")


def _fix_answers(conn, run_id, rnd, fix_note):
    lines, rounds = [], store.read.rounds_of(conn, run_id) if conn is not None else []
    for recorded in reversed(rounds):
        if recorded.round >= rnd:
            continue
        if not recorded.reviewerModel.startswith("github:"):
            break
        lines[:0] = [line for finding in json.loads(recorded.findings)
                     for line in (f"ADDRESS: {' '.join(finding['request'].split())}"
                                  if finding.get("kind") == "instruction"
                                  else f"{finding['verdict']}: {finding['summary']}"
                                  if finding.get("kind") == "thread"
                                  else finding["message"]).splitlines()
                     if "ADDRESS:" in line]
    if fix_note:
        lines.append(f"Operator babysit note: {fix_note}")
    return "\n".join(lines)


def _covering_review(project, conn, run_id, provider, task_id, branch, wt, sha,
                     reviewed, beat_s, pull, ticket, verify_cmd, criteria, ok,
                     out, stale=()):
    from holophyte.loop.review_round import _verify_brief, agent, set_phase
    from holophyte.pr.pullrequest import _park_on_pr
    set_phase(conn, run_id, "reviewing", f"review of the fix at {sha[:12]}")
    record_step(conn, run_id, "covering_review")
    base_sha = main_merge_base(wt, sha)
    rnd = _next_round(conn, run_id)
    round_started = int(time() * 1000)
    # Only what the covered range changes, less what a merged `main` alone
    # brought, is put to the scope question.
    covered = reviewed or base_sha
    scope = scope_files(wt, ticket, covered, sha, candidate_only=True)
    with heartbeat_while(conn, run_id, beat_s):
        evidence = evidence_brief(project, wt, task_id,
                                  ticket_template.parse(ticket).evidence_states)
        described = pr_description_brief(project, pull, bool(evidence))
        verdict, decision, first_reply = _review_reply(project,
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            f"{review_refs(run_id)[0]} as the frozen base and {review_refs(run_id)[1]} "
            "as the candidate in this repo against the ticket below. The "
            + covering_scope(wt, reviewed, sha, pull.url) + described
            + "The ticket is "
            "the contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + _verify_brief(verify_cmd, ok, out)
            + criteria_brief(criteria)
            + stale_approval_brief(stale)
            + tests_brief(wt)
            + scope_brief(wt, ticket, covered, sha, candidate_only=True)
            + evidence
            + "Do not modify anything. End your reply with exactly one "
            "line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha, sha, conn, run_id, run_agent=agent)
    record_round(project, conn, run_id, rnd, "review", verdict, verify_cmd,
                 ok, out, started_at=round_started, criteria=criteria,
                 root=wt, prior_reply=first_reply,
                 approved_range=(reviewed, sha) if reviewed else None,
                 scope=scope)
    stop_if_requested(conn, run_id, "merge_gate")
    if decision == "MALFORMED":
        reason = "the reviewer gave no verdict after one reminder"
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "route_failure", reason)
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha,
                    pull, reason, ())
    # A criterion not met or unwitnessed blocks whatever the verdict says.
    unwitnessed = criteria_findings(
        verdict, criteria, wt, approved_range=(reviewed, sha) if reviewed else None,
        scope=scope)
    if unwitnessed:
        print(f"[holo2] round {rnd}: {len(unwitnessed)} criteria not "
              "witnessed by the review of the fix; treating as "
              "REQUEST_CHANGES")
        verdict += "\n\n" + "\n".join(f["message"] for f in unwitnessed)
    return verdict, decision, unwitnessed, rnd


def _review_fix(project, conn, run_id, provider, task_id, branch, wt, sha,
                reviewed, beat_s, pull, ticket, verify_cmd, contracts,
                criteria=(), fix_note=None, budget_min=None, *, fix_context=""):
    from holophyte.loop.review_round import set_phase
    from holophyte.pr.pullrequest import _park_on_pr, refresh_pr_text
    set_phase(conn, run_id, "verifying", f"verify the fix at {sha[:12]}"
              " before its review")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts,
                             verify_config(project).timeout_sec, conn=conn,
                             run_id=run_id, project=project)
        ok, out = with_baseline(project, wt, verify_cmd, ok, out,
                               conn, run_id)
    stop_if_requested(conn, run_id, "merge_gate")
    merge = merge_config(project)
    auto = merge.approve == "auto"
    if not ok:
        # Park under either mode so `--babysit --note` can send a fix.
        record_unreviewed_verification(conn, run_id, out)
        if auto:
            ledger(conn, run_id, task_id, "failure",
                   f"FAILED verify before the review of the fix at {sha} on"
                   f" {pull.url}; branch {branch} preserved, not merged\n\n{out}",
                   provider)
        reason = failure_reason.verify(
            out, verify_cmd, f"before the review of the fix on {pull.url}"
            if auto else "before human approval")
        failure_reason.record(conn, run_id, reason)
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha,
                    pull, reason, (),
                    reviewed=reviewed)
    if not _fixes_reviewed(merge):
        recovered = _fix_answers(conn, run_id, _next_round(conn, run_id), fix_note)
        answered = "\n".join(part for part in (fix_context, recovered) if part)
        if not answered:
            base = reviewed or main_merge_base(wt, sha)
            answered = sh(["git", "log", "--format=%s", f"{base}..{sha}"], cwd=wt)
        refresh_pr_text(project, conn, run_id, task_id, ticket.splitlines()[0],
                        branch, ticket, beat_s, wt, budget_min, pull, answered, sha=sha)
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha,
                    pull, f"{_moved(sha, reviewed)}, and a human"
                    " says merge on the candidate as it stands"
                    " ([merge] approve = \"human\")", (),
                    reviewed=reviewed)
    review = (project, conn, run_id, provider, task_id, branch, wt, sha,
              reviewed, beat_s, pull, ticket, verify_cmd, criteria, ok, out)
    verdict, decision, unwitnessed, rnd = _covering_review(*review)
    stale = stale_approvals(decision, unwitnessed)
    if stale:
        stale_rereview(conn, run_id, provider, task_id, sha, rnd, stale)
        verdict, decision, unwitnessed, rnd = _covering_review(*review, stale)
        stale = stale_approvals(decision, unwitnessed)
    recovered = _fix_answers(conn, run_id, rnd, fix_note)
    answered = "\n".join(part for part in (fix_context, recovered) if part)
    if not unwitnessed and decision == "APPROVE":
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: APPROVE of the fix at {sha} on {pull.url}\n"
               f"Reviewer verdict:\n{verdict}", provider)
        print(f"[holo2] the fix at {sha[:12]} is approved")
        if not answered:
            answered = sh(["git", "log", "--format=%s",
                           f"{reviewed or main_merge_base(wt, sha)}..{sha}"],
                          cwd=wt)
        refresh_pr_text(project, conn, run_id, task_id, ticket.splitlines()[0],
                        branch, ticket, beat_s, wt, budget_min, pull, answered, sha=sha)
        return sha
    ledger(conn, run_id, task_id, "round",
           f"Round {rnd}: REQUEST_CHANGES on the fix at {sha} on"
           f" {pull.url}; not merged\nReviewer findings:\n{verdict}",
           provider)
    stale_again(branch, sha, stale)
    if fix_note is not None and (unwitnessed or
            decision == "REQUEST_CHANGES"):
        goal = (f"Fix the pre-merge review findings on {pull.url}. The ticket"
                f" is the contract:\n\n{ticket}\n\nReviewer findings:\n{verdict}"
                f"\n\nOperator babysit note:\n{fix_note}\n\n"
                "Fix the blockers on this branch and commit; keep the ticket's"
                " verify commands passing.")
        ledger(conn, run_id, task_id, "round",
               f"Babysit review fix allowance after round {rnd}: one fix and"
               " recorded re-review, even if reviewRoundCap is spent.", provider)
        fixed = _fix_threads(project, conn, run_id, provider, task_id, branch,
                             wt, sha, beat_s, pull, (), None, ticket, verify_cmd,
                             contracts, budget_min, rnd, review_follows=True, goal=goal,
                             reviewed_by_caller=True)
        return _review_fix(project, conn, run_id, provider, task_id, branch, wt,
                           fixed, None, beat_s, pull, ticket, verify_cmd,
                           contracts, criteria, budget_min=budget_min,
                           fix_context=f"{answered}\n{verdict}")
    # No `reviewed`: the judgement on record is this rejection, so the
    # resume that follows reviews the candidate again before any merge.
    _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                f"the review of the fix at {sha[:12]} asked for changes;"
                f" not merged. Reviewer findings:\n{verdict}", ())


def _next_round(conn, run_id):
    return len(store.read.rounds_of(conn, run_id)) + 1 if conn else 1


def _quiet_left(state, quiet_ms, refresh=None):
    key = (state.head_sha, state.updated_at)
    if refresh and key not in refresh:
        refresh.clear()  # A later update or another head is real activity.
    updated_at = (refresh or {}).get(key, state.updated_at)
    if updated_at is None:
        return quiet_ms
    return max(0, quiet_ms - (int(time() * 1000) - updated_at))


def _settled_or_park(project, conn, run_id, beat_s, pull, state, provider,
                     task_id, branch, sha, reviewed, refresh=None,
                     retrigger=None, deadline=None, park_ci=False):
    from holophyte.pr.pullrequest import _park_on_pr
    try:
        state = state or pr_status.pr_state(project, pull)
        state = maintainer_notes.pending_state(conn, run_id, state, pull.url)
        return _settled_state(project, conn, run_id, beat_s, pull, state,
                              refresh, retrigger, deadline, park_ci)
    except WaitsOnCI as waiting:
        stop_if_requested(conn, run_id, "merge_gate")
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha,
                    pull, str(waiting), (), reviewed=reviewed, park_kind="ci")
    except WaitExpired as expired:
        if retrigger is not None:
            sha, reviewed = retrigger.sha, retrigger.reviewed
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha,
                    pull, str(expired), (), reviewed=reviewed)


class WaitExpired(Exception):
    pass


class WaitsOnCI(Exception):
    pass


def _settled_state(project, conn, run_id, beat_s, pull, state=None, refresh=None,
                   retrigger=None, deadline=None, park_ci=False):
    merge = merge_config(project)
    quiet_ms = merge.pr_quiet_sec * 1000
    deadline = deadline or monotonic() + merge.check_wait_sec
    absent = {}
    with heartbeat_while(conn, run_id, beat_s):
        state = state or pr_status.pr_state(project, pull)
        state = route_bot_threads(project, conn, run_id, beat_s, pull, state, merge)
        while (not state.threads and not state.merged and not state.closed
               and state.mergeable != "CONFLICTING"):
            if state.checks == "pending":
                late = unreported(state, absent, merge.missing_check_sec,
                                  monotonic)
                if late:
                    state = retrigger(late) if retrigger else None
                    if state is None:
                        raise WaitExpired(
                            "required checks never reported on the head"
                            f" commit: {', '.join(late)}")
                    park_ci = False
                    state = route_bot_threads(project, conn, run_id, beat_s,
                                              pull, state, merge)
                    continue
                record_step(conn, run_id, "checks")
                reason = "pending checks"
                if state.pending_contexts:
                    reason += f" ({', '.join(state.pending_contexts)})"
                if park_ci and not state.missing_checks:
                    raise WaitsOnCI(reason)
                nap = github.CHECK_POLL_S
                print(f"[holo2] checks pending on {pull.url}; waiting"
                      f" {nap}s")
            elif state.checks == "success" \
                    and (left := _quiet_left(state, quiet_ms, refresh)):
                record_step(conn, run_id, "quiet")
                reason = "quiet wait"
                if park_ci:
                    raise WaitsOnCI(reason)
                nap = min(merge.pr_poll_sec, left / 1000)
                print(f"[holo2] {pull.url} is green and quiet for"
                      f" {(quiet_ms - left) // 1000}s of the"
                      f" {quiet_ms // 1000}s required; waiting {nap}s")
            else:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise WaitExpired(
                    f"{reason} exceeded {merge.check_wait_sec}s on the pull request")
            stop_if_requested(conn, run_id, "merge_gate")
            github.SLEEP(min(nap, remaining))
            state = maintainer_notes.pending_state(
                conn, run_id, pr_status.pr_state(project, pull), pull.url)
            state = route_bot_threads(project, conn, run_id, beat_s, pull, state, merge)
    return state


def _answer_threads(project, conn, run_id, provider, task_id, branch, wt, sha,
                    beat_s, pull, state, rnd, pass_no, model, ticket,
                    verify_cmd, contracts, budget_min, reviewed=None, criteria=()):
    """Only configured bots' or `[bot]` logins' declines are resolved."""
    from holophyte.loop.review_round import agent
    from holophyte.pr.pullrequest import _park_human, _park_on_pr
    merge = merge_config(project)
    record_step(conn, run_id, "threads")
    threads = thread_mentions.classified(state.threads, merge)
    base_sha = main_merge_base(wt, sha)
    round_started = int(time() * 1000)
    act = merge.human_threads == "act"
    judged = tuple(t for t in threads if not maintainer_notes.is_note(t)
                   and t.classification != "MENTIONED"
                   and (act or t.author_kind == "bot"))
    reply = "(no bot opened a thread; the adjudicator was not asked)"
    if judged:
        with heartbeat_while(conn, run_id, beat_s):
            reply = agent(project, "adjudicate",
                          babysitter.adjudication_brief(
                              pull, judged, ticket, sha,
                              babysitter.conventions(wt), run_id=run_id), wt, conn=conn,
                          base_sha=base_sha, candidate_sha=sha, run_id=run_id)
    stop_if_requested(conn, run_id, "merge_gate")
    verdicts = _verdicts_by_kind(
        threads, judged, babysitter.parse_verdicts(reply, len(judged)))
    record_round(project, conn, run_id, rnd, "review",
                 babysitter.round_reply(pull, pass_no, threads, verdicts,
                                      state.checks, sha),
                 None, True, "", started_at=round_started,
                 route=babysitter.route_of(threads),
                 structured_findings=_thread_findings(
                     pull, pass_no, threads, verdicts, state.checks, sha,
                     merge.bot_authors + merge.bot_logins))
    people = sum(t.author_kind not in ("bot", "maintainer")
                 and t.classification != "MENTIONED" for t in threads)
    ledger(conn, run_id, task_id, "round",
           f"Babysit pass {pass_no} over {pull.url}: {len(threads)}"
           f" unresolved thread(s), checks {state.checks}\n"
           + (f"{people} opened by a person, HUMAN"
              " before the adjudicator was asked\n" if not act else
              f"{people} opened by a person, judged (human_threads = act):"
              " ADDRESS is fixed and"
              " answered, anything else is HUMAN\n")
           + f"Adjudicator verdicts:\n{reply}", provider)
    by_verdict = {v: [(n, t, verdicts[n][1]) for n, t in
                      enumerate(threads, 1) if verdicts[n][0] == v]
                  for v in babysitter.VERDICTS}
    # A HUMAN on a bot's thread parks before anything is posted; a person's
    # HUMAN under `act` waits until the rest are fixed and answered.
    if by_verdict["HUMAN"] and (not act or any(
            t.author_kind == "bot" for _, t, _ in by_verdict["HUMAN"])):
        _park_human(project, conn, run_id, provider, task_id, branch, sha, pull,
                    by_verdict["HUMAN"], threads, reviewed)
    thread_mentions.acknowledge(
        project, conn, run_id, pull,
        [t for _, t, _ in by_verdict["ADDRESS"]
         if t.classification == "MENTIONED" and not maintainer_notes.is_note(t)],
        merge)
    if by_verdict["ADDRESS"]:
        sha = _fix_threads(project, conn, run_id, provider, task_id, branch,
                           wt, sha, beat_s, pull, by_verdict["ADDRESS"],
                           model, ticket, verify_cmd, contracts, budget_min,
                           pass_no, criteria=criteria,
                           review_follows=_fixes_reviewed(merge_config(project)))
    declined_open = _decline_threads(project, conn, run_id, beat_s, pull,
                                     by_verdict["DECLINE"], model)
    left_open = declined_open + tuple(
        t for _, t, _ in by_verdict["ADDRESS"]
        if t.author_kind != "bot" and not maintainer_notes.is_note(t)
        and t.classification != "MENTIONED")
    if by_verdict["HUMAN"]:
        _park_human(project, conn, run_id, provider, task_id, branch, sha, pull,
                    by_verdict["HUMAN"],
                    tuple(t for _, t, _ in by_verdict["HUMAN"]) + left_open,
                    reviewed)
    if left_open:
        declined = len(declined_open)
        answered = len(left_open) - declined
        why = [f"{declined} thread(s) declined and left open for their"
               " authors"] if declined else []
        why += [f"{answered} person's thread(s) addressed and left open for"
                " them to close"] if answered else []
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    "; ".join(why), left_open, reviewed=reviewed)
    return sha


def _decline_threads(project, conn, run_id, beat_s, pull, declined, model):
    bot_authors = merge_config(project).bot_authors
    left_open = []
    for _, thread, reason in declined:
        resolve = thread.author.endswith("[bot]") or thread.author in bot_authors
        _post(project, conn, run_id, beat_s, pull, thread,
              babysitter.declined_reply(model, reason), resolve=resolve)
        if not resolve:
            left_open.append(thread)
    if declined:
        print(f"[holo2] {len(declined)} thread(s) declined;"
              f" {len(declined) - len(left_open)} from bots resolved with the reason")
    return tuple(left_open)


def _thread_findings(pull, pass_no, threads, verdicts, checks, sha, bot_logins):
    findings = []
    for n, thread in enumerate(threads, 1):
        if thread.classification == "MENTIONED":
            author = thread.comments[-1]
            is_bot = thread_mentions.bot_author(
                author.author, bot_logins, author.author_kind)
            finding = dict(kind="instruction", path=thread.path or "(no file)",
                                 line=thread.line, author=author.author,
                                 request=thread.request, url=thread.url,
                                 severity="nit", message=thread.request,
                                 **({"triage": thread.triage} if thread.triage else {}))
            findings.append(thread_finding(thread, verdicts[n], finding, bot_logins)
                            if is_bot else finding)
        else:
            reply = babysitter.round_reply(
                pull, pass_no, (thread,), {1: verdicts[n]}, checks, sha)
            legacy, = parse_findings(reply.rsplit("\n", 1)[0])
            findings.append(thread_finding(thread, verdicts[n], legacy, bot_logins))
    return findings


def _verdicts_by_kind(threads, judged, parsed):
    """The factory never declines a person: that folds to `HUMAN`."""
    pending = iter(sorted(parsed))
    verdicts = {}
    for n, t in enumerate(threads, 1):
        if maintainer_notes.is_note(t):
            verdicts[n] = ("ADDRESS",
                           f"operator_note event {maintainer_notes.event_id(t)}")
            continue
        if t.classification == "MENTIONED":
            verdicts[n] = ("ADDRESS", t.request)
            continue
        if t not in judged:
            verdicts[n] = ("HUMAN", "opened by a person")
            continue
        verdict = parsed[next(pending)]
        if t.author_kind != "bot" and verdict[0] != "ADDRESS":
            verdict = ("HUMAN",
                       "a person's thread the adjudicator would not address")
        verdicts[n] = verdict
    return verdicts


def _spec_digest(project, wt, task_id, ticket):
    return capture_spec_digest(project, wt, task_id,
                               ticket_template.parse(ticket or "").evidence_states)


def _review_spec_only_fix(project, conn, run_id, provider, task_id, branch, wt,
                          sha, fixed, beat_s, pull, ticket, verify_cmd,
                          contracts, criteria, budget_min, fixes,
                          reviewed_by_caller):
    if fixed == sha and not reviewed_by_caller:
        _review_fix(project, conn, run_id, provider, task_id, branch, wt, sha,
                    None, beat_s, pull, ticket, verify_cmd, contracts, criteria,
                    budget_min=budget_min, fix_context=str(fixes))


def _fix_threads(project, conn, run_id, provider, task_id, branch, wt, sha,
                 beat_s, pull, addressed, model, ticket, verify_cmd,
                 contracts, budget_min, pass_no, *, review_follows, goal=None,
                 resume_step=None, no_commit_why=None, reviewed=None,
                 criteria=(), reviewed_by_caller=False):
    from holophyte.loop.branch_sync import _candidate_drift
    from holophyte.loop.implement import _record_implementer_output, _transport_timed
    from holophyte.pr.pullrequest import _park_on_pr
    from holophyte.redact import known_secrets, outbound, redact_prose
    if resume_step is None:
        record_step(conn, run_id, "fix")
        maintainer_notes.start_fix(conn, run_id, addressed)
        spec = _spec_digest(project, wt, task_id, ticket)
        fixes, timed_out = _transport_timed(
            project, conn, run_id, beat_s, wt, budget_min,
            goal or babysitter.fix_brief(pull, addressed, ticket))
        saved = dict(fix_state(sha, fixes, timed_out, addressed, model,
                               pass_no, review_follows), spec=spec)
    else:
        saved = dict(resume_step)
        fixes, timed_out = saved["fixes"], saved["timed_out"]
    boundary(conn, run_id, "merge_gate", **saved)
    fixed = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if fixed == sha:
        _record_implementer_output(conn, run_id, f"fix round {pass_no}: {fixes}",
                                   known_secrets(project.config()))
    summaries = babysitter.parse_summaries(redact_prose(fixes, assignments=True))
    spec_moved = "spec" in saved and _spec_digest(
        project, wt, task_id, ticket) != saved["spec"]
    if (addressed and fixed == sha and not timed_out and not spec_moved
            and all(summaries.get(n) for n, _, _ in addressed)
            and not _candidate_drift(wt, branch, fixed)):
        why = "Fix round made no commit; operator instruction needed:\n" + "\n".join(
            f"THREAD {n} -- {where(thread)}:\n> {summaries[n]}"
            for n, thread, _ in addressed)
        why = outbound(why, known_secrets(project.config()))
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull, why,
                    (), park_kind="fix_declined")
    if no_commit_why and fixed == sha and not timed_out:
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    no_commit_why, (), reviewed=reviewed)
    if timed_out or (fixed == sha and not spec_moved):
        raise RunFailure(failure_reason.fix_round(
            [{'message': thread.body} for _, thread, _ in addressed], timed_out,
            f"for {pull.url}; branch {branch} preserved at {sha[:12]}"))
    unclean = _candidate_drift(wt, branch, fixed)
    if unclean:
        ledger(conn, run_id, task_id, "failure",
               f"FAILED after the fix round for {pull.url}: the fix is not"
               f" one clean commit -- {unclean}\nBranch {branch} preserved"
               f" at {fixed}, not pushed; nothing was posted or resolved.",
               provider)
        raise RunFailure(f"fix round for {pull.url} left the worktree"
                         f" unclean ({unclean.splitlines()[0]}); branch"
                         f" {branch} preserved at {fixed[:12]}")
    _review_spec_only_fix(
        project, conn, run_id, provider, task_id, branch, wt, sha, fixed,
        beat_s, pull, ticket, verify_cmd, contracts, criteria, budget_min,
        fixes, reviewed_by_caller)
    fixed = maintainer_notes.cite_commits(wt, sha, fixed, addressed, sh)
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts,
                             verify_config(project).timeout_sec, conn=conn,
                             run_id=run_id, project=project)
        ok, out = with_baseline(project, wt, verify_cmd, ok, out,
                               conn, run_id)
    boundary(conn, run_id, "merge_gate", **saved)
    if not ok or not review_follows:
        record_unreviewed_verification(conn, run_id, out)
    if not ok:
        print(f"[holo2] verify FAILED after the fix round for {pull.url};"
              f" leaving branch {branch} at {fixed} for a human:\n{out}")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED verify after the fix round for {pull.url}; branch"
               f" {branch} preserved at {fixed}, not pushed\n\n{out}",
               provider)
        raise RunFailure(failure_reason.verify(
            out, verify_cmd, f"after the fix round for {pull.url}; "
            f"branch {branch} preserved at {fixed[:12]}"))
    with heartbeat_while(conn, run_id, beat_s):
        stop_if_requested(conn, run_id, "merge_gate")
        github.push_branch(project, branch)
    fixed = sh(["git", "rev-parse", branch], wt)
    print(f"[holo2] pushed the fix round to {github.REMOTE} at {fixed[:12]}")
    for index, (n, thread, reason) in enumerate(addressed):
        if index < saved["posted"]:
            continue
        if maintainer_notes.is_note(thread):
            continue
        reply = babysitter.addressed_reply(model, summaries.get(n, reason), fixed)
        _post(project, conn, run_id, beat_s, pull, thread,
              reply,
              resolve=(thread.author_kind == "bot"
                       or thread.classification == "MENTIONED"))
        saved["posted"] = index + 1
        boundary(conn, run_id, "merge_gate", **saved)
    boundary(conn, run_id, "merge_gate")
    return fixed


def _post(project, conn, run_id, beat_s, pull, thread, body, resolve):
    stop_if_requested(conn, run_id, "merge_gate")
    return post(project, conn, run_id, beat_s, pull, thread, body, resolve)
