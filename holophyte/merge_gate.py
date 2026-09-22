"""The merge gate: the landing the loop's stages end on.

`_merge_gate()` is the `merge_gate` phase -- `main` merged into the branch
(`_sync_main_into_branch()`, with a conflict handed to the implementer
first and otherwise parked on `GATE_CONFLICT_QUESTION`), the pre-merge
verify, the drift check -- run under `_gate_lock()`, the loop's take on
`merge_lock()`. `_park_for_approval()` stops a verified candidate for a
human under `[merge] approve = "human"`; `_resume_at_merge_gate()` is the
run that carries the approved candidate back through the gate; `_merge()`
is the `--no-ff` merge onto main and its one self-resolved conflict.
`_run_stages()` in `holophyte.loop` and `land()` in `holophyte.run` call in;
back-references into the loop are deferred imports inside function bodies.

Moved verbatim from `holophyte.loop` (KO-424, design note 0015).
"""
import contextlib
import subprocess
from dataclasses import replace

import store
import store.read
from holophyte import run as run_state
from holophyte.babysitter import _babysit
from holophyte.board import block_ticket, ledger, merge_drift
from holophyte.claim import _resolve_merge_conflict, reuse_leftover
from holophyte.config_tables import merge_config, sweep_config
from holophyte.environment_git import refuse_environment_history
from holophyte.findings import commit_findings
from holophyte.gates import (
    MergeLockHeld,
    MergeParked,
    RunFailure,
    run_verify,
    sh,
    with_baseline,
)
from holophyte.merge_lock import live_merge_lock
from holophyte.pullrequest import _open_pr, _resume_on_pr
from holophyte.redact import safe_print as print
from holophyte.runs import heartbeat_while, set_phase, warn_on_run
from holophyte.stop import stop_if_requested


def _resume_at_merge_gate(run, carried, verify_cmd,
                          contracts, body, criteria=(),
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
    target, conn, run_id, provider = run.target, run.conn, run.run_id, run.provider
    task_id, issue_id, task = run.task_id, run.issue_id, run.task
    branch, wt, started, budget_min = run.branch, run.wt, run.started, run.budget_min
    from holophyte.loop import _candidate_drift
    # The branch is recorded first, as `_cut_worktree()` records it: the
    # worktree stands from the run's first moment, and the files panel reads
    # `runs.branch` to find it whichever way the resume goes (KO-304).
    store.set_branch(conn, run_id, branch)
    merge = merge_config(target)
    if merge.mode == "pr" and carried.pr_url is not None:
        return _resume_on_pr(run, carried, verify_cmd, contracts, body, criteria)
    if not carried.approved and not carried.paused:
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
            sha = sh(["git", "rev-parse", branch], wt)
        else:
            if carried.paused and merge.approve == "human":
                _park_for_approval(conn, run_id, provider, task_id, branch, sha)
            return run_state.land(replace(run, sha=sha), ok)
    run = replace(run, sha=sha, pr_url=url)
    run = _babysit(run, beat_s, f"{task}\n\n{body}" if body else task,
                   verify_cmd, contracts, criteria, reviewed=sha, verified=sha)
    return run_state.land(run, True)


@contextlib.contextmanager
def _gate_lock(target, conn, run_id, provider, task_id, branch, sha, beat_s):
    """`merge_lock()` as the loop takes it: the run heartbeats through the
    wait, and a wait that runs out parks the ticket naming the holder before
    the `MergeLockHeld` ends the run (an infra failure: no strike spent,
    branch and worktree untouched)."""
    try:
        with live_merge_lock(target, conn, run_id, beat_s):
            yield
    except MergeLockHeld as e:
        _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                      f"merge lock: {e}", f"MERGE GATE DID NOT RUN: {e}.",
                      park_kind="merge_lock")
        raise


def _park_at_gate(conn, run_id, provider, task_id, branch, sha, question,
                  ledger_text, park_kind="question"):
    """A gate refusal that is a person's to answer: the ticket goes
    `blocked_on_operator` asking `question`, the ledger records why, and
    the caller raises the failure that leaves branch and worktree in place.
    The run itself ends the way every refused merge ends."""
    if conn is not None and run_id is not None:
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        if not block_ticket(conn, ticket_id, provider, question, park_kind=park_kind):
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
        failure_kind = "unclassified"
        if detail:
            merged, failure_kind = _resolve_merge_conflict(
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
        raise RunFailure(why, failure_kind)
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
        ok, out = run_verify(verify_cmd, wt, contracts, conn=conn, run_id=run_id,
                             target=target)
        ok, out = with_baseline(target, wt, verify_cmd, ok, out,
                               conn, run_id, before_merge=True)
    stop_if_requested(conn, run_id, "merge_gate")
    if not ok:
        print(f"[holo2] verify FAILED before merge; leaving branch {branch} "
              f"at {sha} for a human:\n{out}")
        _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                      f"verify failed at the merge gate:\n{out[-2000:]}",
                      f"FAILED verify before merge.\n\n{out}\n")
        raise RunFailure(f"verify failed before merge; branch {branch}"
                         f" preserved at {sha[:12]}", "verify")
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
    from holophyte.commit_hygiene import strip_attribution

    strip_attribution(target, wt, branch)
    sha = sh(["git", "rev-parse", branch], wt)
    checked = refuse_environment_history(target, branch, action="merge")
    # Commit a FINDINGS.md window left dirty by an earlier failed run before
    # merging. Normally a no-op: runs no longer write this file mid-flight.
    commit_findings(target, f"FINDINGS: {task_id} review records")

    # `squashing` is skipped, not faked: this merge is --no-ff and rewrites
    # no history, so the run goes merging -> done and the phase §4 puts
    # between them names an activity that never happens here.
    set_phase(conn, run_id, "merging", f"--no-ff merge of {branch} into main")
    mr = subprocess.run(["git", "merge", "--no-ff", checked, "-m",
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


# The question the merge gate parks a ticket on when merging `main` into the
# branch conflicts (KO-342); the skip line names the way back, `--requeue`
# (KO-365), rather than reading the question out.
GATE_CONFLICT_QUESTION = "merge conflict with main on: "
