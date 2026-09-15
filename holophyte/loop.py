"""The loop: worktree setup and reuse, `run_task`, `main`, `report`, the re-exec.

`main()` drives one pass of the factory -- claim, mirror, lease, `run_task()`,
close out, repeat -- and re-executes `factory.py` through the `EXEC` seam
(`reexec_self` from `holophyte.reexec`) after merging a change to the factory
itself (`self_hosted()`). Under `[loop] workers > 1` `main()` is instead
`scheduler()`, a pool of `factory.py --worker` children sized to the claimable
queue, each one `worker()`: the same phases once, for one ticket (KO-343).
`run_task()` is the loop body: the worktree (`reuse_leftover()` for a leftover,
`run_worktree_setup()` for the `[worktree] setup` table, checked at startup by
`config.check_worktree_setup()`), the implement/review/adjudicate turns, the verify
gate, the `--no-ff` merge. `report()` is `--report`'s whole body. Imports the
package modules, `store`, `store.read`, `review_runner`, `provider` and the
standard library; nothing from `factory`.

Seventh and last slice of the phase-2 module split; moved verbatim from
`factory.py`, which is now the entry point that imports `holophyte.cli`.
"""
import contextlib
import json
import re
import subprocess
import traceback
from pathlib import Path
from time import monotonic, time

import review_runner
import store
import store.read
from holophyte import pr
from holophyte.agents import agent
from holophyte.babysitter import _babysit
from holophyte.board import (
    block_ticket,
    body_problem,
    close_out_failure,
    ledger,
    merge_drift,
    mirror_key,
    mirror_status,
    mirror_task,
    release_lease_label,
    release_run,
)
from holophyte.claim import (
    _cut_worktree,
    _resolve_merge_conflict,
    _setup_worktree,
    conflict_brief,
    merge_conflicts,
    reuse_leftover,
)
from holophyte.config import (
    branch_prefix,
    budget_scale,
)
from holophyte.config_tables import (
    loop_config,
    merge_config,
    sweep_config,
)
from holophyte.findings import commit_findings, refresh_findings
from holophyte.gates import (
    GroupKill,
    MergeLockHeld,
    MergeParked,
    RunFailure,
    merge_lock,
    outcome_class_of,
    run_verify,
    sh,
)
from holophyte.pullrequest import (
    _landed_pr,
    _open_pr,
    _park_on_pr,
    _resume_on_pr,
)
from holophyte.redact import known_secrets, redact_prose
from holophyte.review import criteria_brief, criteria_findings
from holophyte.runs import (
    RunSwept,
    heartbeat_while,
    record_round,
    review_round_cap,
    set_phase,
    warn_on_run,
)
from holophyte.supervisor import sweep
from holophyte.sweep_report import SWEEP_HINT, sweep_lines
from holophyte.target import worktree_path

# The paths a run works against, plus the config they carry, are a `Target`
# (below): built once by `cli()` from the command line and passed to every
# function that needs one, so the derivation lives in one place and the
# command line is the only thing that chooses a target. Importing this module
# used to read `sys.argv[1]`, which made every `python3 -m unittest discover`
# retarget the factory at a directory called "discover"; now importing it
# chooses no target at all.


def run_task(target, task, conn=None, run_id=None, provider=None):
    """Run `task` through `_run_stages()`, and stop if the store ended the run.

    The one catch for `store.RunEnded`, and the one for `RunSwept`, its
    mid-turn counterpart from `heartbeat_while()` (see the second `except`).
    The supervisor's `act_on_trip()` --
    or an operator's `--sweep --act` -- fails a run, releases its leases and
    records the outcome while this loop is blocked in an agent call and
    cannot know. When the agent returns, the loop's next `set_phase()` is
    refused, and the refusal is the signal: run 39 (KO-213) went
    `failed -> verifying -> reviewing` after its sweep and would have merged
    under a row that said the work had failed. So the run stops here with
    the sweep's verdict and nothing else: no further phase event, no board
    push, no merge, and the worktree and branch are left exactly as they
    are for the sweep's close-out to describe. `main()` counts the failure
    off the row the sweep wrote, so no `RunFailure` is raised for it; the
    result is whatever outcome the row records, which is `merged` only if
    the ender said so.
    """
    try:
        return _run_stages(target, task, conn, run_id, provider)
    except store.RunEnded as ended:
        print(f"[holo2] run {ended.run_id} was ended by the supervisor"
              f" ({ended.outcome}: {ended.reason}); stopping")
        return ended.outcome == "merged"
    except RunSwept as swept:
        # The heartbeat found the run ended mid-turn and killed the turn
        # (KO-339, run 160): the sweep already released the lease and
        # recorded the outcome, so there is nothing to write and nothing
        # to release. `SWEPT` tells `_dispatch()` to skip the close-out and
        # `main()` to go on to its next claim.
        print(f"[holo2] run {swept.run_id} was ended by the supervisor"
              f" ({swept.reason}); stopping this turn")
        return SWEPT


def _run_stages(target, task, conn=None, run_id=None, provider=None):
    """task: dict from a provider — {id, title, verify, budget_min}.

    Each task works in its own git worktree (the target stays on main, untouched),
    so a dirty/failed task can never block the repo or the next ticket.

    `conn` and `run_id` are the store and the claimed run the loop took the
    lease with. Every stage boundary below records its phase against them
    through `set_phase()`, and every review or adjudication turn records its
    round through `record_round()`, so in-flight state outlives the process:
    the run row, its rounds and its event stream say what the loop was doing
    and what the reviewer found, instead of that living only in this frame and
    in prose. Both default to None for a direct call with no store, which runs
    the same stages and records nothing.

    `provider` is the board the ticket came from, and the run needs it for one
    question only: at the merge gate, has the ticket's contract been edited
    since the claim froze it? A None provider — a direct call, a stub with no
    re-read — simply skips that check, and the gate is what it was.

    The body is the sequence of phase functions below -- worktree, setup,
    implement, review rounds (or the terminal adjudication after them), the
    merge gate, the merge -- each a plain function over the same values this
    frame threads, in the order they ran when this was one function (KO-211).
    """
    task_id = task["id"]
    # The id the ticket is mirrored and re-read under, taken before `task` is
    # rebound to the title below.
    issue_id = mirror_key(task)
    # Claim-to-merge wall clock: run_task is entered immediately after the
    # claim, so this is the ticket's actual duration as far as the loop knows.
    started = monotonic()
    verify_cmd, budget_min = task.get("verify"), task["budget_min"]
    contracts = task.get("contracts")
    # The approved ticket body, kept before `task` collapses to its title: it
    # is the contract the implementer is held to at review, so the implementer
    # turn has to be given it verbatim rather than the one-line title the
    # branch is named after.
    body = (task.get("body") or "").strip()
    # The criteria the reviewer must account for one by one, numbered in the
    # order the body lists them.
    criteria = list(task.get("criteria") or ())
    # The issue's page on Linear, for a written pull request body to link;
    # None for a provider that carries none.
    issue_url = task.get("url")
    task = task["title"]
    # The name carries the ticket identifier ahead of the title slug: two
    # tickets whose titles agree for 30 characters must not share a branch or
    # a worktree, and a preserved branch has to be traceable to its ticket
    # from `git branch` alone, whatever `[worktree] branch_prefix` puts ahead
    # of the slash. The title portion keeps its own cap; the identifier is
    # added on top of it rather than eating into it.
    ident = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    slug = f"{ident}-{slug}"
    branch = f"{branch_prefix(target)}/{slug}"
    wt = worktree_path(target, branch)
    # The approved candidate: `--approve` ended the ticket's parked run with
    # its resume point at the merge gate, and its worktree still stands.
    # Nothing to implement or review -- the candidate was, and a person said
    # merge -- so the run reuses the worktree and goes straight to the gate.
    carried = _approved_candidate(conn, run_id)
    if carried is not None and wt.exists():
        return _resume_at_merge_gate(
            target, conn, run_id, provider, task_id, issue_id, task, branch,
            wt, carried, started, verify_cmd, contracts, budget_min, body,
            criteria, issue_url=issue_url)
    fresh = _cut_worktree(target, conn, run_id, provider, task_id, task,
                          branch, wt)

    # Every wait below -- setup, agent turn, verify -- runs under
    # `heartbeat_while()`, beating at half the supervisor's stale threshold
    # so a slow agent is never read as a dead loop (KO-212, run 39). Half,
    # so one late beat is still inside the threshold.
    beat_s = sweep_config(target).heartbeat_stale_ms / 2000
    _setup_worktree(target, conn, run_id, provider, task_id, task, branch, wt,
                    fresh, beat_s)

    # The review base is main, not the HEAD reuse entered on: preserved
    # commits were never approved, so the reviewer must see them inside the
    # diff. Identical on a fresh cut, where HEAD is main.
    base_sha = sh(["git", "rev-parse", "main"], target.path)
    # Where this run started, WIP commit and preserved commits included. The
    # no-commit gate below compares against this rather than main, so carried
    # leftovers cannot stand in for the implementer's own progress.
    start_sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    # 1. implement — the ticket verbatim: title, then the approved body, then
    # the verify commands the gate will actually run. The same `ticket` text is
    # what the reviewer and the adjudicator below judge against, so all three
    # turns are held to one contract. A ticket with no body
    # (a file-backed task line, a stub provider) degrades to the title alone.
    ticket = f"{task}\n\n{body}" if body else task
    # A reuse that left main's merge mid-way (conflicts) hands the paths to
    # the implementer as the opening of its brief; empty on every other cut.
    conflicts = merge_conflicts(wt)
    sha = _implement(target, conn, run_id, task_id, task, branch, wt, fresh,
                     beat_s, start_sha, ticket, verify_cmd, budget_min,
                     conflicts=conflicts)

    # 2. review rounds, up to the cap the candidate's size earns it. Verify
    # runs before each review and its result goes into the brief; every
    # round that is not a clean approval — the last one included — gets a
    # fix round, because a last-round blocker is the cheapest fix in the
    # loop and used to need a human to close it out.
    cap = _review_cap(target, conn, run_id, provider, task_id, wt)
    sha, rnd, approved = _review_rounds(
        target, conn, run_id, provider, task_id, branch, wt, beat_s, base_sha,
        sha, ticket, verify_cmd, contracts, criteria, budget_min, cap)
    if not approved:
        _terminal_adjudication(target, conn, run_id, provider, task_id, task,
                               branch, wt, beat_s, base_sha, sha, ticket,
                               verify_cmd, contracts, cap)

    # 4. pre-merge verify (catches fix-round regressions), then merge. Both
    # happen under `merge_gate`: §4's gate node is the one edge out of a
    # passing review, and this verify is the mechanical half of what the gate
    # asks. Under the `personal` autonomy profile the human half is a no-op,
    # so the run passes through the node rather than around it and a failed
    # pre-merge verify is a run stopped at the gate.
    merge = merge_config(target)
    # The gate and the merge run under the target's merge lock, so two runs
    # reaching it together take turns and each merges the `main` the other
    # left (KO-342). Under `mode = "pr"` the candidate leaves the machine
    # instead of landing on main: pushed, opened as a pull request, and
    # babysat -- its threads answered, its checks awaited -- until it
    # merges through the PR's own API or parks for the operator. `approve`
    # is read there: the PR is what the human's answer is about. The lock
    # covers the push-and-open and not the babysitter, which waits on a
    # remote for as long as it takes.
    with _gate_lock(target, conn, run_id, provider, task_id, branch, sha,
                    beat_s):
        ok, sha = _merge_gate(target, conn, run_id, provider, task_id,
                              issue_id, branch, wt, beat_s, sha, verify_cmd,
                              contracts, ticket, budget_min,
                              sync_main=merge.mode != "pr")
        if merge.mode == "pr":
            url = _open_pr(target, conn, run_id, task_id, task, branch, body,
                           beat_s, wt, started, budget_min, issue_url)
        elif merge.approve == "human":
            # The human half of the gate, when the target asks for one: the
            # candidate is approved and verified, and a person says "merge".
            _park_for_approval(conn, run_id, provider, task_id, branch, sha)
        else:
            return _land(target, conn, run_id, provider, task_id, task,
                         branch, wt, sha, ok, started, budget_min, rnd)
    merge_sha = _babysit(target, conn, run_id, provider, task_id,
                          issue_id, task, branch, wt, sha, beat_s, url,
                          ticket, verify_cmd, contracts, budget_min,
                          criteria, reviewed=sha, verified=sha)
    return _landed_pr(conn, run_id, provider, task_id, task, branch, url,
                      merge_sha, started, budget_min, rnd)


