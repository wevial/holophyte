"""`run_task()`: a claimed run through implement, review, adjudicate and merge."""
from dataclasses import replace
from time import monotonic

import store
import store.read
from holophyte.babysit import maintainer_notes
from holophyte.babysit.babysitter import _babysit
from holophyte.board import projection
from holophyte.config.config_tables import merge_config, sweep_config
from holophyte.loop import run as run_state
from holophyte.loop.adjudicate import _terminal_adjudication
from holophyte.loop.claim import (
    _cut_worktree,
    _setup_worktree,
    claimed_run,
    merge_conflicts,
)
from holophyte.loop.dispatch import SWEPT
from holophyte.loop.gates import InfraFailure, sh
from holophyte.loop.gates import (
    MergeParked as MergeParked,
)
from holophyte.loop.implement import _implement
from holophyte.loop.merge_gate import (
    DriftRequeued,
    _gate_lock,
    _merge_gate,
    _park_for_approval,
    _resume_at_merge_gate,
)
from holophyte.loop.review_round import _review_cap, _review_rounds
from holophyte.loop.runs import RunSwept
from holophyte.loop.stop import Aborted, continuation
from holophyte.loop.trim import trim
from holophyte.pr.pullrequest import _prepare_pr, _push_and_open
from holophyte.redact import safe_print as print
from holophyte.review import reproduce
from holophyte.story.story_claim import story_brief


def run_task(project, task, conn=None, run_id=None, provider=None):
    """Run the claimed stages; a run the store or sweep ended stops with its verdict."""
    try:
        run = project if isinstance(project, run_state.Run) else task.get("_run")
        run = run or claimed_run(
            project, task, conn, run_id, provider, clock=monotonic)
        return _run_stages(run, task)
    except store.IllegalTransition as refused:
        raise InfraFailure(str(refused)) from refused
    except store.RunEnded as ended:
        if ended.outcome == "paused" or isinstance(ended, (Aborted,
                                                           DriftRequeued)):
            ticket_id = store.read.run_snapshot(run.conn, run.run_id).ticketId
            projection.mirror_push(run.conn, ticket_id, run.provider)
            projection.release_lease_label(run.project, run.conn, ticket_id,
                                      run.provider, run.run_id)
            return SWEPT
        print(f"[holo2] run {ended.run_id} was ended by the supervisor"
              f" ({ended.outcome}: {ended.reason}); stopping")
        return ended.outcome == "merged"
    except RunSwept as swept:
        print(f"[holo2] run {swept.run_id} was ended by the supervisor"
              f" ({swept.reason}); stopping this turn")
        return SWEPT


def _run_stages(run, task):
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, issue_id, started = run.task_id, run.issue_id, run.started
    verify_cmd, budget_min = task.get("verify"), task["budget_min"]
    contracts = task.get("contracts")
    body = (task.get("body") or "").strip()
    criteria = list(task.get("criteria") or ())
    issue_url = task.get("url")
    task = task["title"]
    branch, wt = run.branch, run.wt
    carried = _approved_candidate(conn, run_id)
    if carried is not None and wt.exists():
        return _resume_at_merge_gate(
            run, carried, verify_cmd, contracts, body,
            criteria, issue_url=issue_url)
    cut = not wt.exists()
    fresh = _cut_worktree(project, conn, run_id, provider, task_id, task,
                          branch, wt)

    # Half the stale threshold, so one late beat is still inside it.
    beat_s = sweep_config(project).heartbeat_stale_ms / 2000
    _setup_worktree(project, conn, run_id, provider, task_id, task, branch, wt,
                    fresh, beat_s)

    # Preserved commits were never approved, so the review base is main.
    base_sha = sh(["git", "rev-parse", "main"], project.path)
    start_sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    ticket = f"{task}\n\n{body}" if body else task
    if conn is not None:
        ticket += story_brief(project, conn, store.read.run_snapshot(
            conn, run_id).ticketId)
    conflicts = merge_conflicts(wt)
    resume = continuation(conn, run_id)
    test = None if resume or conflicts else reproduce.first_turn(
        project, conn, run_id, provider, task_id, wt, beat_s, start_sha, ticket,
        body, verify_cmd, budget_min)
    if resume and resume["phase"] != "working":
        sha, unreproduced = start_sha, reproduce.routed(resume)
    elif test and not test.failing:
        sha, unreproduced = test.sha, True
    else:
        sha, unreproduced = _implement(
            project, conn, run_id, task_id, task, branch, wt, fresh and not test,
            beat_s, test.sha if test else start_sha, ticket, verify_cmd,
            budget_min, conflicts=conflicts,
            opening=maintainer_notes.requeue_context(conn, run_id)
            + (test.opening() if test else ""), contracts=contracts, cut=cut)
        if not unreproduced:
            sha = trim(project, conn, run_id, beat_s, wt, base_sha, sha,
                       verify_cmd, contracts)

    cap = _review_cap(project, conn, run_id, provider, task_id, wt)
    sha, rnd, approved = (reproduce.review_rounds if unreproduced else _review_rounds)(
        project, conn, run_id, provider, task_id, branch, wt, beat_s, base_sha,
        sha, ticket, verify_cmd, contracts, criteria, budget_min, cap, resume=resume)
    if not approved:
        _terminal_adjudication(project, conn, run_id, provider, task_id, task,
                               branch, wt, beat_s, base_sha, sha, ticket,
                               verify_cmd, contracts, max(cap, rnd), criteria,
                               resume=resume)

    merge = merge_config(project)
    # Under `mode = "pr"` the merge lock covers the push-and-open alone.
    if merge.mode == "pr":
        ok, sha = _merge_gate(project, conn, run_id, provider, task_id,
                              issue_id, branch, wt, beat_s, sha, verify_cmd,
                              contracts, ticket, budget_min,
                              sync_main=False)
        title, text = _prepare_pr(project, conn, run_id, task_id, task, branch,
                                  body, beat_s, wt, started, budget_min,
                                  issue_url)
        with _gate_lock(project, conn, run_id, provider, task_id, branch, sha,
                        beat_s):
            url = _push_and_open(project, conn, run_id, branch, title, text,
                                 beat_s)
        sha = sh(["git", "rev-parse", branch], wt)
    else:
        with _gate_lock(project, conn, run_id, provider, task_id, branch, sha,
                        beat_s):
            ok, sha = _merge_gate(project, conn, run_id, provider, task_id,
                                  issue_id, branch, wt, beat_s, sha,
                                  verify_cmd, contracts, ticket,
                                  budget_min)
            if merge.approve == "human":
                _park_for_approval(conn, run_id, provider, task_id, branch,
                                   sha)
            return run_state.land(replace(run, sha=sha, rnd=rnd), ok)
    run = replace(run, sha=sha, rnd=rnd, pr_url=url)
    run = _babysit(run, beat_s, ticket, verify_cmd, contracts, criteria,
                   reviewed=sha, verified=sha, just_pushed=True)
    return run_state.land(run, True)


def _approved_candidate(conn, run_id):
    if conn is None:
        return None
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    return store.read.approved_candidate(conn, ticket_id, run_id)