def _approved_candidate(conn, run_id):
    """The ticket's prior run whose approved candidate this run carries, or
    None: a direct call with no store carries nothing."""
    if conn is None:
        return None
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    return store.read.approved_candidate(conn, ticket_id, run_id)


def _resume_at_merge_gate(target, conn, run_id, provider, task_id, issue_id,
                          task, branch, wt, carried, started, verify_cmd,
                          contracts, budget_min, body, criteria=(),
                          issue_url=None):
    """The approved candidate's run: the preserved worktree, the pre-merge
    verify against the main of today, the merge. No implementer, no reviewer.

    Under `[merge] mode = "pr"` nothing here lands on main either. A
    candidate the park already opened as a pull request (`carried.pr_url`)
    goes back to the babysitter before the worktree is touched -- the PR is
    the thing the answer is about, and the candidate on it may have moved
    past the park's sha by fix rounds, so the local drift check below does
    not apply: the branch as it stands is what the PR holds. The release
    says what the answer was: `--approve` is the human's "merge", so a PR
    that is green and quiet merges through the API whatever `[merge]
    approve` says; `--babysit` is "look again", and such a PR parks for
    the human under `approve = "human"` as it did before. A candidate
    parked with no PR (parked under `mode = "local"` before the mode
    changed) goes through the gate below and then leaves the machine as a
    fresh run's would, pushed and opened -- but only on an approval. The
    gate below merges, so a candidate carried here with `carried.approved`
    False (the intervention `store.babysit()` writes as the newest on its
    run, which it refuses to write on a PR-less run but a hand-written
    store row could) is not taken through it: the run fails naming the
    release, the tree untouched, and a human answers with `--approve`.

    An approval is of one sha: the candidate the reviewer approved and the
    pre-merge verify passed, recorded by the park as `runs.candidateSha`.
    So before anything readies the worktree it is held to that sha -- a
    clean tree, HEAD and the branch both on it. Anything else (a commit
    slipped in since the park, uncommitted edits) is not the approved
    candidate, and merging it here would land unreviewed work with the
    implementer and reviewer both skipped; the run fails naming both shas,
    the tree untouched -- no WIP rescue commit, nothing deleted -- for a
    human to look at. Only then does `reuse_leftover()` ready the worktree
    as after a failed run -- main merged in when it moved on, so the
    verify below is against current main -- but without the origin sync:
    the approval is of the recorded sha, and a fast-forward to a remote
    ahead would put commits no review saw under the merge below. A
    candidate that turns out to
    hold nothing beyond main is refused too: an approval is of commits, and
    a branch with none is not what the operator signed off on. The walk is
    `claimed -> merge_gate` directly, the one edge §4 draws for this path,
    with the carried run named on the stream.
    """
    # The branch is recorded first, as `_cut_worktree()` records it: the
    # worktree stands from the run's first moment, and the files panel reads
    # `runs.branch` to find it whichever way the resume goes (KO-304).
    store.set_branch(conn, run_id, branch)
    merge = merge_config(target)
    if merge.mode == "pr" and carried.pr_url is not None:
        return _resume_on_pr(target, conn, run_id, provider, task_id,
                             issue_id, task, branch, wt, carried, started,
                             verify_cmd, contracts, budget_min, body,
                             criteria)
    if not carried.approved:
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to merge the candidate for: {task}\nrun"
               f" {carried.run_id} was released by --babysit, which is not"
               " an approval, and the candidate has no pull request to"
               " babysitter; nothing was merged, committed or deleted."
               " --approve KO-n is the release that merges it.", provider)
        raise RunFailure(f"run {carried.run_id}'s candidate on {branch} was"
                         " released by --babysit, not approved, and has no"
                         " pull request; not merging")
    why = _candidate_drift(wt, branch, carried.sha)
    if why is not None:
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to merge the approved candidate"
               f" for: {task}\n{why}\nNothing was"
               " committed or deleted; a human reconciles"
               " the worktree before this ticket is run"
               " again.", provider)
        raise RunFailure(f"approved candidate on {branch} is not what was"
                         f" approved: {why}")
    ok, why = reuse_leftover(target, wt, branch, conn=conn, run_id=run_id,
                             provider=provider, task_id=task_id,
                             sync_origin=False)
    if not ok:
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to reuse the approved candidate's"
               f" worktree for: {task}\n{why}\nNothing"
               " was deleted.", provider)
        raise RunFailure(f"cannot reuse the approved candidate's worktree:"
                         f" {why}")
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if sha == sh(["git", "rev-parse", "main"], target.path):
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to merge the approved candidate"
               f" for: {task}\n{branch} holds nothing"
               " beyond main; nothing to merge.", provider)
        raise RunFailure(f"approved candidate on {branch} holds nothing"
                         " beyond main; nothing to merge")
    # Only reached with a store: a direct call carries no candidate.
    store.record_event(conn, run_id, "approved_candidate",
                       f"resuming run {carried.run_id}'s approved candidate"
                       f" {branch} at {sha[:12]} at the merge gate;"
                       " no implementer or reviewer runs")
    print(f"[holo2] {task_id}: approved candidate {branch} at {sha[:12]}"
          f" from run {carried.run_id}; skipping to the merge gate")
    beat_s = sweep_config(target).heartbeat_stale_ms / 2000
    with _gate_lock(target, conn, run_id, provider, task_id, branch, sha,
                    beat_s):
        ok, sha = _merge_gate(target, conn, run_id, provider, task_id,
                              issue_id, branch, wt, beat_s, sha, verify_cmd,
                              contracts, f"{task}\n\n{body}" if body else task,
                              budget_min, sync_main=merge.mode != "pr")
        if merge.mode == "pr":
            url = _open_pr(target, conn, run_id, task_id, task, branch, body,
                           beat_s, wt, started, budget_min, issue_url)
        else:
            return _land(target, conn, run_id, provider, task_id, task,
                         branch, wt, sha, ok, started, budget_min, 0)
    merge_sha = _babysit(target, conn, run_id, provider, task_id,
                          issue_id, task, branch, wt, sha, beat_s, url,
                          f"{task}\n\n{body}" if body else task,
                          verify_cmd, contracts, budget_min, criteria,
                          reviewed=sha, verified=sha)
    return _landed_pr(conn, run_id, provider, task_id, task, branch, url,
                      merge_sha, started, budget_min, 0)


def _sync_branch_from_origin(target, conn, run_id, provider, task_id,
                             branch, wt, url=None, reviewed=None,
                             diverged=None):
    """Fast-forward the worktree and `branch` to what `origin` holds for
    it, and return the branch's sha afterwards (KO-379).

    A person pushing commits on top of a parked candidate is the normal way
    a factory pull request is adjusted, and it leaves the local branch
    behind the remote: judged from there, the pass would read the pull
    request's head as "not the candidate this run pushed" and park again.
    So the resume fetches the branch first and compares the two the way
    `_sync_main_into_branch()` compares `main`: the remote an ancestor of
    the local tip (or equal) means nothing to do; the local tip an ancestor
    of the remote means a fast-forward, with a ledger note saying why the
    reviewed delta grew -- the new commits are held to `reviewed` exactly as
    a fix round's are; neither means the two diverged, and the run parks
    naming both shas with the worktree untouched. A fetch that cannot
    resolve (no remote, an unreachable one) is one printed line, and the
    pass goes on from the local branch as before: the pull-request pass's
    own "someone else pushed" park still covers a head it cannot see.

    `diverged` is the failure text for the caller that cannot park on a
    pull request -- the claim's reuse of a leftover branch (KO-410): a
    divergence then fails the run with it instead, `{branch}`, `{local}`
    and `{remote}` filled in.
    """
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    fetched = subprocess.run(["git", "fetch", "origin", branch], cwd=wt,
                             capture_output=True, text=True)
    if fetched.returncode != 0:
        print(f"[holo2] could not fetch {branch} from origin; working from"
              f" the local branch at {sha[:12]}:"
              f" {fetched.stderr.strip() or fetched.stdout.strip()}")
        return sha
    remote = sh(["git", "rev-parse", "FETCH_HEAD"], cwd=wt)

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=wt, capture_output=True).returncode == 0

    if remote == sha or is_ancestor(remote, sha):
        return sha
    if not is_ancestor(sha, remote):
        if diverged is not None:
            raise RunFailure(diverged.format(branch=branch, local=sha,
                                             remote=remote))
        pull = pr.parse_pr_url(url)
        if pull is None:
            raise RunFailure(f"cannot read a pull request off {url!r};"
                             f" branch {branch} preserved at {sha[:12]}")
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    f"the local branch {branch} at {sha[:12]} and origin's at"
                    f" {remote[:12]} diverged; neither fast-forwards to the"
                    " other, so nothing was fetched into the worktree and"
                    " a human reconciles them", (), reviewed=reviewed)
    count = sh(["git", "rev-list", "--count", f"{sha}..{remote}"], cwd=wt)
    sh(["git", "merge", "--ff-only", remote], cwd=wt)
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    note = (f"Fast-forwarded {branch} to {sha} from origin ({count}"
            f" commit(s) pushed by someone else)")
    print(f"[holo2] {note}")
    if conn is not None and run_id is not None:
        store.record_ledger(conn, run_id, "note", note)
    return sha


def _candidate_drift(wt, branch, approved):
    """Why the worktree at `wt` is not the candidate `approved` names, or
    None when it is: a clean tree with HEAD and `branch` both on that sha.
    The approved candidate at the merge gate and the fix round's commit
    under a pull request are held to the same test.

    `approved` is None only for a run parked by a module older than
    `runs.candidateSha`; with nothing recorded there is nothing to hold the
    tree to, so the refusal names that instead of merging on trust.
    """
    if approved is None:
        return ("the park recorded no candidate sha, so nothing vouches for"
                f" what {branch} now holds")
    dirty = sh(["git", "status", "--porcelain"], cwd=wt)
    if dirty:
        return (f"the worktree holds uncommitted changes on top of"
                f" {approved[:12]}:\n{dirty}")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if head != approved:
        return f"the worktree is at {head[:12]}, not {approved[:12]}"
    tip = sh(["git", "rev-parse", "--verify", "--quiet",
              f"refs/heads/{branch}"], cwd=wt)
    if tip != approved:
        return f"branch {branch} is at {tip[:12]}, not {approved[:12]}"
    return None


def _land(target, conn, run_id, provider, task_id, task, branch, wt, sha, ok,
          started, budget_min, rnd):
    """The merge, the target's `[merge] after` commands and the merged ledger
    line; returns the merge commit's sha. Shared by the ordinary run and the
    approved candidate's."""
    merge_sha = _merge(target, conn, run_id, provider, task_id, task, branch,
                       wt, sha)
    # Still under the merge lock, so the checkout the commands see is the
    # main this merge left and no sibling's merge moves it under them. A
    # failure parks the run rather than failing it: the merge has landed,
    # and a failed run would send the loop back to redo work main holds.
    _run_after(target, conn, run_id, provider, task_id, merge_sha,
               merge_config(target).after)
    # Nothing tells Linear the ticket is done here any more. The merge makes
    # the ticket `merged` in the store, and `main()` projects that status onto
    # the board through `mirror_push()` once the run has been released — one
    # writer of the workflow state instead of a call from the middle of a run
    # that has not finished ending yet.
    # One greppable line of timing data per merged ticket: the estimate stays
    # write-only otherwise, and a future burndown script reads this format.
    actual_min = (monotonic() - started) / 60
    ledger(conn, run_id, task_id, "merge",
           f"MERGED to main (branch {branch} deleted). "
           f"Verify: {'passed' if ok else 'n/a'}.\n"
           f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
           f"rounds: {rnd}", provider)
    # The task's own commit of FINDINGS.md is `main()`'s, not this frame's:
    # the run's close-out entry exists only once the run has been released,
    # which happens after this returns.
    print(f"[holo2] merged: {task}")
    # The merge commit itself, for the close-out to stamp on the run: truthy,
    # so every caller that read this as "did it merge" still does.
    return merge_sha


# How much of a failed `[merge] after` command's output the park's note and
# ledger carry: the last lines, where a build tool says what went wrong.
AFTER_TAIL_LINES = 20


def _run_after(target, conn, run_id, provider, task_id, merge_sha, commands):
    """`[merge] after` (KO-347): run `commands` in order in the main checkout
    once the merge commit exists, each printed with its exit code. The first
    nonzero exit stops the list and parks the run `blocked_on_operator` with
    the command and the tail of its output as the note and the ticket's
    question; `MergeParked` then unwinds the run without marking it merged.
    Nothing here touches the merge commit: main keeps it either way.
    """
    for cmd in commands:
        done = subprocess.run(cmd, shell=True, cwd=target.path,
                              capture_output=True, text=True)
        print(f"[holo2] after: {cmd} -> exit {done.returncode}")
        if done.returncode == 0:
            continue
        tail = "\n".join((done.stdout + done.stderr).splitlines()
                         [-AFTER_TAIL_LINES:])
        why = (f"[merge] after command failed with exit {done.returncode}:"
               f" {cmd}\n{tail}")
        if conn is not None and run_id is not None:
            ticket_id = store.read.run_snapshot(conn, run_id).ticketId
            if not block_ticket(conn, ticket_id, provider, why):
                print(f"[holo2] {task_id} could not be moved to"
                      " blocked_on_operator; parking the run anyway")
            store.park(conn, run_id, "blocked_on_operator", why)
        print(f"[holo2] parked after merge {merge_sha[:12]}: {why}")
        ledger(conn, run_id, task_id, "note",
               f"MERGED to main at {merge_sha}, then {why}\nThe merge stands;"
               " the run waits in blocked_on_operator.", provider)
        raise MergeParked(f"merged at {merge_sha[:12]}; after command failed:"
                          f" {cmd}")


def _scale_note(target, budget_min):
    """The ` (45 min at scale 1.5)` a budget line carries when the
    target's `[agents] budget_scale` stretches the ticket's estimate for
    the implementer harness -- nothing when the scale is 1, so the line
    is byte-identical to the one it always was."""
    scale = budget_scale(target)
    if scale == 1:
        return ""
    return f" ({budget_min * scale:g} min at scale {scale:g})"


def _timed(target, conn, run_id, beat_s, wt, budget_min, goal):
    """Run one implementer turn with the budget as its wall-clock cap.

    Returns `(output, timed_out)`: what the turn printed ("" when it
    printed nothing) and whether the cap ended it rather than the turn
    itself. The pair rather than a `None` sentinel because a timed-out
    turn still has last words -- the no-commit gate records them on the
    run before the worktree goes (KO-375) -- so the cap's partial capture
    is handed back untrimmed, not dropped with the `TimeoutExpired`.

    The budget is the dispatch's own timeout, not an alarm around it: an
    alarm interrupted the wait but left the implementer and its children
    running, so a run recorded as over budget kept committing into the
    worktree. `agent()` kills the whole group before raising, and what
    the turn printed before the kill is kept in the log. The cap armed
    here is `budget_min` times the target's `[agents] budget_scale` --
    the estimate unchanged, the harness's pace priced in.
    """
    # The sweep's hook: a beat that finds the run ended kills the turn's
    # whole process group, the same kill the budget sends, and the block
    # raises `RunSwept` for `run_task()` once the turn has stopped.
    kill = GroupKill()
    try:
        with heartbeat_while(conn, run_id, beat_s, on_swept=kill):
            return (agent(target, "implement", goal, wt,
                          timeout=budget_min * budget_scale(target) * 60,
                          on_start=kill.arm),
                    False)
    except subprocess.TimeoutExpired as expired:
        print(f"[holo2] task exceeded {budget_min} min budget"
              f"{_scale_note(target, budget_min)}")
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        partial = partial.strip()
        print("[holo2] implementer output before the budget fired:\n"
              + (partial[-2000:] or "(no output before the budget fired)"))
        return partial, True


def _open_findings(conn, run_id):
    """The findings a refused turn leaves open, as one line for the run's
    failure reason: the newest ended round's stored findings, joined. `none
    on record` for a run with no round yet -- the first turn refused on a
    reclaim -- or one whose latest round was clean.
    """
    rounds = store.read.newest_ended_rounds(conn, run_id)
    findings = json.loads(rounds[0].findings) if rounds else []
    items = []
    for finding in findings:
        message = " ".join(str(finding.get("message", "")).split())
        where = str(finding.get("path") or "?")
        if finding.get("line"):
            where += f":{finding['line']}"
        items.append(f"{where}: {message}" if message else where)
    return "; ".join(items) if items else "none on record"


def _check_run_cap(target, conn, run_id, budget_min, sha):
    """Fail the run rather than arm a turn its ceiling cannot hold.

    `[supervisor] run_cap` is the run's hard ceiling: `runs.timeBoxMs` --
    scaled the way the sweep and `/status` count it -- times the cap. Each
    turn's budget bounds one turn; this bounds the sum, for the run that
    keeps earning turns by failing review. Before `_timed()` arms a turn,
    the time spent since `runs.startedAt` plus the scaled budget the turn
    would get is held against the ceiling; over it the turn is refused
    rather than started and killed mid-edit: the run fails with a reason
    naming the minutes, the box, the cap, the candidate's sha and the open
    findings, and a `run_cap` event says the same in the run's own stream,
    so a requeue can carry the candidate.

    A storeless call has no `startedAt` to count from, and a run whose
    ticket carried no estimate has no box for the ceiling to multiply; both
    pass.
    """
    if conn is None or run_id is None:
        return
    run = store.read.run_snapshot(conn, run_id)
    if run is None or not run.timeBoxMs or not budget_min:
        return
    scale = budget_scale(target)
    cap = sweep_config(target).run_cap
    box_ms = run.timeBoxMs * scale
    spent_ms = int(time() * 1000) - run.startedAt
    if spent_ms + budget_min * scale * 60000 <= box_ms * cap:
        return
    reason = (f"out of time: {spent_ms / 60000:.1f} min spent of a "
              f"{box_ms / 60000:.0f} min box (cap {cap:g}x); candidate "
              f"preserved at {sha[:12]}; open findings: "
              f"{_open_findings(conn, run_id)}")
    store.record_event(conn, run_id, "run_cap", reason)
    raise RunFailure(reason)


# How much of the implementer's final output a no-commit turn keeps on the
# run (KO-375): the last characters, where a refusal or a "this contract
# cannot be met" explanation ends up.
OUTPUT_TAIL = 4000


def _record_implementer_output(conn, run_id, out, secrets=()):
    """Keep the tail of a no-commit turn's output as an `implementer_output`
    event, before the worktree it may have explained itself in is gone.

    A `detail` row, like `crash`: the summary is the message's first line and
    the payload its last `OUTPUT_TAIL` characters. The output is prose, not
    a document, so it goes through `redact_prose()`: every value in
    `secrets` -- the config's and the environment's credentials,
    `known_secrets()` -- and every `name = value` pair with a secret's name
    are replaced before the store sees the text, so a secret the
    implementer echoed never reaches it."""
    if conn is None or run_id is None:
        return
    text = redact_prose((out or "").strip(), secrets)
    summary = text.splitlines()[0] if text else "(implementer printed nothing)"
    store.record_event(conn, run_id, "implementer_output", summary,
                       level="detail", payload=text[-OUTPUT_TAIL:])


def _implement(target, conn, run_id, task_id, task, branch, wt, fresh, beat_s,
               start_sha, ticket, verify_cmd, budget_min, conflicts=()):
    """The implementer phase: one turn against `ticket`, then the no-commit
    gate. Returns the candidate's sha. `conflicts` are the paths a reuse
    left mid-merge; they open the brief (`conflict_brief()`)."""
    commands = (f"\n\nThese verify commands must pass before review and again "
                f"before merge:\n\n{verify_cmd}" if verify_cmd else "")
    # The run's ceiling before the first turn: a reclaim can arrive with
    # the run already old, and a turn the cap has no room for is refused
    # rather than started.
    _check_run_cap(target, conn, run_id, budget_min, start_sha)
    out, timed_out = _timed(
        target, conn, run_id, beat_s, wt, budget_min,
        conflict_brief(branch, conflicts)
        + f"Implement this task in this repo:\n\n{ticket}{commands}\n\n"
        "The ticket above is the contract, acceptance criteria "
        "included; the task is done only when they hold. Commit your "
        "work with a clear message. Stay strictly on-scope; do not "
        "expand the task.")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    # A reused branch whose tip already differs from main carries a candidate
    # an earlier run left behind. An implementer handed finished work
    # correctly adds nothing to it, so reading that as "no progress" failed
    # the ticket on every relaunch and left operator surgery or destroying the
    # work as the only exits (holophyte-bugs #3). The carried tip is the
    # candidate instead, and it still owes verify and review below.
    carried = not fresh and bool(
        subprocess.run(["git", "diff", "--quiet", "main", "HEAD"],
                       cwd=wt, capture_output=True).returncode)
    if head == start_sha and not carried and timed_out:
        # The budget is a wall-clock cap, not a judgement of the work: a
        # turn killed mid-edit — KO-391's died inside `git commit` with the
        # whole move staged — keeps the tree as a WIP commit on the branch
        # and takes the timed-out path below, so the requeue carries the
        # work instead of starting over. Only a tree with no changes at
        # all reaches the discard.
        # `-uall`: default porcelain collapses an untracked directory into
        # one `??` line, which would report files as directories in the
        # event below.
        dirty = sh(["git", "status", "--porcelain", "-uall"],
                   cwd=wt).splitlines()
        if dirty:
            # The kill can land inside `git add` itself — KO-391's turn died
            # mid-staging — and SIGKILL does no cleanup, so the interrupted
            # operation leaves `index.lock` and the add below would refuse
            # it with "File exists". The turn's whole process group was
            # reaped before `TimeoutExpired` reached here, so a lock in this
            # worktree can only be the dead turn's: remove it and stage.
            lock = Path(wt, sh(["git", "rev-parse", "--git-path",
                                "index.lock"], cwd=wt))
            lock.unlink(missing_ok=True)
            sh(["git", "add", "-A"], cwd=wt)
            # The identity is pinned for the same reason the reuse WIP
            # commit pins it: a rescue commit is the factory's, and a
            # target with no committer configured must not make it raise.
            sh(["git", "-c", "user.name=holophyte",
                "-c", "user.email=holophyte@factory.invalid",
                "commit", "-q", "-m",
                f"WIP: implementer budget fired mid-edit ({task_id});"
                " not verified"], cwd=wt)
            head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
            note = (f"budget fired mid-edit; {len(dirty)} changed file(s)"
                    f" committed as WIP on {branch} at {head[:12]}")
            print(f"[holo2] {note}")
            if conn is not None and run_id is not None:
                store.record_event(conn, run_id, "wip_committed", note)
    if head == start_sha and not carried:
        print(f"[holo2] implementer made no commits for: {task}")
        # What the turn said is the only evidence left once the worktree
        # goes; it is on the run before the discard, whatever the exit code.
        _record_implementer_output(conn, run_id, out,
                                   known_secrets(target.config()))
        if fresh:
            sh(["git", "worktree", "remove", "--force", str(wt)], target.path)
            sh(["git", "branch", "-D", branch], target.path)
            raise RunFailure("implementer made no commits; the empty branch"
                             " and worktree were discarded")
        # A reused worktree holds work some earlier run preserved; this
        # run's implementer adding nothing is no reason to destroy it.
        raise RunFailure(f"implementer made no new commits; preserved work"
                         f" kept on {branch} at {start_sha[:12]}")
    if head == start_sha:
        note = (f"candidate carried from a prior run; implementer added"
                f" nothing to {branch} at {start_sha[:12]}")
        print(f"[holo2] {note}")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "carried_candidate", note)
    if timed_out:
        # The budget alarm fired *after* real commits landed. A timeout is
        # not "no work": destroying the commits here would repeat the
        # incident this path exists to prevent.
        raise RunFailure(f"implementer exceeded the {budget_min} min budget"
                         f"{_scale_note(target, budget_min)}; work kept on "
                         f"{branch} at {head[:12]}")
    return sh(["git", "rev-parse", "HEAD"], cwd=wt)


def _verify_brief(verify_cmd, ok, out):
    """The verify result as the reviewer sees it — omitted when the ticket
    declares no command, so the brief never implies a gate that never ran."""
    if not verify_cmd:
        return ""
    return (f"A mechanical verification command was run and "
            f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n")


def _changed_lines(wt):
    """Insertions plus deletions of the candidate against its merge base
    with main. The merge base rather than main itself, so a preserved
    branch that already merged main is not charged for main's own lines.
    Binary files show `-` in `--numstat` and count for nothing.
    """
    base = sh(["git", "merge-base", "main", "HEAD"], cwd=wt)
    total = 0
    for line in sh(["git", "diff", "--numstat", base, "HEAD"], cwd=wt).splitlines():
        added, removed, *_ = line.split("\t")
        total += sum(int(n) for n in (added, removed) if n.isdigit())
    return total


def _review_cap(target, conn, run_id, provider, task_id, wt):
    """The review-round cap for this run, from the candidate's size and the
    target's `[loop]` review keys (`review_round_cap()`). Measured once,
    before round 1, and written to the run's row and its narrative so the
    store says how many rounds the candidate was given and why: `/runs/N`
    serves the row's value as `max_rounds` (KO-321). A `run_task()` driven
    with no store has no row to write.
    """
    lines = _changed_lines(wt)
    cap = review_round_cap(lines, loop_config(target))
    print(f"[holo2] review cap {cap} for {lines} changed lines")
    if conn is not None and run_id is not None:
        store.set_review_round_cap(conn, run_id, cap)
    ledger(conn, run_id, task_id, "note",
           f"Review cap {cap} for {lines} changed lines", provider)
    return cap


def _review_rounds(target, conn, run_id, provider, task_id, branch, wt, beat_s,
                   base_sha, sha, ticket, verify_cmd, contracts, criteria,
                   budget_min, cap):
    """The review phase: up to `cap` rounds of verify, review and fix round.

    Returns `(sha, rnd, approved)`: the candidate's sha after the last fix
    round, the number of the round that ended the phase, and whether that
    round was a clean approval. `approved` False means every round and its
    fix is spent and the terminal adjudication is next.
    """
    for rnd in range(1, cap + 1):
        set_phase(conn, run_id, "verifying", f"round {rnd}: verify before review")
        if rnd == 1:
            # The merge `reuse_leftover()` left for the implementer is owed
            # as its first commit; a tree still mid-merge here is the park
            # the handoff replaced, failed with the branch preserved.
            unresolved = merge_conflicts(wt)
            if unresolved:
                raise RunFailure(
                    f"preserved commits on {branch} conflict with a main"
                    f" that moved on and the implementer left the merge"
                    f" unresolved in {', '.join(unresolved)}; a human"
                    f" resolves the merge before this ticket is run again;"
                    f" branch {branch} preserved at {sha[:12]}")
        with heartbeat_while(conn, run_id, beat_s):
            ok, out = run_verify(verify_cmd, wt, contracts)
        if ok:
            print(f"[holo2] verify ok before round {rnd}")
        else:
            print(f"[holo2] verify FAILED before round {rnd}:\n{out}")

        set_phase(conn, run_id, "reviewing", f"round {rnd} review")
        round_started = int(time() * 1000)
        with heartbeat_while(conn, run_id, beat_s):
            verdict = agent(target, "review",
                f"You are a READ-ONLY code reviewer. Review commit {sha} using "
                "refs/review/base as the frozen base and refs/review/candidate "
                "as the candidate "
                "in this repo against the ticket below. The ticket is the "
                "contract, acceptance criteria included: a candidate that "
                "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
                f"{ticket}\n\n"
                + _verify_brief(verify_cmd, ok, out)
                + criteria_brief(criteria)
                + "Do not modify anything. End your reply with exactly one "
                "line:\n"
                "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
                "If REQUEST_CHANGES, list only concrete blockers.", wt,
                base_sha=base_sha, candidate_sha=sha)
        # Before the approval check, so the round that ends the loop is stored
        # like every other one: a review the store has no row for is a round
        # §6 cannot compare the next one against.
        record_round(target, conn, run_id, rnd, "review", verdict, verify_cmd,
                     ok, out,
                     started_at=round_started, criteria=criteria, root=wt)

        # A criterion the reviewer left not met or unwitnessed is a blocker
        # whatever the verdict line says (KO-165 was approved with one unmet),
        # and so is one whose witness names a test the worktree does not hold:
        # `criteria_findings()` reads that as `unwitnessed — named test not
        # found: ...`.
        unwitnessed = criteria_findings(verdict, criteria, wt)
        if unwitnessed:
            print(f"[holo2] round {rnd}: {len(unwitnessed)} criteria not "
                  "witnessed; treating as REQUEST_CHANGES")
        if (ok and not unwitnessed
                and review_runner.terminal_verdict(verdict) == "APPROVE"):
            # The approving round is a round like any other: the narrative
            # of a clean merge is a `round` entry and then a `merge` one.
            ledger(conn, run_id, task_id, "round",
                   f"Round {rnd}: APPROVE\nReviewer verdict:\n{verdict}",
                   provider)
            return sha, rnd, True

        # 3. implementer addresses findings (same branch, new commit) --
        # unless the run's ceiling has no room left for the turn: refused
        # here rather than started and killed mid-edit.
        _check_run_cap(target, conn, run_id, budget_min, sha)
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        fixes, timed_out = _timed(
            target, conn, run_id, beat_s, wt, budget_min,
            "A reviewer left findings on your work. The ticket you "
            "are held to, acceptance criteria included:\n\n"
            f"{ticket}\n\nReviewer findings:\n\n{verdict}\n\n"
            "For EACH finding, adjudicate it first: ADDRESS (concrete "
            "blocker — fix now), FOLLOW_UP (valid but out of scope — name "
            "it in the commit message), or DECLINE (invalid/out-of-scope — "
            "state the rationale in the commit message). Then fix only the "
            "ADDRESS items and commit.")
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
               f"Reviewer findings:\n{verdict}\n\n"
               f"Implementer response:\n{fixes}", provider)
        if timed_out or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            raise RunFailure(f"fix round {rnd} timed out or made no progress;"
                             f" branch {branch} preserved at {sha[:12]}")
        sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    return sha, rnd, False


def _terminal_adjudication(target, conn, run_id, provider, task_id, task,
                           branch, wt, beat_s, base_sha, sha, ticket,
                           verify_cmd, contracts, cap):
    """3b. Terminal adjudication: all `cap` review rounds and their fixes
    are spent, so one fresh independent run issues a bare verdict on the
    final state. There is no further fix round under any outcome —
    anything but PASS preserves the branch and stops the loop.
    """
    set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        print(f"[holo2] verify FAILED before adjudication; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human:\n{out}")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED verify before terminal adjudication after "
               f"{cap} review rounds (the run's cap); branch {branch} preserved "
               f"at {sha}\n\n{out}", provider)
        raise RunFailure(f"verify failed before terminal adjudication;"
                         f" branch {branch} preserved at {sha[:12]}")
    print("[holo2] verify ok before adjudication")

    set_phase(conn, run_id, "reviewing", "terminal adjudication")
    round_started = int(time() * 1000)
    with heartbeat_while(conn, run_id, beat_s):
        reply = agent(target, "adjudicate",
            f"You are a READ-ONLY final adjudicator. Judge commit {sha} "
            "using refs/review/base as the frozen base and "
            "refs/review/candidate as the candidate "
            "in this repo against the ticket below. The ticket is the "
            "contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + _verify_brief(verify_cmd, ok, out)
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
            base_sha=base_sha, candidate_sha=sha)
    # The adjudication is a round of the run like the reviews before it —
    # numbered after them, so the run's rounds read in the order they
    # happened.
    record_round(target, conn, run_id, cap + 1, "adjudicate", reply,
                 verify_cmd, ok, out, started_at=round_started)
    try:
        decision = review_runner.terminal_verdict(
            reply, review_runner.ADJUDICATION_VERDICTS)
    except review_runner.ReviewBoundaryError:
        decision = "MALFORMED"  # no clean verdict — read as FAIL
    if decision != "PASS":
        print(f"[holo2] terminal adjudication: {decision}; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human. Task: {task}")
        ledger(conn, run_id, task_id, "adjudication",
               f"Terminal adjudication after {cap} review "
               f"rounds: {decision}; branch {branch} preserved at "
               f"{sha}\n\nAdjudicator reply:\n{reply}", provider)
        raise RunFailure(f"terminal adjudication: {decision};"
                         f" branch {branch} preserved at {sha[:12]}")
    print("[holo2] terminal adjudication: PASS")
    ledger(conn, run_id, task_id, "adjudication",
           f"Terminal adjudication after {cap} review "
           f"rounds: PASS\n\nAdjudicator reply:\n{reply}", provider)


@contextlib.contextmanager
def _gate_lock(target, conn, run_id, provider, task_id, branch, sha, beat_s):
    """`merge_lock()` as the loop takes it: the run heartbeats through the
    wait, and a wait that runs out parks the ticket naming the holder before
    the `MergeLockHeld` ends the run (an infra failure: no strike spent,
    branch and worktree untouched)."""
    beat = (lambda: store.heartbeat(conn, run_id)) if run_id is not None else None
    try:
        with merge_lock(target, run_id, on_wait=beat):
            yield
    except MergeLockHeld as e:
        _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                      f"merge lock: {e}", f"MERGE GATE DID NOT RUN: {e}.")
        raise


def _park_at_gate(conn, run_id, provider, task_id, branch, sha, question,
                  ledger_text):
    """A gate refusal that is a person's to answer: the ticket goes
    `blocked_on_operator` asking `question`, the ledger records why, and
    the caller raises the failure that leaves branch and worktree in place.
    The run itself ends the way every refused merge ends."""
    if conn is not None and run_id is not None:
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        if not block_ticket(conn, ticket_id, provider, question):
            print(f"[holo2] {task_id} could not be moved to"
                  " blocked_on_operator; failing the run anyway")
    ledger(conn, run_id, task_id, "failure",
           f"{ledger_text} Branch {branch} preserved at {sha}.", provider)


def _unwind_merge(wt, sha):
    """Take `wt` back to `sha` with no merge in progress after the gate's
    resolution turn failed to leave a committed, clean merge.

    `git merge --abort` is the first try; it refuses when the turn left a
    staged resolution it would have to drop, and it has nothing to abort
    when the turn committed the merge itself. Either way the owed state
    is the same -- the branch at its pre-merge sha, merge state gone --
    and a hard reset to `sha` is that directly: what is preserved is the
    branch's committed work, never the tree the turn left behind.
    """
    subprocess.run(["git", "merge", "--abort"], cwd=wt,
                   capture_output=True, text=True)
    if (subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                       cwd=wt, capture_output=True).returncode == 0
            or sh(["git", "rev-parse", "HEAD"], cwd=wt) != sha):
        sh(["git", "reset", "--hard", sha], cwd=wt)
        print(f"[holo2] merge --abort could not unwind the failed"
              f" resolution; reset to {sha[:12]}")


def _is_ancestor(cwd, a, b):
    """Whether commit `a` is an ancestor of `b` in `cwd`'s repository."""
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                          cwd=cwd, capture_output=True).returncode == 0


def _merge_ref(wt, ref):
    """`git merge --no-edit REF` into the branch `wt` has checked out;
    `(status, detail)`.

    `"ancestor"` -- `ref` is already merged in, nothing ran, detail is
    HEAD's sha. `"merged"` -- the merge committed, detail is its sha.
    `"conflicted"` -- the merge stopped: detail is the sorted unmerged
    paths it stopped on, empty when git failed the merge without naming
    any, and `wt` is left mid-merge for the caller -- handed to the
    implementer first at the merge gate (`_sync_main_into_branch()`),
    resolved by an implementer turn on a conflicting pull request
    (`_merge_origin_main()`).
    """
    head = sh(["git", "rev-parse", "HEAD"], wt)
    if _is_ancestor(wt, ref, "HEAD"):
        return "ancestor", head
    mr = subprocess.run(["git", "merge", "--no-edit", ref], cwd=wt,
                        capture_output=True, text=True)
    if mr.returncode == 0:
        return "merged", sh(["git", "rev-parse", "HEAD"], wt)
    conflicted = sorted(
        p for p in subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt,
            capture_output=True, text=True).stdout.splitlines() if p.strip())
    return "conflicted", conflicted


def _sync_main_into_branch(target, conn, run_id, provider, task_id, branch,
                           wt, sha, beat_s, ticket, budget_min, ref="main"):
    """Merge `ref` into the branch in its worktree, so the gate verifies
    and merges the candidate as it will sit on today's `main`. `ref` is
    `main` at the local gate; the babysit pass's own call for a
    conflicting pull request is `_merge_origin_main()`, which wants
    `origin/main` and a different conflict disposition. Returns the
    branch's sha afterwards: unchanged when `ref` is already an ancestor.

    A conflict goes to the implementer first (KO-404): the same
    resolution turn the claim path runs on a leftover mid-merge worktree
    (KO-355), in this worktree, against the run's budget like a fix
    round. When the turn leaves the merge committed over a clean tree the
    gate's verify runs on the merged sha; when it does not the merge is
    undone (`_unwind_merge()`: `merge --abort`, or a reset to `sha` when
    the turn's leftovers refuse it), the branch left at `sha`, and the
    run parks with the conflicting paths in the question as before. (The
    `--no-ff` merge's own FINDINGS.md
    self-resolution is not repeated here: nothing on the branch writes
    FINDINGS.md any more, so a conflict there is a real one.)
    """
    status, detail = _merge_ref(wt, ref)
    if status == "ancestor":
        return sha
    print(f"[holo2] {ref} moved past {branch}; merging {ref} into the"
          " branch before the gate's verify")
    if status == "conflicted":
        if detail:
            merged = _resolve_merge_conflict(
                target, conn, run_id, branch, wt, sha, detail, ticket,
                beat_s, budget_min)
            if merged is not None:
                note = (f"gate conflict on {', '.join(detail)}"
                        f" resolved by the implementer at {merged[:12]}")
                if conn is not None and run_id is not None:
                    store.record_event(conn, run_id, "merge_gate", note)
                ledger(conn, run_id, task_id, "note",
                       f"MERGE GATE: {note}.", provider)
                print(f"[holo2] {note}")
                return merged
        _unwind_merge(wt, sha)
        paths = ", ".join(detail) or "(no unmerged paths reported)"
        why = (f"{store.GATE_CONFLICT_REASON}{branch} conflicted on:"
               f" {paths}; branch preserved at {sha[:12]}")
        print(f"[holo2] {why}")
        _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                      f"{GATE_CONFLICT_QUESTION}{paths}; resolve it"
                      f" on {branch} and --requeue, or merge by hand",
                      f"MERGE GATE: {ref} conflicts with {branch} on"
                      f" {paths}; the merge of {ref} into the branch was"
                      " aborted.")
        raise RunFailure(why)
    merged = detail
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "merge_gate",
                           f"merged {ref} into {branch}: {sha[:12]} ->"
                           f" {merged[:12]}")
    print(f"[holo2] {ref} merged into {branch}: {sha[:12]} -> {merged[:12]}")
    return merged


def merge_conflict_goal(branch, pull, conflicts):
    """The implementer turn's goal for a pull request's merge that stopped
    on `conflicts`: resolve and commit the in-progress merge, nothing
    else. The hand-off KO-355 gave a preserved branch's mid-merge
    worktree, run here on the PR GitHub reported conflicting."""
    return (f"The worktree is mid-merge. Merging main into {branch} -- the"
            f" branch pull request {pull.url} is open on, which GitHub"
            f" reports conflicting -- stopped on conflicts in:"
            f" {', '.join(conflicts)}. Resolve each one keeping both"
            " sides' intent (the branch's work and main's new lines both"
            " stay), then commit the merge with a message naming both"
            " sides. That commit is the whole turn: no other work, no"
            " rebase, no force-push.")


def _merge_gate(target, conn, run_id, provider, task_id, issue_id, branch, wt,
                beat_s, sha, verify_cmd, contracts, ticket, budget_min,
                sync_main=True):
    """The `merge_gate` phase: `main` merged into the branch (unless
    `sync_main` is off -- PR mode, where the merge is the remote's), the
    pre-merge verify on the result, then the drift check. Returns the
    verify's `ok`, for the merged ledger line, and the branch's sha as the
    gate leaves it. The caller holds the merge lock."""
    set_phase(conn, run_id, "merge_gate", "pre-merge verify, then the autonomy gate")
    if sync_main:
        sha = _sync_main_into_branch(target, conn, run_id, provider, task_id,
                                     branch, wt, sha, beat_s, ticket,
                                     budget_min)
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        print(f"[holo2] verify FAILED before merge; leaving branch {branch} "
              f"at {sha} for a human:\n{out}")
        _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                      f"verify failed at the merge gate:\n{out[-2000:]}",
                      f"FAILED verify before merge.\n\n{out}\n")
        raise RunFailure(f"verify failed before merge; branch {branch}"
                         f" preserved at {sha[:12]}")
    print("[holo2] verify ok before merge")

    # The other half of the gate, and the one a mechanical verify cannot ask:
    # this candidate was implemented, reviewed and verified against the ticket
    # as it stood at the claim, so a body edited since then means the work
    # answers a contract that no longer exists. Merging it would land code
    # nobody approved against the ticket as it now reads, and the honest
    # answer is the one every other refusal at this gate gives — leave the
    # branch and its worktree for a human.
    drift = merge_drift(conn, run_id, provider, issue_id)
    if drift:
        warn_on_run(conn, run_id,
                    f"{task_id} changed while the run was working "
                    f"({', '.join(drift)}); not merging {branch} at {sha} — "
                    "the candidate answers the ticket as it was claimed, not "
                    "as it now reads")
        ledger(conn, run_id, task_id, "failure",
               "MERGE REFUSED: the ticket drifted from the contract "
               f"this run was claimed under ({', '.join(drift)}). "
               f"Branch {branch} preserved at {sha}. Work it again "
               "against the body as it now reads, or restore the "
               "body the run was claimed under.", provider)
        raise RunFailure(f"ticket drifted from the claimed contract"
                         f" ({', '.join(drift)}); branch {branch} preserved"
                         f" at {sha[:12]}")
    return ok, sha


def _park_for_approval(conn, run_id, provider, task_id, branch, sha):
    """`[merge] approve = "human"`: stop an approved, verified candidate at
    the gate for a person to say "merge".

    Three writes, in the order a reader of the store needs them: the ticket
    goes `blocked_on_operator` asking `merge?`, which is what `/attention`
    shows; `store.park()` moves the run to `awaiting_merge_approval` and
    gives the lease back in one transaction, leaving the run open -- no
    `endedAt`, no outcome, because nothing failed and nothing merged; the
    ledger names the branch and the candidate sha the answer is about. Then
    `MergeParked` unwinds `run_task()` so the branch and worktree are left in
    place exactly as after a refused merge. Nothing touches main.
    """
    # The ticket row the run was claimed on, read off the run: the frame
    # carries the board's issue id, and the status move keys on the store's.
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    if not block_ticket(conn, ticket_id, provider, "merge?"):
        # The store did not take the move (warned on the run): the run still
        # parks, so an unmirrored ticket cannot make the loop merge what the
        # operator asked to sign off on.
        print(f"[holo2] {task_id} could not be moved to blocked_on_operator;"
              " parking the run anyway")
    store.park(conn, run_id, "awaiting_merge_approval",
               f"approved and verified; {branch} at {sha[:12]} waits for"
               " a human to say merge ([merge] approve = \"human\")",
               candidate_sha=sha)
    print(f"[holo2] approved and verified; parked {branch} at {sha[:12]}"
          " awaiting merge approval")
    ledger(conn, run_id, task_id, "note",
           "AWAITING MERGE APPROVAL: review approved and "
           f"verify passed; branch {branch} preserved at "
           f"{sha} and not merged ([merge] approve = "
           "\"human\"). Answer merge? to release it.", provider)
    raise MergeParked(f"awaiting merge approval; branch {branch} preserved"
                      f" at {sha[:12]}")


def _merge(target, conn, run_id, provider, task_id, task, branch, wt, sha):
    """The `merging` phase: the `--no-ff` merge of `branch` into main, its
    one self-resolved conflict, and the post-merge cleanup. Returns the full
    sha of the merge commit main now sits on."""
    # Commit any pending FINDINGS.md changes BEFORE merging so the merge
    # never trips over a dirty index. Nothing is written to the file during a
    # run any more, so this is normally a no-op; what it still catches is a
    # window an earlier failed run regenerated and left uncommitted.
    commit_findings(target, f"FINDINGS: {task_id} review records")

    # `squashing` is skipped, not faked: this merge is --no-ff and rewrites
    # no history, so the run goes merging -> done and the phase §4 puts
    # between them names an activity that never happens here.
    set_phase(conn, run_id, "merging", f"--no-ff merge of {branch} into main")
    mr = subprocess.run(["git", "merge", "--no-ff", branch, "-m",
                         f"Merge {branch}: {task}"], cwd=target.path,
                        capture_output=True, text=True)
    if mr.returncode != 0:
        _resolve_no_ff_conflict(target, conn, run_id, provider, task_id,
                                branch, sha)
    # The merge has landed: main's HEAD is the merge commit, read now before
    # the cleanup below and before anything else moves main. The branch
    # holds nothing main does not, so the worktree's stray untracked files
    # are not preserved work — and a cleanup refusal must not re-classify
    # merged work as a failed run.
    merge_sha = sh(["git", "rev-parse", "HEAD"], target.path)
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], target.path)
        sh(["git", "branch", "-d", branch], target.path)
    except RuntimeError as e:
        print(f"[holo2] post-merge cleanup left debris: {e}")
    return merge_sha


def _resolve_no_ff_conflict(target, conn, run_id, provider, task_id, branch,
                            sha):
    """A failed `--no-ff` merge: resolve it if FINDINGS.md alone conflicted,
    otherwise abort it and fail the run with main restored."""
    # What conflicted is the index's answer, not the merge output's: a
    # substring search over stdout+stderr also matches a conflict in
    # `docs/FINDINGS.md-notes.md`, or one whose message merely mentions
    # the file, and would then "resolve" a conflict nobody looked at.
    conflicted = sorted(
        p for p in subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=target.path,
            capture_output=True, text=True).stdout.splitlines() if p.strip())
    if conflicted == ["FINDINGS.md"]:
        # conflict limited to FINDINGS.md — prefer the branch side (fuller log)
        subprocess.run(["git", "checkout", "--theirs", "FINDINGS.md"],
                       cwd=target.path, capture_output=True, text=True)
        sh(["git", "add", "FINDINGS.md"], target.path)
        sh(["git", "commit", "--no-edit"], target.path)
        return
    # Anything else is a human's merge to make. An `assert` here was
    # both stripped under `python -O` and, when it did fire, left main
    # sitting on a half-applied merge with an unresolved index while
    # the run died mid-frame. Abort first, so main is the integration
    # point it was before the attempt, and fail the run through the
    # same close-out every other refusal at this gate uses — branch
    # and worktree preserved.
    subprocess.run(["git", "merge", "--abort"], cwd=target.path,
                   capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=target.path,
                           capture_output=True, text=True).stdout.strip()
    paths = ", ".join(conflicted) or "(no unmerged paths reported)"
    why = (f"merge of {branch} into main conflicted on: {paths};"
           f" branch and worktree preserved")
    if dirty:
        # The abort did not restore main: say so in the reason rather
        # than let the next run discover it.
        why += (" — main is NOT clean after the abort: "
                + " ".join(dirty.split()))
    print(f"[holo2] {why}")
    ledger(conn, run_id, task_id, "failure",
           f"MERGE ABORTED: conflict on {paths}. Branch "
           f"{branch} preserved at {sha}. Rebase it on main "
           "and re-run, or merge it by hand.", provider)
    raise RunFailure(why)


def _startup_sweep(target, conn):
    """Startup self-sweep, read-only: it records what it saw — a first
    strike on anything silent — so the *next* invocation or a
    `--sweep --act` can act on the second sighting. Nothing is failed
    from here; one sample is not evidence (STALE_STRIKES). One sweep,
    printed once, per invocation: the refused-claim handler below
    points back at these lines rather than re-sweeping (which would
    count one silence twice) or reprinting (which would look like it
    had).
    """
    seen = sweep(target, conn, int(time() * 1000))
    if seen.trips or seen.watched or seen.restarts:
        print("\n".join(sweep_lines(seen, target)))
        if seen.trips:
            print(SWEEP_HINT.format(target=target.path))
    return seen


def _mirror_queue(target, conn, project, provider):
    """Mirror every issue the board lists as ready, before the claim picks
    one, so the Board shows the queue and not only the claimed ticket.

    The Board reads the store's mirror and nothing more, and until now the
    mirror held only what the loop had claimed: the operator filed four
    tickets and saw none of them (KO-334). The listing is the one the claim
    chooses from, so a ticket the loop would not claim -- Backlog, closed,
    blocked in Linear -- is not shown either. Each candidate takes the same
    body-driven route the claim takes in `_admit_ticket()`: a body the
    template validator rejects is mirrored with `specced=False` and lands
    in `needs_spec`, a valid one lands where its lists put it. Statuses
    that are somebody's decision -- `in_flight`, `blocked_on_operator`,
    `blocked_on_deps`, the terminal ones -- are left alone by
    `store.tickets.mirror_ticket()` itself, and dependencies are left as the store
    has them. A board that cannot be asked, or a listing the mirror
    chokes on, skips the whole step in one printed line and the claim
    proceeds: this fills the Board, it does not gate the work. Nothing is
    written to Linear. Returns the listing it mirrored -- the scheduler
    counts its claimable tickets from it (KO-343) -- and None when the step
    was skipped: a board that could not be asked has said nothing about the
    queue, and an empty list would say it is empty.
    """
    mirrored = []
    try:
        for task in provider.ready_issues():
            specced = body_problem(task, target.path) is None
            mirror_task(conn, project, task, specced=specced)
            mirrored.append(task)
    except Exception as e:  # any transport or mirror failure: not a gate
        print(f"[holo2] queue mirror skipped: the board's ready issues could"
              f" not be mirrored ({e})")
        return None
    return mirrored


# The question the merge gate parks a ticket on when merging `main` into the
# branch conflicts (KO-342); the skip line names the way back, `--requeue`
# (KO-365), rather than reading the question out.
GATE_CONFLICT_QUESTION = "merge conflict with main on: "


# The repository the factory runs from, for telling its own frames in a
# crash's traceback from the standard library's and a dependency's.
ROOT = Path(__file__).resolve().parent.parent


def _factory_frame(e):
    """`path:function:line` of the innermost traceback frame of `e` whose
    file lives under `ROOT` — the deepest place in the factory's own code the
    exception escaped from — or None when no frame does. Never raises: a
    reason is worth more than a frame, so any trouble reading the traceback
    answers None."""
    try:
        for frame in reversed(traceback.extract_tb(e.__traceback__)):
            path = Path(frame.filename)
            if not path.is_absolute():
                continue  # `<string>`, `<frozen ...>`: nowhere to point at
            try:
                rel = path.resolve().relative_to(ROOT)
            except ValueError:
                continue
            if "site-packages" in rel.parts:
                continue  # a dependency installed inside the repository
            return f"{rel.as_posix()}:{frame.name}:{frame.lineno}"
    except Exception:  # noqa: BLE001 - the frame is a bonus, never a failure
        pass
    return None


def crash_reason(e):
    """The one-line close-out reason for an exception that escaped
    `run_task()`: `TYPE: message`, then `(at path:function:line)` naming the
    innermost frame in the factory's own code when the traceback has one.

    One line because sh()'s message carries the failed command's whole
    output, and the reason lands verbatim in an escalation comment's
    markdown bullet. The frame is what run 103 (KO-273) lacked: `database is
    locked` with nothing to say which write raised it."""
    reason = " ".join(f"{type(e).__name__}: {e}".split())
    frame = _factory_frame(e)
    return f"{reason} (at {frame})" if frame else reason


def _record_crash(conn, run_id, e, reason):
    """Keep the whole traceback as a `detail` event of kind `crash` before
    the close-out moves the run to `failed`. Best effort: the store may be
    the very thing that crashed the run, and its failure must not replace
    the reason in flight."""
    if conn is None:
        return
    try:
        store.record_event(
            conn, run_id, "crash", reason, level="detail",
            payload="".join(traceback.format_exception(type(e), e,
                                                       e.__traceback__)))
    except Exception as err:  # noqa: BLE001 - see the docstring
        print(f"[holo2] crash event not recorded: {err}")


class _Parked:
    """`_dispatch()`'s answer for a run parked awaiting merge approval:
    falsy, because nothing merged, and its own object, because the loop
    goes on rather than stopping on a failure."""

    def __bool__(self):
        return False


PARKED = _Parked()


class _Swept:
    """`_dispatch()`'s answer for a run the supervisor ended mid-turn: the
    heartbeat noticed, the turn was killed, and the sweep's own close-out is
    the last word on the run. Falsy like a failure, so no caller mistakes it
    for a merge, and its own object so `main()` can tell it from one."""

    def __bool__(self):
        return False

    def __repr__(self):
        return "SWEPT"


SWEPT = _Swept()


def _dispatch(target, conn, run_id, provider, task, ticket_id, refresh=True):
    """One run of `task` under `run_id`, with its failure accounting and
    close-out. Returns whether the run merged, `PARKED` for a run stopped
    at the gate by `[merge] approve = "human"`, or `SWEPT` for a run the
    supervisor ended mid-turn, whose close-out the sweep already did.

    `run_task()` answers with the merge commit's sha when it merged, and
    that sha is what the release stamps on the run; a bare `True` (the
    supervisor ended the run as merged, or a test's stand-in) merges the
    run without one. `refresh=False` leaves the run's FINDINGS.md
    regeneration -- a merged run's and a failed run's alike -- to the
    caller: a worker does it under the merge lock
    (`_render_findings_locked()`), where the file is not written beside a
    sibling's merge."""
    merged = False
    reason = None
    outcome_class = "work"
    try:
        merged = run_task(target, task, conn, run_id, provider)
    except MergeParked as e:
        # Not a failure and not an ending: `store.park()` has already moved
        # the run to `awaiting_merge_approval` and given the lease back, so
        # there is nothing to release and nothing to close out below.
        merged = PARKED
        print(f"[holo2] run parked: {e}")
    except RunFailure as e:
        reason = str(e)
        outcome_class = outcome_class_of(e)
        print(f"[holo2] run failed: {reason}")
    except Exception as e:  # noqa: BLE001 - crash containment
        # Anything that escapes run_task is this run's failure. The
        # error text becomes the close-out reason — one clean line
        # naming the factory frame it escaped from, instead of a
        # traceback with the reason lost to release_run()'s generic
        # default (KO-146 incident, run 9). The traceback itself goes
        # to the run's events, where the close-out below cannot lose it.
        reason = crash_reason(e)
        print(f"[holo2] run crashed: {reason}")
        _record_crash(conn, run_id, e, reason)
    finally:
        if merged is PARKED:
            # Parked, alive, lease released: the run's own outcome is still
            # open, so there is no entry to render and no failure to count.
            # The board lease goes with the store lease `store.park()` gave
            # back: a parked ticket is a human's, not this writer's.
            release_lease_label(target, conn, ticket_id, provider, run_id)
        elif merged is SWEPT:
            # Swept: ended, released, unlabelled and rendered by the sweep
            # itself, and the swept run is over -- nothing more is written
            # to it.
            pass
        elif merged:
            release_run(conn, run_id, True,
                        merge_sha=merged if isinstance(merged, str) else None)
            # `in_flight -> merged`, projected as Done. A run that did
            # not merge leaves the ticket in flight on purpose: the
            # branch is preserved for a human and the board should go
            # on saying the work is open, so there is nothing to push.
            mirror_status(conn, ticket_id, "merged", provider)
            release_lease_label(target, conn, ticket_id, provider, run_id)
            # Close-out, and the first moment the run's own outcome is
            # a row: the window is regenerated here rather than inside
            # `run_task()` so the entry that ends the run is in it.
            if refresh:
                refresh_findings(target, conn)
        else:
            # The failure close-out: release, escalate if this failure
            # was one too many, regenerate the window. Shared with the
            # supervisor sweep, which fails runs this loop is no
            # longer around to fail itself. Its own failure (a locked
            # store, say) must not replace what was in flight — a
            # KeyboardInterrupt included — with a traceback of its
            # own; the lease stays for release() or the sweep.
            try:
                close_out_failure(target, conn, run_id, ticket_id,
                                  reason,
                                  provider=provider,
                                  outcome_class=outcome_class,
                                  refresh=refresh)
            except Exception as close_err:  # noqa: BLE001
                print(f"[holo2] close-out failed: {close_err}")
    return merged
