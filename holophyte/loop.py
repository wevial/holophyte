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
import os
import re
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep, time

import review_runner
import store
import store.read
from holophyte import babysitter, pr
from holophyte.agents import agent, agent_route, probe_implementer
from holophyte.board import (
    MAX_FAILED_RUNS,
    block_ticket,
    body_problem,
    close_out_failure,
    drop_lease_label,
    escalate,
    failure_history,
    foreign_lease_holders,
    is_strike_question,
    lease_holders,
    lease_host,
    lease_label,
    lease_turn,
    ledger,
    merge_drift,
    mirror_key,
    mirror_push,
    mirror_status,
    mirror_task,
    release_lease_label,
    release_run,
    store_status,
)
from holophyte.config import (
    branch_prefix,
    loop_config,
    merge_config,
    report_config,
    setup_commands,
    setup_timeout,
    sweep_config,
)
from holophyte.findings import commit_findings, refresh_findings
from holophyte.gates import (
    GroupKill,
    InfraFailure,
    MergeLockHeld,
    MergeParked,
    RunFailure,
    merge_lock,
    outcome_class_of,
    run_verify,
    sh,
)
from holophyte.redact import known_secrets, redact_prose
from holophyte.reexec import reexec_command, reexec_self
from holophyte.report import report_lines
from holophyte.review import criteria_brief, criteria_findings
from holophyte.runs import (
    RunSwept,
    heartbeat_while,
    open_store,
    record_round,
    review_round_cap,
    set_phase,
    warn_on_run,
)
from holophyte.supervisor import (
    SWEEP_HINT,
    Sweep,
    supervisor_liveness_line,
    sweep,
    sweep_lines,
)
from holophyte.target import worktree_path

# The paths a run works against, plus the config they carry, are a `Target`
# (below): built once by `cli()` from the command line and passed to every
# function that needs one, so the derivation lives in one place and the
# command line is the only thing that chooses a target. Importing this module
# used to read `sys.argv[1]`, which made every `python3 -m unittest discover`
# retarget the factory at a directory called "discover"; now importing it
# chooses no target at all.
# How the loop restarts itself after merging a change to its own code: the
# process image is replaced, never a module reloaded. A seam so tests can
# see the decision without exec-ing the test runner.
EXEC = os.execv


def timeout_report(cmd, expired):
    """Read one `subprocess.TimeoutExpired` as a failure report.

    A command that hangs is a failed command, not an unhandled exception: it
    is the cap doing its job, and the caller can only act on it if it arrives
    as the same `(ok, report)` a non-zero exit arrives as. Whatever the
    command printed before the cap fired is kept -- a hung build says where it
    hung in its last line of output -- and trimmed to the same 2000 characters
    a passing verify keeps, with silence reported as silence.
    """
    out = expired.output or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    out = out.strip()[-2000:]
    return (f"[verify] command timed out after {expired.timeout:g}s: {cmd}\n"
            + (out or "(no output before the timeout)"))


def run_worktree_setup(target, wt, conn=None, run_id=None):
    """Run the target's setup commands in the fresh worktree `wt`.

    Returns `(ok, report)`. Each command goes through `run_verify()`, so a
    failure reads like a failed verify and not like a bare non-zero exit: the
    command is named, its output is shown, a top-level `&&` chain is
    attributed clause by clause, and silence is reported as silence. The same
    machinery also means a wall-clock cap per command -- `setup_timeout()`,
    the target's `[worktree] setup_timeout_sec` over the verify cap; setup
    is a build step, not a round -- and the cap takes the command's whole
    process tree with it, so a build that hangs is not still writing into the
    worktree while the caller deletes it. A command that reaches the cap is
    failed like any other failing command rather than raised: on a `False`
    the caller discards a branch it cut fresh (a reused worktree, which may
    hold preserved work, is left in place), and a setup that hangs is exactly
    the case that must not leave a fresh cut behind.

    Commands run in order and stop at the first failure: step two of a setup
    assumes step one worked, so running on would only report a second failure
    about the first one. A target that names no setup runs nothing and records
    no phase, so an absent table leaves the run byte-identical to today's.
    """
    commands = setup_commands(target)
    if not commands:
        return True, ""
    timeout = setup_timeout(target)
    set_phase(conn, run_id, "working",
              f"worktree setup: {len(commands)} command(s) in {wt}")
    for n, command in enumerate(commands, 1):
        try:
            ok, out = run_verify(command, wt, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            ok, out = False, timeout_report(command, e)
        if not ok:
            return False, (f"[holo2] worktree setup command {n} of "
                           f"{len(commands)} FAILED: {command}\n{out}")
        print(f"[holo2] worktree setup {n}/{len(commands)} ok: {command}")
    return True, ""


def reuse_leftover(target, wt, branch):
    """Ready leftover worktree `wt` for a new run on `branch`; (ok, reason).

    The reuse rule, stated once: preserved work survives. An unregistered
    directory is refused with a reason rather than deleted or crashed into
    (`git worktree add` onto a non-empty directory dies); uncommitted
    changes become a WIP commit on the branch; and the branch is reset to
    main only when the leftover verifiably holds nothing — a clean tree and
    a branch whose tips main already contains. A branch with preserved
    commits keeps them, with main merged in when it has moved on: the
    review routes and the merge both require main to be an ancestor of the
    candidate, so a carried branch predating the current main would
    otherwise stall the ticket on every rerun. A merge that stops on
    conflicts is left mid-merge in the worktree for the implementer turn to
    resolve as its first commit (`merge_conflicts()` names the paths for
    its brief); the ones seen were tests appended at the same lines, never a
    person's call. A worktree sitting off the branch while the branch holds
    commits of its own is a human's call and is refused with the state
    named. Nothing is ever deleted here.
    """
    sh(["git", "worktree", "prune"], target.path)
    r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                       cwd=target.path, capture_output=True, text=True)
    # Exact resolved paths, not a substring test: slugs are truncated titles,
    # so a registered `.../add-a-thing-later` must not vouch for an
    # unregistered `.../add-a-thing` — and git prints resolved paths, so a
    # target reached through a symlink must not read as unregistered.
    registered = {str(Path(line[len("worktree "):]).resolve())
                  for line in r.stdout.splitlines()
                  if line.startswith("worktree ")}
    if str(Path(wt).resolve()) not in registered:
        return False, (f"leftover directory {wt} exists but is not a"
                       " registered worktree; a human moves it aside or"
                       " removes it before this ticket is run again")
    dirty = sh(["git", "status", "--porcelain"], cwd=wt)

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=wt, capture_output=True).returncode == 0

    # `checkout -B` below moves `branch` to wherever HEAD is, so a worktree
    # sitting off the branch — detached for a human's comparison, say —
    # while the branch holds commits of its own would silently orphan them.
    # Whose tip is the work is not this function's call to make.
    head_ref = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    branch_held = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=wt, capture_output=True).returncode == 0
    if head_ref != branch and branch_held and not is_ancestor(branch, "HEAD"):
        return False, (f"worktree {wt} is on {head_ref} while branch"
                       f" {branch} holds commits it does not; a human"
                       " reconciles them before this ticket is run again")
    if (not dirty and is_ancestor("HEAD", "main")
            and (not branch_held or is_ancestor(branch, "main"))):
        # Verifiably empty: a clean tree, and neither tip holding anything
        # main does not already have. The one case where resetting loses no
        # work — and the reset is what keeps the branch from starting behind
        # a main that moved on since the leftover was cut.
        sh(["git", "checkout", "-B", branch, "main"], cwd=wt)
        return True, ""
    # `-B` with no start point parks `branch` at the HEAD we are on without
    # touching the tree, so it cannot die on uncommitted files the way
    # `-B branch main` does.
    sh(["git", "checkout", "-B", branch], cwd=wt)
    if dirty:
        sh(["git", "add", "-A"], cwd=wt)
        # The identity is pinned so a target with no committer configured
        # cannot make the one function whose contract is "no traceback
        # escapes" raise — and a rescue commit is the factory's, not a
        # person's.
        sh(["git", "-c", "user.name=holophyte",
            "-c", "user.email=holophyte@factory.invalid", "commit", "-m",
            f"WIP: uncommitted leftovers preserved on reuse of {branch}"],
           cwd=wt)
        print(f"[holo2] preserved uncommitted leftovers as a WIP commit"
              f" on {branch}")
    if not is_ancestor("main", "HEAD"):
        # Preserved commits under a main that moved on: the review routes
        # and the merge gate both require main to be an ancestor of the
        # candidate, so left diverged the branch would raise out of every
        # review dispatch and stall the ticket on each rerun. Bringing main
        # in preserves the commits and restores the invariant. A conflict
        # is left in the tree for the implementer: parking it for a person
        # cost an operator round-trip per add/add overlap in a test file
        # (KO-355), and the first verify fails the run if it is still there.
        r = subprocess.run(["git", "-c", "user.name=holophyte",
                            "-c", "user.email=holophyte@factory.invalid",
                            "merge", "--no-edit", "main"],
                           cwd=wt, capture_output=True, text=True)
        if r.returncode != 0:
            conflicts = merge_conflicts(wt)
            if not conflicts:
                # Not a textual conflict -- the merge died some other way
                # and left nothing an implementer can resolve.
                subprocess.run(["git", "merge", "--abort"], cwd=wt,
                               capture_output=True)
                return False, (f"merging the moved-on main into preserved"
                               f" branch {branch} failed without a"
                               f" conflict to resolve; a human reconciles"
                               f" them before this ticket is run again\n"
                               f"{(r.stdout + r.stderr).strip()}")
            print(f"[holo2] merge of the moved-on main into preserved branch"
                  f" {branch} stopped on conflicts in"
                  f" {', '.join(conflicts)}; left mid-merge for the"
                  " implementer to resolve first")
            return True, ""
        print(f"[holo2] merged the moved-on main into preserved branch"
              f" {branch}")
    return True, ""


def merge_conflicts(wt):
    """The paths a merge in progress in `wt` stopped on; empty when the
    tree is not mid-merge. `MERGE_HEAD` is the mid-merge marker git itself
    keeps, so a merge whose conflicts were staged but never committed still
    counts as unresolved -- the paths are then read from the merge's two
    parents rather than from the index's unmerged entries."""
    mid_merge = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
        cwd=wt, capture_output=True).returncode == 0
    if not mid_merge:
        return []
    unmerged = sh(["git", "diff", "--name-only", "--diff-filter=U"],
                  cwd=wt).splitlines()
    return unmerged or sh(["git", "diff", "--name-only", "HEAD", "MERGE_HEAD"],
                          cwd=wt).splitlines()


def conflict_brief(branch, conflicts):
    """The paragraph that opens an implementer brief whose worktree was
    left mid-merge by `reuse_leftover()`; empty when there is nothing to
    resolve."""
    if not conflicts:
        return ""
    return (f"FIRST, before the ticket's work: the worktree is mid-merge."
            f" Merging main into the preserved branch {branch} stopped on"
            f" conflicts in: {', '.join(conflicts)}. Resolve each one keeping"
            " both sides' intent (the branch's preserved work and main's new"
            " lines both stay), then commit the merge with a message naming"
            " both sides, so that commit is your first. Only then do the"
            " ticket's work below. A run whose first verify still finds the"
            " merge unresolved fails.\n\n")


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
    sha = _implement(target, conn, run_id, task, branch, wt, fresh, beat_s,
                     start_sha, ticket, verify_cmd, budget_min,
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
                              contracts, sync_main=merge.mode != "pr")
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
    exactly as after a failed run -- main merged in when it moved on, so the
    verify below is against current main. A candidate that turns out to
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
    ok, why = reuse_leftover(target, wt, branch)
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
                              contracts, sync_main=merge.mode != "pr")
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


def _resume_on_pr(target, conn, run_id, provider, task_id, issue_id, task,
                  branch, wt, carried, started, verify_cmd, contracts,
                  budget_min, body, criteria=()):
    """The resumed run of a candidate open as a pull request: the babysitter
    again, from the branch as it stands, with the release's answer
    (`carried.approved`) deciding what a green, quiet PR does.

    What the babysitter may merge without another review is not the branch
    as it stands but the sha an independent judgement covered: the
    operator's `--approve` is of the sha the park recorded, and
    `--babysit` is no judgement at all, so it carries the park's
    `approvedSha` -- the reviewer's approval, or None when the park had
    none to record (a fix the reviewer rejected, a store older than the
    column). A branch at any other sha is reviewed again before the merge
    API is called; that is `_babysit()`'s `reviewed`.

    Nothing here has verified the branch either: the park's verify was a
    process ago, against the main of that day, and `--babysit` or
    `--approve` vouches for a judgement, not for the tree. So the
    babysitter is told no sha is verified (`verified=None`) and runs the
    merge gate -- the ticket's verify commands, then the drift check --
    on the candidate before the merge API is called."""
    url = carried.pr_url
    reviewed = carried.sha if carried.approved else carried.approved_sha
    if sh(["git", "status", "--porcelain"], cwd=wt):
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to babysit {url} for: {task}\nthe worktree holds"
               " uncommitted changes; nothing was committed or deleted, and"
               " a human reconciles it before this ticket is run again.",
               provider)
        raise RunFailure(f"worktree of {branch} holds uncommitted changes;"
                         f" not babysitting {url}")
    sha = _sync_branch_from_origin(target, conn, run_id, provider, task_id,
                                   branch, wt, url, reviewed)
    store.record_event(conn, run_id, "pull_request",
                       f"resuming run {carried.run_id}'s candidate {branch}"
                       f" at {sha[:12]} on {url}"
                       + (" after an approval" if carried.approved
                          else " for another babysit pass"))
    print(f"[holo2] {task_id}: candidate {branch} is open as {url};"
          " babysitting it")
    beat_s = sweep_config(target).heartbeat_stale_ms / 2000
    set_phase(conn, run_id, "merge_gate", f"babysitting {url}")
    merge_sha = _babysit(target, conn, run_id, provider, task_id, issue_id,
                          task, branch, wt, sha, beat_s, url,
                          f"{task}\n\n{body}" if body else task, verify_cmd,
                          contracts, budget_min, criteria,
                          approved=carried.approved, reviewed=reviewed,
                          verified=None)
    return _landed_pr(conn, run_id, provider, task_id, task, branch, url,
                      merge_sha, started, budget_min, 0)


def _sync_branch_from_origin(target, conn, run_id, provider, task_id,
                             branch, wt, url, reviewed):
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
    """
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    fetched = subprocess.run(["git", "fetch", "origin", branch], cwd=wt,
                             capture_output=True, text=True)
    if fetched.returncode != 0:
        print(f"[holo2] could not fetch {branch} from origin; babysitting"
              f" from the local branch at {sha[:12]}:"
              f" {fetched.stderr.strip() or fetched.stdout.strip()}")
        return sha
    remote = sh(["git", "rev-parse", "FETCH_HEAD"], cwd=wt)

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=wt, capture_output=True).returncode == 0

    if remote == sha or is_ancestor(remote, sha):
        return sha
    if not is_ancestor(sha, remote):
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


def _refresh_main(target, run_id=None):
    """Fetch `origin` and fast-forward the checkout's `main` when
    `origin/main` is ahead, so every branch is cut from everything already
    on `main` anywhere (KO-378). Three cases after the fetch: `origin/main`
    already an ancestor of `main` (equal, or the local-mode checkout ahead
    on unpushed merges) and nothing changes; `main` an ancestor of
    `origin/main` and it is fast-forwarded -- never reset, origin is not the
    source of truth for a local-mode target; neither, and the two diverged:
    the refusal names both shas and the run fails before any cut. A target
    with no `origin` skips the step. A fetch that fails is the network's
    failure, not the ticket's, so it is `InfraFailure`; so is a divergence,
    which a person untangles and which says nothing about the ticket.

    Under the merge lock, as the gate's merge is, so a fast-forward and a
    `--no-ff` merge into `main` never interleave.
    """
    if "origin" not in sh(["git", "remote"], target.path).splitlines():
        return

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=target.path, capture_output=True).returncode == 0

    with merge_lock(target, run_id):
        fr = subprocess.run(["git", "fetch", "origin"], cwd=target.path,
                            capture_output=True, text=True)
        if fr.returncode != 0:
            raise InfraFailure("git fetch origin failed before the cut:"
                               f" {fr.stderr.strip() or fr.stdout.strip()}")
        if subprocess.run(["git", "rev-parse", "--verify", "-q", "origin/main"],
                          cwd=target.path, capture_output=True).returncode != 0:
            return  # a remote with no `main` yet: nothing to compare against
        local = sh(["git", "rev-parse", "main"], target.path)
        remote = sh(["git", "rev-parse", "origin/main"], target.path)
        if is_ancestor("origin/main", "main"):
            return
        if is_ancestor("main", "origin/main"):
            # `merge --ff-only` moves the checked-out branch; a checkout
            # sitting elsewhere gets its `main` ref moved directly, the
            # ancestry just proved it a fast-forward.
            head = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], target.path)
            if head == "main":
                sh(["git", "merge", "--ff-only", "origin/main"], target.path)
            else:
                sh(["git", "update-ref", "refs/heads/main", remote, local],
                   target.path)
            print(f"[holo2] main fast-forwarded to origin/main:"
                  f" {local[:12]} -> {remote[:12]}")
            return
    raise InfraFailure(f"main diverged from origin/main: main is at {local},"
                       f" origin/main is at {remote}; neither contains the"
                       " other, so no branch was cut -- a person reconciles"
                       " the checkout with origin before this ticket is run"
                       " again")


def _cut_worktree(target, conn, run_id, provider, task_id, task, branch, wt):
    """The worktree phase: cut `branch` at `wt`, or reuse the leftover there.

    Returns whether the worktree is fresh -- holds nothing beyond main -- so
    the close-outs after it neither claim preservation over nothing nor keep
    an empty leftover alive forever.
    """
    # §4's one edge out of `claimed`, taken before the first git command:
    # cutting the worktree is already this run doing the ticket's work, so a
    # crash in it belongs to `working` and not to a run that still looks
    # freshly claimed. The branch is recorded first: the files panel reads
    # `runs.branch` to find the worktree, and a `working` run without one
    # answered 409 for the whole phase the panel exists to show (KO-304).
    if conn is not None:
        store.set_branch(conn, run_id, branch)
    set_phase(conn, run_id, "working", f"cutting {branch} and implementing")
    if wt.exists():
        # leftover from a previous failed run — reuse it so preserved work
        # survives; the branch check below still gates on commits.
        ok, why = reuse_leftover(target, wt, branch)
        if not ok:
            ledger(conn, run_id, task_id, "failure",
                   f"FAILED to reuse leftover worktree for: {task}\n"
                   f"{why}\nNothing was deleted.", provider)
            raise RunFailure(f"cannot reuse leftover worktree: {why}")
        # Whether the leftover actually holds anything, decided from content
        # rather than from which arm ran: an empty reuse was reset to main by
        # reuse_leftover() and is indistinguishable from a fresh cut, so the
        # close-outs below must neither claim preservation over nothing nor
        # keep an empty leftover alive forever.
        return (not sh(["git", "status", "--porcelain"], cwd=wt)
                and sh(["git", "rev-parse", "HEAD"], cwd=wt)
                == sh(["git", "rev-parse", "main"], target.path))
    # The mirror leftover: the branch exists but its directory does not
    # (a FAIL close-out preserves both; a human may clear only the
    # directory). `checkout -b` would die on it, and deleting the branch
    # could destroy preserved commits — so the run fails cleanly, the
    # same answer as the unregistered directory.
    if sh(["git", "branch", "--list", branch], target.path):
        why = (f"branch {branch} already exists with no worktree; a"
               " human moves it aside or deletes it before this ticket"
               " is run again")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to cut a fresh worktree for: {task}\n"
               f"{why}\nNothing was deleted.", provider)
        raise RunFailure(f"cannot cut a fresh worktree: {why}")
    _refresh_main(target, run_id)
    sh(["git", "worktree", "add", "--detach", str(wt), "main"], target.path)
    sh(["git", "checkout", "-b", branch], cwd=wt)
    return True


def _setup_worktree(target, conn, run_id, provider, task_id, task, branch, wt,
                    fresh, beat_s):
    """The setup phase: the target's `[worktree] setup` table, run in `wt`.

    The worktree exists and nothing has been dispatched into it yet, which
    is the only moment the target's own setup can run: an implementer whose
    toolchain is missing burns its whole budget discovering that, and a
    worktree that silently borrows the main checkout's environment tests
    something other than the branch it is on. A failure here is the run's
    failure. A branch this run cut fresh is discarded -- no agent ran, so
    there is no work on it to keep -- while a reused worktree may hold
    preserved work the setup failure says nothing about, and is left
    exactly as found. Either way no agent ran, so the failure is the
    factory's plumbing, not evidence about the ticket: it closes out as
    `InfraFailure` and does not spend one of the ticket's strikes.
    """
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_worktree_setup(target, wt, conn, run_id)
    if ok:
        return
    print(out)
    # Ledger first: a deletion that itself fails must not also cost the
    # durable record of why the run stopped.
    if fresh:
        ledger(conn, run_id, task_id, "failure",
               f"FAILED worktree setup for: {task}\nNo agent ran;"
               f" branch {branch} holds nothing and is"
               f" discarded.\n\n{out}", provider)
        sh(["git", "worktree", "remove", "--force", str(wt)], target.path)
        sh(["git", "branch", "-D", branch], target.path)
        raise InfraFailure("worktree setup failed; no agent ran and the"
                           " empty branch was discarded")
    ledger(conn, run_id, task_id, "failure",
           f"FAILED worktree setup for: {task}\nNo agent ran; "
           f"reused worktree {wt} left in place with its "
           f"work.\n\n{out}", provider)
    raise InfraFailure(f"worktree setup failed; no agent ran; reused"
                       f" worktree and branch {branch} left in place with"
                       " their work")


def _timed(target, conn, run_id, beat_s, wt, budget_min, goal):
    """Run one implementer turn with the budget as its wall-clock cap; None on
    timeout.

    The budget is the dispatch's own timeout, not an alarm around it: an
    alarm interrupted the wait but left the implementer and its children
    running, so a run recorded as over budget kept committing into the
    worktree. `agent()` kills the whole group before raising, and what
    the turn printed before the kill is kept in the log.
    """
    # The sweep's hook: a beat that finds the run ended kills the turn's
    # whole process group, the same kill the budget sends, and the block
    # raises `RunSwept` for `run_task()` once the turn has stopped.
    kill = GroupKill()
    try:
        with heartbeat_while(conn, run_id, beat_s, on_swept=kill):
            return agent(target, "implement", goal, wt,
                         timeout=budget_min * 60, on_start=kill.arm)
    except subprocess.TimeoutExpired as expired:
        print(f"[holo2] task exceeded {budget_min} min budget")
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        partial = partial.strip()[-2000:]
        print("[holo2] implementer output before the budget fired:\n"
              + (partial or "(no output before the budget fired)"))
        return None


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


def _implement(target, conn, run_id, task, branch, wt, fresh, beat_s,
               start_sha, ticket, verify_cmd, budget_min, conflicts=()):
    """The implementer phase: one turn against `ticket`, then the no-commit
    gate. Returns the candidate's sha. `conflicts` are the paths a reuse
    left mid-merge; they open the brief (`conflict_brief()`)."""
    commands = (f"\n\nThese verify commands must pass before review and again "
                f"before merge:\n\n{verify_cmd}" if verify_cmd else "")
    out = _timed(target, conn, run_id, beat_s, wt, budget_min,
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
    if out is None:
        # The budget alarm fired *after* real commits landed. A timeout is
        # not "no work": destroying the commits here would repeat the
        # incident this path exists to prevent.
        raise RunFailure(f"implementer exceeded the {budget_min} min budget;"
                         f" work kept on {branch} at {head[:12]}")
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

        # 3. implementer addresses findings (same branch, new commit)
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        fixes = _timed(target, conn, run_id, beat_s, wt, budget_min,
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
        if fixes is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
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


def _sync_main_into_branch(target, conn, run_id, provider, task_id, branch,
                           wt, sha):
    """Merge `main` into the branch in its worktree, so the gate verifies
    and merges the candidate as it will sit on today's `main`. Returns the
    branch's sha afterwards: unchanged when `main` is already an ancestor.

    A conflict is a person's to resolve, whatever the path: the merge is
    aborted, the branch left at `sha`, and the run parks with the
    conflicting paths in the question. (The `--no-ff` merge's own
    FINDINGS.md self-resolution is not repeated here: the ticket's contract
    is that a `main` that conflicts with the branch parks, and nothing on
    the branch writes FINDINGS.md any more, so a conflict there is a real
    one.)
    """
    if subprocess.run(["git", "merge-base", "--is-ancestor", "main", "HEAD"],
                      cwd=wt, capture_output=True).returncode == 0:
        return sha
    print(f"[holo2] main moved past {branch}; merging main into the branch"
          " before the gate's verify")
    mr = subprocess.run(["git", "merge", "--no-edit", "main"], cwd=wt,
                        capture_output=True, text=True)
    if mr.returncode != 0:
        conflicted = sorted(
            p for p in subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt,
                capture_output=True, text=True).stdout.splitlines() if p.strip())
        subprocess.run(["git", "merge", "--abort"], cwd=wt,
                       capture_output=True, text=True)
        paths = ", ".join(conflicted) or "(no unmerged paths reported)"
        why = (f"{store.GATE_CONFLICT_REASON}{branch} conflicted on:"
               f" {paths}; branch preserved at {sha[:12]}")
        print(f"[holo2] {why}")
        _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                      f"{GATE_CONFLICT_QUESTION}{paths}; resolve it"
                      f" on {branch} and --requeue, or merge by hand",
                      f"MERGE GATE: main conflicts with {branch} on"
                      f" {paths}; the merge of main into the branch was"
                      " aborted.")
        raise RunFailure(why)
    merged = sh(["git", "rev-parse", "HEAD"], wt)
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "merge_gate",
                           f"merged main into {branch}: {sha[:12]} ->"
                           f" {merged[:12]}")
    print(f"[holo2] main merged into {branch}: {sha[:12]} -> {merged[:12]}")
    return merged


def _merge_gate(target, conn, run_id, provider, task_id, issue_id, branch, wt,
                beat_s, sha, verify_cmd, contracts, sync_main=True):
    """The `merge_gate` phase: `main` merged into the branch (unless
    `sync_main` is off -- PR mode, where the merge is the remote's), the
    pre-merge verify on the result, then the drift check. Returns the
    verify's `ok`, for the merged ledger line, and the branch's sha as the
    gate leaves it. The caller holds the merge lock."""
    set_phase(conn, run_id, "merge_gate", "pre-merge verify, then the autonomy gate")
    if sync_main:
        sha = _sync_main_into_branch(target, conn, run_id, provider, task_id,
                                     branch, wt, sha)
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


# The most of `git diff main...HEAD` a written-PR turn is shown, in
# characters; past it the diff is cut and the prompt says so (KO-336).
PR_TEXT_DIFF_CAP = 60_000
# The wall clock a written-PR turn gets, in minutes, unless less of the
# run's box is left: a description, not an implementation.
PR_TEXT_BUDGET_MIN = 5


def _written_pr_text(target, conn, run_id, task_id, task, branch, body,
                     beat_s, wt, started, budget_min, issue_url):
    """`[merge] pr_text = "written"`: one implementer turn writes the PR's
    title and body from the diff; `(title, body)`, or None when its reply
    could not be read or the turn ran out of time, with one printed line
    saying so.

    The turn is given the diff against `main` (capped at `PR_TEXT_DIFF_CAP`,
    with a note when cut), the ticket, the repository's `AGENTS.md` and
    `CLAUDE.md` when the worktree root has them, and the target's `pr_style`
    instructions; it answers with a line `TITLE: ...` and the body after it.
    The body carries `Linear: KO-n` and the issue URL as its last line, and
    no FINDINGS entry: the description is the repository's, the entry is the
    factory's. The budget is `PR_TEXT_BUDGET_MIN` or what is left of the
    run's box, whichever is less, and at least one minute.
    """
    diff = sh(["git", "diff", "main...HEAD"], cwd=wt)
    if len(diff) > PR_TEXT_DIFF_CAP:
        diff = (diff[:PR_TEXT_DIFF_CAP]
                + "\n\n[diff truncated here: the change is larger than this"
                " prompt can carry; describe what is shown]")
    parts = [
        f"Write the pull request title and description for branch {branch},"
        f" the candidate for ticket {task_id}: {task}.",
        "Answer with exactly one line `TITLE: ...` (the title alone, under"
        f" {pr.PR_TITLE_MAX} characters) followed by the description in"
        " Markdown. Describe what the diff changes and why, in this"
        " repository's own style; do not paste the ticket, and do not add"
        " a link to the ticket -- the loop appends one. Do not edit, commit"
        " or run anything: answer with the text only.",
    ]
    style = merge_config(target).pr_style.strip()
    if style:
        parts.append(f"Style instructions from the target's configuration:"
                     f"\n{style}")
    for name, text in babysitter.conventions(wt):
        parts.append(f"The repository's {name}:\n\n{text}")
    parts.append(f"The ticket:\n\n{body or task}")
    parts.append(f"The diff against main (`git diff main...HEAD`):\n\n"
                 f"```diff\n{diff}\n```")
    goal = "\n\n".join(parts)
    left = budget_min - (monotonic() - started) / 60
    minutes = max(1, min(PR_TEXT_BUDGET_MIN, int(left)))
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"writing the pull request text for {branch}"
                           " from the diff")
    reply = _timed(target, conn, run_id, beat_s, wt, minutes, goal)
    parsed = pr.parse_pr_text(reply) if reply is not None else None
    if parsed is None:
        why = ("the turn ran out of time" if reply is None
               else "the reply has no `TITLE:` line, an empty title, or a"
               f" title over {pr.PR_TITLE_MAX} characters")
        print(f"[holo2] written PR text refused for {task_id}: {why};"
              " opening the pull request with the ticket's title and body")
        return None
    title, text = parsed
    return title, pr.pr_body_written(text, task_id, issue_url)


def _open_pr(target, conn, run_id, task_id, task, branch, body, beat_s,
             wt=None, started=None, budget_min=None, issue_url=None):
    """`[merge] mode = "pr"`: push the approved candidate and open its pull
    request; return the PR's URL.

    `git push origin BRANCH`, then the PR with the title `KO-n: TITLE` and
    the ticket body plus the run's FINDINGS entry as its body -- in that
    order, so a PR never names a branch the remote does not hold. Either
    refusing is `InfraFailure` out of `holophyte.pr`: the route gave out,
    not the ticket, so no strike is spent and the branch and worktree stay
    exactly as after a refused merge. Nothing touches main.

    Under `[merge] pr_text = "written"` the title and body are what
    `_written_pr_text()` had one implementer turn write from the diff,
    before the push; a reply it cannot read is the ticket form above for
    this PR, so a PR is always opened (KO-336). `wt`, `started` and
    `budget_min` are that turn's worktree and box, and `issue_url` the
    link its body ends with; a direct call with none of them takes the
    ticket form.

    Both calls leave the machine and block for as long as the remote takes,
    so they run under `heartbeat_while()` like every other wait: a slow push
    is not a dead loop for the supervisor to sweep before the URL is on the
    run (KO-259 review round 1).
    """
    written = None
    if wt is not None and merge_config(target).pr_text == "written":
        written = _written_pr_text(target, conn, run_id, task_id, task,
                                   branch, body, beat_s, wt, started,
                                   budget_min, issue_url)
    # Still the `merge_gate` phase: the push and the create are the mode's
    # way out of the gate, named on the stream rather than as a phase move.
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"pushing {branch} to {pr.REMOTE} and opening its"
                           " pull request")
    with heartbeat_while(conn, run_id, beat_s):
        pr.push_branch(target, branch)
        print(f"[holo2] pushed {branch} to {pr.REMOTE}")
        now = int(time() * 1000)
        if written is not None:
            title, text = written
        else:
            title = pr.pr_title(task_id, task)
            text = pr.pr_body(conn, run_id, body, now)
        url = pr.create_pull_request(target, branch, title, text)
    print(f"[holo2] pull request open: {url}")
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"pull request open: {url}")
    return url


def _babysit(target, conn, run_id, provider, task_id, issue_id, task, branch,
              wt, sha, beat_s, url, ticket, verify_cmd, contracts, budget_min,
              criteria=(), approved=False, reviewed=None, verified=None):
    """Babysitter the pull request `url` until it merges or the run parks;
    return the merge commit's sha.

    Design note 7's second half, the `review -> eval -> fix -> reply ->
    watch` loop, capped by `[merge] pr_rounds`. Each pass reads the PR
    once (`pr.pr_state()`: unresolved threads, the head's check rollup,
    merged or closed) and is one `reviewRounds` row with route
    `github:LOGIN`, so FINDINGS shows it beside the Codex rounds. A pass
    with threads hands them to `_answer_threads()`: the adjudicator
    verdicts each, the fix round takes the accepted ones, replies and
    resolves follow, and a decline or a `HUMAN` parks the run with the
    thread listed. A pass with none waits for pending checks, then: red
    checks park; green ones are "ready to merge", which merges through the
    PR's merge API under `approve = "auto"` or after the operator's
    `--approve` (`approved`), and parks for the human otherwise. A fix
    round moves the candidate past the sha the reviewer approved, and the
    fix is the implementer's work nobody independent has judged: before
    the merge, `_review_fix()` reviews the candidate at its fixed sha, and
    anything but an approval parks the run (the operator's `--approve`
    was of the sha it released, so a candidate moved since is a human's
    to release again). `reviewed` is that sha as the caller knows it: the
    candidate just approved and verified on a fresh run, the park's
    `approvedSha` on a `--babysit` resume, None when nothing on record
    covers the branch -- which reads as "moved" and gets the review. Every
    park records it, so the next resume starts from the same fact.
    `verified` is the sha the merge gate's verify covered in this process
    -- the candidate a fresh run took through `_merge_gate()` before the
    PR opened, None on a resume, where the park's verify is a process
    old -- and the merge API is never called on any other sha: a
    candidate not verified here goes through `_merge_gate()` first, the
    ticket's verify commands and the drift check both, and a failure
    stops the run at the gate as it does under `mode = "local"`.
    `criteria` are the ticket's acceptance criteria, which the review of
    a fix is held to as a review round is. Past `pr_rounds` passes the
    run parks naming the cap. A PR someone merged
    by hand lands the run as merged with that sha; one closed unmerged
    fails it.

    Every park is `_park_on_pr()`: the ticket asks `PR open: URL` with the
    open threads listed, `runs.prUrl` and `runs.candidateSha` are written
    with the phase move, and `MergeParked` unwinds the run with the branch
    and worktree left standing. Nothing touches local main.
    """
    merge = merge_config(target)
    pull = pr.parse_pr_url(url)
    if pull is None:
        raise RunFailure(f"cannot read a pull request off {url!r};"
                         f" branch {branch} preserved at {sha[:12]}")
    model = agent_route(target, "adjudicate")
    # `reviewed`: the sha an independent judgement covers -- the reviewer's
    # approval or the operator's release. A fix round moves `sha` past it.
    for pass_no in range(1, merge.pr_rounds + 1):
        state = _settled_state(target, conn, run_id, beat_s, pull)
        if state.merged:
            print(f"[holo2] {pull.url} is already merged as"
                  f" {(state.merge_sha or '?')[:12]}")
            return state.merge_sha
        if state.closed:
            raise RunFailure(f"{pull.url} was closed without merging;"
                             f" branch {branch} preserved at {sha[:12]}")
        if state.head_sha and state.head_sha != sha:
            # The PR's head is not the candidate this run pushed: someone
            # else pushed to the branch. Its checks and threads are about
            # their commit, not the one verified and reviewed here, so
            # nothing is judged, fixed or merged on it -- the operator looks.
            _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                        f"the pull request's head is {state.head_sha[:12]},"
                        f" not the candidate {sha[:12]} this run pushed;"
                        " someone else pushed to the branch, and the"
                        " babysitter does not judge or merge their commit",
                        state.threads, reviewed=reviewed)
        rnd = len(store.read.rounds_of(conn, run_id)) + 1 if conn else pass_no
        if state.threads:
            sha = _answer_threads(target, conn, run_id, provider, task_id,
                                  branch, wt, sha, beat_s, pull, state, rnd,
                                  pass_no, model, ticket, verify_cmd,
                                  contracts, budget_min, reviewed=reviewed)
            continue
        reply = babysitter.round_reply(pull, pass_no, (), {}, state.checks, sha)
        record_round(target, conn, run_id, rnd, "review", reply, None, True,
                     "", started_at=int(time() * 1000),
                     route=babysitter.route_of(()))
        ledger(conn, run_id, task_id, "round",
               f"Babysit pass {pass_no} over {pull.url}: no unresolved"
               f" threads, checks {state.checks}", provider)
        if state.checks != "success":
            _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                        f"checks {state.checks} on the head commit", (),
                        reviewed=reviewed)
        print(f"[holo2] {pull.url} is ready to merge: checks green, no"
              " unresolved threads")
        if sha != reviewed:
            if merge.approve != "auto":
                _park_on_pr(target, conn, run_id, provider, task_id, branch, sha,
                            pull, f"{_moved(sha, reviewed)}, and a human"
                            " says merge on the candidate as it stands"
                            " ([merge] approve = \"human\")", (),
                            reviewed=reviewed)
            _review_fix(target, conn, run_id, provider, task_id, branch, wt,
                        sha, reviewed, beat_s, pull, ticket, verify_cmd,
                        contracts, criteria)
            # The review vouches for the fix, not for the gate: `verified`
            # stays behind, so the fixed candidate goes through
            # `_merge_gate()` below -- the drift check as well as the verify
            # -- before the merge API is called.
            reviewed = sha
        if merge.approve == "auto" or approved:
            if sha != verified:
                _merge_gate(target, conn, run_id, provider, task_id, issue_id,
                            branch, wt, beat_s, sha, verify_cmd, contracts,
                            sync_main=False)
                verified = sha
            return _merge_pr(target, conn, run_id, provider, task_id, branch,
                             wt, sha, beat_s, pull, reviewed=reviewed)
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    "ready to merge; waiting for a human to say merge"
                    " ([merge] approve = \"human\")", (), reviewed=reviewed)
    state = _settled_state(target, conn, run_id, beat_s, pull)
    _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                f"[merge] pr_rounds = {merge.pr_rounds} passes made; the"
                " babysitter stops here", state.threads, reviewed=reviewed)


def _moved(sha, reviewed):
    """Why the candidate at `sha` needs an independent look: it sits past
    the sha the last judgement covered, or nothing on record covers it."""
    if reviewed is None:
        return (f"no approval on record covers the candidate at {sha[:12]}"
                " (the last review asked for changes, or the park recorded"
                " none)")
    return (f"the fix rounds moved the candidate from {reviewed[:12]} to"
            f" {sha[:12]}; the release covered {reviewed[:12]}")


def _review_fix(target, conn, run_id, provider, task_id, branch, wt, sha,
                reviewed, beat_s, pull, ticket, verify_cmd, contracts,
                criteria=()):
    """The independent review of a candidate the babysitter's fix rounds
    moved from `reviewed` to `sha` (None: nothing on record covers it),
    before the merge API is called.

    The fix commits are the implementer's answer to the PR's threads; the
    reviewer's approval and the adjudicator's verdicts both came before
    them, so nothing independent has judged the candidate as it stands.
    The same reviewer route and brief as a review round: verify first,
    then a read-only review of the candidate at `sha` over the frozen
    `refs/review/*` pair, recorded as a `reviewRounds` row, and held to
    the ticket's `criteria` as a review round is: an approval that leaves
    a criterion not met, unwitnessed, or witnessed by a test the worktree
    does not hold is a `REQUEST_CHANGES` whatever its verdict line says.
    Anything but an approval parks the run on the PR with the findings in
    the ticket's question -- there is no further fix round here; the
    operator reads the findings and answers with `--babysit` or by
    hand."""
    set_phase(conn, run_id, "verifying", f"verify the fix at {sha[:12]}"
              " before its review")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        ledger(conn, run_id, task_id, "failure",
               f"FAILED verify before the review of the fix at {sha} on"
               f" {pull.url}; branch {branch} preserved, not merged\n\n{out}",
               provider)
        raise RunFailure(f"verify failed before the review of the fix on"
                         f" {pull.url}; branch {branch} preserved at"
                         f" {sha[:12]}")
    set_phase(conn, run_id, "reviewing", f"review of the fix at {sha[:12]}")
    base_sha = sh(["git", "merge-base", "main", sha], cwd=wt)
    rnd = _next_round(conn, run_id)
    round_started = int(time() * 1000)
    with heartbeat_while(conn, run_id, beat_s):
        verdict = agent(target, "review",
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate "
            "as the candidate in this repo against the ticket below. The "
            + (f"candidate was approved at {reviewed[:12]} and has since "
               "been moved by fix commits answering review threads on "
               if reviewed else
               "candidate has been moved by fix commits answering review "
               "threads, and the last review of it asked for changes, on ")
            + f"{pull.url}; nobody independent has judged those commits, so "
            "read the whole candidate, the fixes included. The ticket is "
            "the contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + _verify_brief(verify_cmd, ok, out)
            + criteria_brief(criteria)
            + "Do not modify anything. End your reply with exactly one "
            "line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha=base_sha, candidate_sha=sha)
    record_round(target, conn, run_id, rnd, "review", verdict, verify_cmd,
                 ok, out, started_at=round_started, criteria=criteria,
                 root=wt)
    # The same gate as a review round's: a criterion left not met or
    # unwitnessed is a blocker whatever the verdict line says.
    unwitnessed = criteria_findings(verdict, criteria, wt)
    if unwitnessed:
        print(f"[holo2] round {rnd}: {len(unwitnessed)} criteria not "
              "witnessed by the review of the fix; treating as "
              "REQUEST_CHANGES")
        verdict += "\n\n" + "\n".join(f["message"] for f in unwitnessed)
    if not unwitnessed and review_runner.terminal_verdict(verdict) == "APPROVE":
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: APPROVE of the fix at {sha} on {pull.url}\n"
               f"Reviewer verdict:\n{verdict}", provider)
        print(f"[holo2] the fix at {sha[:12]} is approved")
        return
    ledger(conn, run_id, task_id, "round",
           f"Round {rnd}: REQUEST_CHANGES on the fix at {sha} on"
           f" {pull.url}; not merged\nReviewer findings:\n{verdict}",
           provider)
    # No `reviewed`: the judgement on record is this rejection, so the
    # resume that follows reviews the candidate again before any merge.
    _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                f"the review of the fix at {sha[:12]} asked for changes;"
                f" not merged. Reviewer findings:\n{verdict}", ())


def _next_round(conn, run_id):
    """The number the run's next `reviewRounds` row takes; 1 with no
    store."""
    return len(store.read.rounds_of(conn, run_id)) + 1 if conn else 1


def _settled_state(target, conn, run_id, beat_s, pull):
    """One read of the PR, re-read while its checks are pending and it has
    no thread to answer -- every `pr.CHECK_POLL_S`, for at most
    `pr.CHECK_WAIT_S` -- under the heartbeat, so a long CI run is not a
    dead loop. Threads are answered without waiting: the fix they call for
    restarts the checks anyway."""
    waited = 0
    with heartbeat_while(conn, run_id, beat_s):
        state = pr.pr_state(target, pull)
        while (state.checks == "pending" and not state.threads
               and not state.merged and waited < pr.CHECK_WAIT_S):
            print(f"[holo2] checks pending on {pull.url}; waiting"
                  f" {pr.CHECK_POLL_S}s")
            pr.SLEEP(pr.CHECK_POLL_S)
            waited += pr.CHECK_POLL_S
            state = pr.pr_state(target, pull)
    return state


def _answer_threads(target, conn, run_id, provider, task_id, branch, wt, sha,
                    beat_s, pull, state, rnd, pass_no, model, ticket,
                    verify_cmd, contracts, budget_min, reviewed=None):
    """One pass over the PR's unresolved threads; return the candidate's
    sha after the fix round, or park.

    The adjudicator judges every thread against the ticket and the
    candidate (the same frozen `refs/review/*` pair a review round gets)
    and answers `ADDRESS`, `DECLINE` or `HUMAN` per thread; the pass is
    recorded as a round before anything is posted, so an interrupted pass
    has its row. A `HUMAN` verdict ends the pass with nothing posted: the
    run parks and the ticket's question quotes the thread. Otherwise the
    addressed threads go to one fix round (`_timed()`, the implementer),
    the verify commands run over the fix, the branch is pushed, and each
    addressed thread gets a reply naming the change and the sha and is
    resolved; each declined thread gets a reply with the reason and stays
    open. Every reply and resolve is a `runEvents` row. Declines park the
    run with those threads listed -- they are the reviewer's to close.

    Under `[merge] human_threads = "act"` a person's thread is judged too:
    an `ADDRESS` on it is fixed and answered like a bot's but never
    resolved, and any other verdict folds to `HUMAN`. A person's `HUMAN`
    parks after the fix round then, so a bot's defect is not held up by a
    person's question, and a pass that answered a person parks with their
    thread listed as left open for them to close. A bot's `HUMAN` still
    ends the pass before anything is posted.
    """
    threads = state.threads
    base_sha = sh(["git", "merge-base", "main", sha], cwd=wt)
    round_started = int(time() * 1000)
    # Under `human_threads = "park"` a thread a person opened is the
    # operator's whatever it says: it is HUMAN before the adjudicator is
    # asked, and the adjudicator sees the bots' threads alone, renumbered
    # so its reply and `parse_verdicts()` agree. Under `"act"` the person's
    # threads are judged too, but only an ADDRESS on one stands: anything
    # else folds to HUMAN, so a person is never declined. A deleted account
    # reads as a person: silence is the safe side.
    act = merge_config(target).human_threads == "act"
    judged = tuple(t for t in threads if act or t.author_kind == "bot")
    reply = "(no bot opened a thread; the adjudicator was not asked)"
    if judged:
        with heartbeat_while(conn, run_id, beat_s):
            reply = agent(target, "adjudicate",
                          babysitter.adjudication_brief(
                              pull, judged, ticket, sha,
                              babysitter.conventions(wt)),
                          wt, base_sha=base_sha, candidate_sha=sha)
    verdicts = _verdicts_by_kind(
        threads, judged, babysitter.parse_verdicts(reply, len(judged)))
    record_round(target, conn, run_id, rnd, "review",
                 babysitter.round_reply(pull, pass_no, threads, verdicts,
                                      state.checks, sha),
                 None, True, "", started_at=round_started,
                 route=babysitter.route_of(threads))
    ledger(conn, run_id, task_id, "round",
           f"Babysit pass {pass_no} over {pull.url}: {len(threads)}"
           f" unresolved thread(s), checks {state.checks}\n"
           + (f"{len(threads) - len(judged)} opened by a person, HUMAN"
              " before the adjudicator was asked\n" if not act else
              f"{sum(t.author_kind != 'bot' for t in threads)} opened by a"
              " person, judged (human_threads = act): ADDRESS is fixed and"
              " answered, anything else is HUMAN\n")
           + f"Adjudicator verdicts:\n{reply}", provider)
    by_verdict = {v: [(n, t, verdicts[n][1]) for n, t in
                      enumerate(threads, 1) if verdicts[n][0] == v]
                  for v in babysitter.VERDICTS}
    # A HUMAN verdict on a bot's thread ends the pass before anything is
    # posted, under either setting -- bot handling does not move. Only a
    # person's HUMAN under `act` waits: the bots' threads and the person's
    # ADDRESSes are fixed and answered first, and the pass parks after,
    # the person's HUMAN thread quoted, unanswered, and one that was
    # addressed listed as left open for them to close -- so the next pass
    # does not judge it again.
    if by_verdict["HUMAN"] and (not act or any(
            t.author_kind == "bot" for _, t, _ in by_verdict["HUMAN"])):
        _park_human(target, conn, run_id, provider, task_id, branch, sha, pull,
                    by_verdict["HUMAN"], threads, reviewed)
    if by_verdict["ADDRESS"]:
        sha = _fix_threads(target, conn, run_id, provider, task_id, branch,
                           wt, sha, beat_s, pull, by_verdict["ADDRESS"],
                           model, ticket, verify_cmd, contracts, budget_min)
    for _, thread, reason in by_verdict["DECLINE"]:
        _post(target, conn, run_id, beat_s, pull, thread,
              babysitter.declined_reply(model, reason), resolve=False)
    left_open = tuple(t for _, t, _ in by_verdict["DECLINE"]) + tuple(
        t for _, t, _ in by_verdict["ADDRESS"] if t.author_kind != "bot")
    if by_verdict["HUMAN"]:
        _park_human(target, conn, run_id, provider, task_id, branch, sha, pull,
                    by_verdict["HUMAN"],
                    tuple(t for _, t, _ in by_verdict["HUMAN"]) + left_open,
                    reviewed)
    if left_open:
        declined, answered = (len(by_verdict["DECLINE"]),
                              len(left_open) - len(by_verdict["DECLINE"]))
        why = [f"{declined} thread(s) declined and left open for their"
               " authors"] if declined else []
        why += [f"{answered} person's thread(s) addressed and left open for"
                " them to close"] if answered else []
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    "; ".join(why), left_open, reviewed=reviewed)
    return sha


def _park_human(target, conn, run_id, provider, task_id, branch, sha, pull,
                human, listed, reviewed):
    """Park the run on the threads the pass found `HUMAN`, each quoted in
    the ticket's question, with `listed` as the open threads."""
    quoted = "\n\n".join(babysitter.quoted(t) for _, t, _ in human)
    _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                "a thread needs a human's answer; nothing was posted on"
                f" it:\n{quoted}", listed, reviewed=reviewed)


def _verdicts_by_kind(threads, judged, parsed):
    """`{number: (verdict, reason)}` over all of `threads`, numbered as the
    round row lists them: a thread not in `judged` -- a person's (or one
    whose opener is unknown) under `human_threads = "park"` -- is `HUMAN`,
    "opened by a person"; a judged thread takes the next verdict off
    `parsed`, the adjudicator's verdicts over `judged` in order. A verdict
    on a person's thread that is not `ADDRESS` folds to `HUMAN`: the
    factory never declines a person, and the adjudicator's silence on
    them is not a licence either."""
    pending = iter(sorted(parsed))
    verdicts = {}
    for n, t in enumerate(threads, 1):
        if t not in judged:
            verdicts[n] = ("HUMAN", "opened by a person")
            continue
        verdict = parsed[next(pending)]
        if t.author_kind != "bot" and verdict[0] != "ADDRESS":
            verdict = ("HUMAN",
                       "a person's thread the adjudicator would not address")
        verdicts[n] = verdict
    return verdicts


def _fix_threads(target, conn, run_id, provider, task_id, branch, wt, sha,
                 beat_s, pull, addressed, model, ticket, verify_cmd,
                 contracts, budget_min):
    """The fix round for the addressed threads, then the push, then a reply
    on each and a resolve on each bot's; return the fixed candidate's
    sha."""
    fixes = _timed(target, conn, run_id, beat_s, wt, budget_min,
                   babysitter.fix_brief(pull, addressed, ticket))
    if fixes is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
        raise RunFailure(f"fix round for {pull.url} timed out or made no"
                         f" progress; branch {branch} preserved at"
                         f" {sha[:12]}")
    fixed = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    # The verify runs over the working tree, so it vouches for the commit
    # only when the tree is that commit: a fix half committed and half
    # left in the tree would verify green here and push a commit that
    # does not hold it -- and resolve the thread on it. The tree is left
    # as it is for a human; nothing is committed, deleted or pushed.
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
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        print(f"[holo2] verify FAILED after the fix round for {pull.url};"
              f" leaving branch {branch} at {fixed} for a human:\n{out}")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED verify after the fix round for {pull.url}; branch"
               f" {branch} preserved at {fixed}, not pushed\n\n{out}",
               provider)
        raise RunFailure(f"verify failed after the fix round for {pull.url};"
                         f" branch {branch} preserved at {fixed[:12]}")
    with heartbeat_while(conn, run_id, beat_s):
        pr.push_branch(target, branch)
    print(f"[holo2] pushed the fix round to {pr.REMOTE} at {fixed[:12]}")
    summaries = babysitter.parse_summaries(fixes)
    # A person's thread is theirs to close: the reply names the fix and
    # the sha, and the thread is left unresolved for its author.
    for n, thread, reason in addressed:
        _post(target, conn, run_id, beat_s, pull, thread,
              babysitter.addressed_reply(model, summaries.get(n, reason),
                                       fixed),
              resolve=thread.author_kind == "bot")
    return fixed


def _post(target, conn, run_id, beat_s, pull, thread, body, resolve):
    """Reply `body` on `thread`, resolving it when `resolve`; each call
    that landed is a `runEvents` row, so an interrupted pass can be read
    back from the stream."""
    with heartbeat_while(conn, run_id, beat_s):
        pr.reply_thread(target, pull, thread.id, body)
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "pull_request",
                               f"replied on thread {thread.url}:"
                               f" {babysitter.gist(body.splitlines()[-1])}")
        if resolve:
            pr.resolve_thread(target, pull, thread.id)
            if conn is not None and run_id is not None:
                store.record_event(conn, run_id, "pull_request",
                                   f"resolved thread {thread.url}")


def _merge_pr(target, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
              pull, reviewed=None):
    """The `merging` phase under `mode = "pr"`: the PR merged through the
    merge API -- never a local push of main -- pinned to the candidate `sha`
    the pass judged, then the worktree and local branch removed as after a
    local merge; return the merge commit's sha. GitHub declining the merge,
    the head having moved since the pass included, parks the run with its
    reason."""
    set_phase(conn, run_id, "merging", f"merging {pull.url} through the"
              " pull request API")
    try:
        with heartbeat_while(conn, run_id, beat_s):
            merge_sha = pr.merge_pull_request(target, pull, sha)
    except pr.MergeRefused as refused:
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    f"GitHub refused the merge: {refused}", (),
                    reviewed=reviewed)
    print(f"[holo2] merged {pull.url} as {merge_sha[:12]}")
    # Local main is not moved: the factory never pushes it, and pulling it
    # here would make the writer host's checkout the loop's business. The
    # worktree and the local branch hold nothing the PR does not.
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], target.path)
        sh(["git", "branch", "-D", branch], target.path)
    except RuntimeError as e:
        print(f"[holo2] post-merge cleanup left debris: {e}")
    return merge_sha


def _landed_pr(conn, run_id, provider, task_id, task, branch, url, merge_sha,
               started, budget_min, rnd):
    """The merged ledger line for a candidate that landed through its pull
    request; returns the merge sha, which is what the close-out stamps."""
    actual_min = (monotonic() - started) / 60
    ledger(conn, run_id, task_id, "merge",
           f"MERGED through {url} as {merge_sha} (branch {branch} deleted"
           " locally; local main not moved).\n"
           f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
           f"rounds: {rnd}", provider)
    print(f"[holo2] merged: {task}")
    return merge_sha


def _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                why, threads, reviewed=None):
    """Park the run on its pull request: the ticket asks `PR open: URL`
    with `why` and the open `threads` listed, `store.park()` writes
    `runs.prUrl`, `runs.candidateSha` and -- `reviewed`, the sha the last
    independent judgement covered, when there is one -- `runs.approvedSha`
    with the phase move, the ledger carries the same, and `MergeParked`
    unwinds the run with branch and worktree left standing. The operator's
    ways on are `--approve KO-n` (merge it) and `--babysit KO-n` (look
    again, which merges at `reviewed` alone and reviews anything else) --
    and, since KO-362, the loop's own tick: the park reads the pull
    request once more, *after* this pass's pushes and replies, and
    records its `updatedAt` and thread count on the run
    (`runs.prSeenAt`, `runs.prSeenThreads`, with the checks rollup and
    review decision beside them, KO-368), so the reconcile that sees
    the pull request move past them is seeing a reviewer, not the
    babysitter's own writes. A read that fails records nothing, and the
    reconcile then records without babysitting."""
    short = sha[:12] if sha else "an unrecorded sha"
    question = babysitter.open_threads_question(pull, why, threads)
    if conn is not None and run_id is not None:
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        if not block_ticket(conn, ticket_id, provider, question):
            print(f"[holo2] {task_id} could not be moved to"
                  " blocked_on_operator; parking the run anyway")
        store.park(conn, run_id, "awaiting_merge_approval",
                   f"{babysitter.gist(why)}; {branch} at {short} is open as"
                   f" {pull.url} ([merge] mode = \"pr\")",
                   candidate_sha=sha, pr_url=pull.url, approved_sha=reviewed,
                   pr_seen=_pr_seen(target, pull))
    print(f"[holo2] parked on {pull.url}: {babysitter.gist(why)}")
    ledger(conn, run_id, task_id, "note",
           f"PR OPEN: {pull.url}\n{why}\nBranch {branch} is pushed at {sha}"
           " and not merged ([merge] mode = \"pr\"). The run waits in"
           " awaiting_merge_approval; --approve merges it once green and"
           " quiet, --babysit looks at the threads again."
           + ("\nOpen threads:\n" + "\n".join(
               babysitter.thread_line(n, t) for n, t in enumerate(threads, 1))
              if threads else ""), provider)
    raise MergeParked(f"pull request open: {pull.url}; {babysitter.gist(why)};"
                      f" branch {branch} preserved at {short}")


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
        _resolve_merge_conflict(target, conn, run_id, provider, task_id,
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


def _resolve_merge_conflict(target, conn, run_id, provider, task_id, branch,
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


def self_hosted(target):
    """Whether `target` is the repository this very module was imported from.

    Decided once at startup by `main()`: a loop working on the factory's own
    checkout keeps running the pre-merge code after every merge, so each
    dogfooded fix is invisible to the loop that merged it until someone
    restarts it (the writer host, 2026-09-02: run 17 cut a worktree without the
    ticket id run 16 had just merged support for).
    """
    # This module lives in `holophyte/`, one level below the repository; the
    # comparison is against the repository, as it was when it lived in
    # `factory.py`.
    return Path(__file__).resolve().parent.parent == target.path.resolve()


def main(target, provider):
    """The loop: one process working the queue a ticket at a time under
    `[loop] workers = 1`, the default; a scheduler over a pool of
    `--worker` children above it (KO-343). Returns the exit status.

    The first thing the pass does is prove a configured `[agents]
    implementer` answers (KO-357): `check_agent_commands()` settled that the
    program resolves, and this settles that it runs and replies, by asking it
    for one word under a short cap. A route that does not answer ends the
    pass here, nonzero, with the command and what it said on the terminal --
    before a ticket is claimed, where being wrong costs a message rather
    than a lease held through a failed implement turn. The default route is
    not probed, and a `--worker` child does not repeat this: it enters
    through `worker()`, not here."""
    probe = probe_implementer(target)
    if probe is not None:
        print(probe.describe())
        if not probe.ok:
            return 1
    knobs = loop_config(target)
    if knobs.workers == 1:
        return _serial(target, provider, knobs)
    return scheduler(target, provider, knobs)


def _serial(target, provider, knobs):
    """One pass of the factory in this process: claim, mirror, lease,
    `run_task()`, close out, repeat. The phases are the plain functions
    below, called in the order they ran when this was one function
    (KO-211). The loop as it was before the pool: `[loop] workers = 1`
    runs exactly this, and a `--worker` child runs the same phases once
    in `worker()`."""
    restart_after_merge = self_hosted(target)
    stop_on_failure = knobs.stop_on_failure
    order = knobs.order
    # Whether any run this pass failed, for the exit code when the loop was
    # told to go on past failures: the shell still sees a nonzero status for
    # a night that was not clean.
    failed = False
    conn = open_store(target)
    try:
        # The provider knows its team by name rather than by id; the column's
        # contract is one row per Linear team, which the name keys just as
        # well until the provider resolves the id.
        project = store.ensure_project(conn, provider.team, target.path)
        seen = _startup_sweep(target, conn)
        _reconcile_at_startup(target, conn, project, provider)
        # The tickets this pass has refused to claim. A blocked ticket keeps
        # its place in the board's ready set — `blocked_on_operator` projects
        # to Todo, the column a human picks work out of — so it is offered
        # again the moment it is skipped. Remembering the refusal is what
        # turns "not this one" into "the one after it" instead of the same
        # ticket forever.
        skip = set()
        first_pass = True
        while True:
            # Before the claim: a pull request a person merged since the
            # last pass ships its parked run here (KO-359). The first pass
            # asked at startup, before the mirror was repaired.
            if not first_pass:
                # A ticket sent back to the babysitter for new review
                # activity (KO-362) may be one this pass parked and put in
                # `skip`; it is ready again, and this pass claims it.
                skip -= _reconcile_pull_requests(target, conn, project,
                                                 provider)
            first_pass = False
            _mirror_queue(target, conn, project, provider)
            task, ticket_id, run_id = _claim_next(target, conn, project,
                                                  provider, order, skip, seen)
            if not task:
                # The exit note, in the store before it is on the terminal:
                # a loop that was re-exec'd and found nothing to claim ends
                # here without ever heartbeating, and this is what tells the
                # sweep the restart came back.
                store.record_loop_return(conn, project)
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if failed else None
            if run_id is None:
                return
            merged = _dispatch(target, conn, run_id, provider, task, ticket_id)
            if merged is PARKED:
                # An approved candidate waiting for a person: not a failure,
                # so neither the stop nor the exit status is spent on it.
                # Its ticket is `blocked_on_operator`, which the claim path
                # refuses, so skipping it is only cheaper than refusing it.
                skip.add(task["id"])
                print(f"[holo2] {task['id']} parked awaiting merge approval;"
                      " continuing to the next ready ticket")
                continue
            if merged is SWEPT:
                # The sweep closed the run out and the loop honoured it by
                # stopping the turn; the ticket's mirror says what the sweep
                # left it saying, so it is not offered again this pass, and
                # a failure the sweep already counted is not counted twice.
                skip.add(task["id"])
                print(f"[holo2] {task['id']} was swept mid-turn; continuing"
                      " to the next ready ticket")
                continue
            if not merged:
                # The regenerated window stays uncommitted, like the preserved
                # branch it describes: a human closes both out. Nonzero so the
                # shell — and anything supervising it — sees the failure.
                if stop_on_failure:
                    return 1  # stop on first failure; ticket stays In Progress
                # `[loop] stop_on_failure = false`: the run is closed out
                # exactly as above, and the loop goes on to the next ready
                # ticket. The failed one is skipped for the rest of this
                # pass -- its mirror is `in_flight`, so the claim path would
                # refuse it anyway, but not offering it again is cheaper than
                # refusing it and the print is one line about the failure
                # rather than two.
                failed = True
                skip.add(task["id"])
                print(f"[holo2] {task['id']} failed; continuing to the next"
                      " ready ticket (stop_on_failure = false)")
                continue
            commit_findings(target,
                            f"Complete task {task['id']}: {task['title']}")
            if restart_after_merge:
                # Store and Linear are terminal for this run, the lease is
                # released and no worktree is open: the re-exec starts
                # exactly where the next pass would, from the merged code.
                # Only after a merge -- a failure returned above, which is
                # the intended stop.
                _reexec(target, conn, project)
                return  # only a test's EXEC returns
    finally:
        conn.close()


def _claim_next(target, conn, project, provider, order, skip, seen):
    """Walk the board's queue to the first ticket this process may run and
    lease it. Returns `(task, ticket_id, run_id)`: `task` None when the
    queue is exhausted, `run_id` None when the claim said stop for a human
    (`_claim_run()`). Every ticket refused on the way -- unadmitted, or
    leased by another run between the admission read and the claim -- is
    added to `skip`, so the caller's next ask is the one after it."""
    while True:
        task = provider.claim_next(skip=skip, order=order)
        if not task:
            return None, None, None
        ticket_id = _admit_ticket(target, conn, project, provider, task, seen)
        if ticket_id is None:
            skip.add(task["id"])
            continue
        run_id = _claim_run(target, conn, project, provider, task, ticket_id,
                            seen)
        if run_id is HELD:
            # Another loop on this target took the ticket between the
            # admission read and the claim: its work, not this loop's
            # problem. Skipped like a held ticket found at admission.
            skip.add(task["id"])
            continue
        return task, ticket_id, run_id


# --- the pool (KO-343) -------------------------------------------------------
#
# A worker's exit status is its one word back to the scheduler. `0` is a
# merge, as a clean process exit should be; `1` a failed run, the status the
# serial loop exits with on one and the one an uncaught exception exits a
# Python process with, so a worker that crashed outside `_dispatch()` reads
# as the failure it is. The other three are the scheduler's alone.
WORKER_MERGED = 0
WORKER_FAILED = 1
WORKER_PARKED = 2   # parked awaiting merge approval: not a failure
WORKER_IDLE = 3     # nothing left to claim
WORKER_STOP = 4     # the claim said stop for a human (`_claim_run()`)
# The environment variable a worker reads its slot number from, for the
# `[holo2 wN]` prefix on its lines: the children share the scheduler's
# stdout, and the prefix is what tells their lines apart in one log.
WORKER_SLOT_ENV = "HOLOPHYTE_WORKER"
# The seams the scheduler spawns and reaps through, so a test patches these
# and never `subprocess.Popen` or `os.wait` for the whole process.
SPAWN = subprocess.Popen


# How often the timed wait looks for an exited child, in seconds.
WAIT_POLL_S = 0.5


def _wait_any(children, timeout):
    """Block until any child exits, or `timeout` seconds pass; return
    `(pid, exit_code)`, or `(None, None)` when the deadline passed with no
    exit (KO-353). `timeout` is `None` for no deadline.

    `children` is the pool's live `Popen` objects by pid. The scheduler
    holds them for as long as the workers live -- a `Popen` dropped while
    its child runs is put on the module's housekeeping list, and the next
    `Popen()` reaps whatever on that list has exited, out from under this
    `os.wait()`: the worker becomes a phantom the pool waits on forever and
    its exit status is lost (the review of KO-343 reproduced it with two
    real children). Held, they are reaped here alone, and the one reaped is
    told its status so it is not put on that list when the pool drops it.
    Under a deadline the wait is `os.waitpid(-1, WNOHANG)` every
    `WAIT_POLL_S` until a child is reported or the deadline passes: there
    is no `os.wait()` with a timeout, and a signal-driven one would race
    a child that exited before the alarm was set.
    """
    if timeout is None:
        pid, status = os.wait()
    else:
        deadline = monotonic() + timeout
        while True:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid:
                break
            if monotonic() >= deadline:
                return None, None
            sleep(min(WAIT_POLL_S, max(deadline - monotonic(), 0)))
    code = os.waitstatus_to_exitcode(status)
    if pid in children:
        children[pid].returncode = code
    return pid, code


WAIT = _wait_any
# A worker runs no sweep of its own -- the scheduler swept once, and a
# second sweep would count one silence twice -- so its held-ticket lines
# have no sweep to point at.
NOTHING_SEEN = Sweep(0, (), False, (), ())


def worker(target, provider):
    """One `--worker` child: claim one ticket, run it, close it out, exit.

    The serial loop's phases once, less what the scheduler has already
    done -- the startup sweep, the reconcile, the queue mirror -- and less
    what belongs to the scheduler alone: no re-exec after a self-merge
    (the scheduler restarts once the pool has drained, so this worker
    finishes on the code it started with) and no exit note. Returns one
    of the `WORKER_*` statuses; the scheduler reads it from the exit code.
    """
    slot = os.environ.get(WORKER_SLOT_ENV)
    if slot:
        # Both streams: a traceback, or a verify line's stderr, lands in the
        # same shared log as the progress lines, and is only attributable to
        # this worker by the prefix (the review of KO-343, second round).
        sys.stdout = _PrefixedOut(sys.stdout, f"[holo2 w{slot}]")
        sys.stderr = _PrefixedOut(sys.stderr, f"[holo2 w{slot}]")
    knobs = loop_config(target)
    conn = open_store(target)
    try:
        project = store.ensure_project(conn, provider.team, target.path)
        task, ticket_id, run_id = _claim_next(target, conn, project, provider,
                                              knobs.order, set(), NOTHING_SEEN)
        if not task:
            print("[holo2] nothing left to claim; worker done.")
            return WORKER_IDLE
        if run_id is None:
            return WORKER_STOP
        merged = _dispatch(target, conn, run_id, provider, task, ticket_id,
                           refresh=False)
        if merged is PARKED:
            print(f"[holo2] {task['id']} parked awaiting merge approval")
            return WORKER_PARKED
        if not merged:
            _render_findings_locked(target, conn, run_id, task)
            return WORKER_FAILED
        _render_findings_locked(target, conn, run_id, task,
                                commit=f"Complete task {task['id']}: {task['title']}")
        return WORKER_MERGED
    finally:
        conn.close()


def _render_findings_locked(target, conn, run_id, task, commit=None):
    """A worker's rendering of FINDINGS.md, under the merge lock: the
    regeneration, and for a merged run its commit with `commit`'s message.

    The serial loop writes the window (and commits it, for a merged run)
    after its gate has let the lock go, which costs nothing when it is the
    only process in the checkout. A worker is not: a sibling can be merging
    in the same checkout at that moment, and a write to FINDINGS.md beside
    its merge dirties the checkout it is merging in or lands in its index,
    while a `git add`/`git commit` beside it is an index-lock failure for
    one of them (the review of KO-343, both rounds). So the write, and the
    commit when there is one, are one held span, the same lock the gate
    takes; a failed run's close-out passes `refresh=False` to
    `close_out_failure()` and renders here instead. A lock that cannot be
    had within the gate's wait leaves the window unrendered and says so:
    the run's outcome is in the store, and the next close-out in this
    checkout renders these rows with its own.
    """
    try:
        with merge_lock(target, run_id):
            refresh_findings(target, conn)
            if commit is not None:
                commit_findings(target, commit)
    except MergeLockHeld as e:
        what = "uncommitted" if commit is not None else "unrendered"
        print(f"[holo2] FINDINGS.md left {what} for {task['id']}: {e}")


class _PrefixedOut:
    """A text stream that starts every line with `prefix`, folding the
    factory's own `[holo2]` tag into it: `[holo2] run failed` from worker
    2 reads `[holo2 w2] run failed`, and any other line is prefixed whole.
    Writes are passed through as they come, so the line buffering the
    package set on the real stream still lands each line when it is said.
    """

    def __init__(self, stream, prefix):
        self.stream = stream
        self.prefix = prefix
        self.at_line_start = True
        # Whitespace written at a line start with no newline yet, such
        # as the indentation `traceback` writes before a source line: held
        # until the line shows what it is, so the prefix lands before it.
        self.held = ""

    def write(self, text):
        out = []
        for piece in text.splitlines(keepends=True):
            if self.at_line_start:
                piece = self.held + piece
                self.held = ""
                if not piece.strip():
                    if not piece.endswith(("\n", "\r")):
                        self.held = piece
                        continue
                elif piece.startswith("[holo2]"):
                    piece = self.prefix + piece[len("[holo2]"):]
                else:
                    piece = f"{self.prefix} {piece}"
            self.at_line_start = piece.endswith(("\n", "\r"))
            out.append(piece)
        return self.stream.write("".join(out))

    def __getattr__(self, name):
        return getattr(self.stream, name)


def scheduler(target, provider, knobs):
    """`[loop] workers > 1`: keep up to `knobs.workers` `--worker` children
    running, one per claimable ticket, until the queue is empty.

    The startup checks and the sweep once, then a tick per child exit:
    mirror the board's ready listing, count the tickets a worker could
    claim (`_claimable()`), spawn until `min(claimable, workers)` are
    alive, block until any child exits, read its status. While the pool
    is below the ceiling the block carries `knobs.tick_sec` as a deadline,
    and a deadline that reaps nobody is a tick like any other: the
    listing and the count run again for a ticket filed since (KO-353); a
    full pool waits on exits alone. A failed worker
    under `stop_on_failure` stops the spawning and the running workers are
    waited for, as the serial loop stops on its first failure; a merge into
    the factory itself does the same and re-execs once the pool has
    drained, so no worker ever runs code newer than the scheduler's. A
    worker that found nothing to claim is not a stop: the listing can run
    ahead of a claim a sibling is about to make, so the tick spawns nothing
    and the next exit recounts. Exits 0 with the queue empty and the pool
    drained, nonzero when any worker failed or stopped for a human.
    """
    conn = open_store(target)
    pool = {}  # pid -> (slot number, Popen), the live workers
    slots = iter(range(1, sys.maxsize))
    state = _PoolState(self_hosted(target), knobs.stop_on_failure)
    try:
        project = store.ensure_project(conn, provider.team, target.path)
        _startup_sweep(target, conn)
        _reconcile_at_startup(target, conn, project, provider)
        first_tick = True
        while True:
            # Every tick, timer or exit: a pull request merged on GitHub
            # since the last one ships its parked run (KO-359). The first
            # tick asked at startup, before the mirror was repaired.
            if not first_tick:
                _reconcile_pull_requests(target, conn, project, provider)
            first_tick = False
            listing = None
            if state.spawning:
                listing = _mirror_queue(target, conn, project, provider)
                if listing is None:
                    # The board could not be asked: an empty listing would
                    # end the loop reporting a queue it never saw. Nothing
                    # is spawned on it; a live pool recounts at its next
                    # exit, an empty one ends the loop nonzero, as the
                    # serial loop's claim ends it when the board is down.
                    state.unlisted()
                else:
                    # The claimable count leaves out the tickets the live
                    # workers hold -- a claim is a lease -- so the pool the
                    # queue can fill is the workers running plus what is
                    # still free to take, capped at the ceiling. Counting
                    # only the free tickets against the live pool never
                    # refilled a pool after its first exit.
                    want = min(len(pool) + _claimable(conn, project, listing),
                               knobs.workers)
                    while len(pool) < want:
                        slot = next(slots)
                        child = _spawn_worker(target, slot)
                        pool[child.pid] = (slot, child)
            if not pool:
                if listing is None and state.spawning:
                    print("[holo2] the board's ready listing failed and no"
                          " worker is running; stopping. relaunch once the"
                          " board answers")
                    return 1
                if state.restart and not state.stopped:
                    # Not after a stop: a restarted scheduler would know
                    # nothing of the failure, spawn again and exit clean
                    # under `stop_on_failure = true`. The operator relaunches
                    # on the merged code, as after a serial failure.
                    _reexec(target, conn, project)
                    return  # only a test's EXEC returns
                store.record_loop_return(conn, project)
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if state.failed else None
            timeout = None if len(pool) >= knobs.workers else knobs.tick_sec
            pid, code = WAIT({pid: child for pid, (_, child) in pool.items()},
                             timeout)
            if pid in pool:  # else the supervisor, another child, or a tick
                state.exited(pool.pop(pid)[0], code)
    finally:
        conn.close()


class _PoolState:
    """What the scheduler has learnt from its workers' exits: whether any
    failed (the exit status), whether it may still spawn, and whether it
    restarts once the pool has drained. A drain is for good -- a failure
    under `stop_on_failure`, a stop for a human, a self-merge -- while an
    idle worker only holds the next tick's spawning, since the listing
    can run ahead of a claim a sibling is about to make. The first two
    are a `stopped` drain: the loop ends nonzero when the pool is in, and
    a self-merge seen alongside does not restart it."""

    def __init__(self, restart_after_merge, stop_on_failure):
        self.restart_after_merge = restart_after_merge
        self.stop_on_failure = stop_on_failure
        self.failed = False
        self.stopped = False
        self.paused = False
        self.restart = False

    @property
    def draining(self):
        return self.stopped or self.restart

    @property
    def spawning(self):
        return not (self.draining or self.paused)

    def unlisted(self):
        """This tick's listing failed: no verdict on the queue, no spawn,
        and the loop's exit is nonzero whatever the pool goes on to do."""
        self.failed = True

    def exited(self, slot, code):
        """Read worker `slot`'s exit `code`; one printed line each."""
        self.paused = False
        if code == WORKER_MERGED:
            print(f"[holo2] worker {slot} merged its ticket")
            if self.restart_after_merge:
                # Workers mid-run finish on the code they started with; none
                # is started on it, and the scheduler restarts from the
                # merged code once the last one is in.
                self.restart = True
        elif code == WORKER_PARKED:
            print(f"[holo2] worker {slot} parked its ticket awaiting"
                  " merge approval")
        elif code == WORKER_IDLE:
            print(f"[holo2] worker {slot} found nothing to claim")
            self.paused = True
        elif code == WORKER_STOP:
            print(f"[holo2] worker {slot} stopped for a human")
            self.failed = self.stopped = True
        else:
            print(f"[holo2] worker {slot} failed (exit {code})")
            self.failed = True
            if self.stop_on_failure:
                self.stopped = True


def _claimable(conn, project, listing):
    """How many of the board's ready `listing` a worker could claim now:
    the store's own pickability -- mirrored `ready`, under no live run's
    lease, specced, and every dependency merged -- asked of the rows
    `_mirror_queue()` just refreshed. The store's word, not the board's:
    a ticket a failed run left `in_flight`, one parked on the operator, one
    whose body the validator refused or one waiting on a sibling all sit in
    the board's ready column, and a worker spawned for one of them would
    only refuse it (the review of KO-343 found the dependency clause
    missing here: a worker spawned for a ticket `pickable()` then refused).
    One store read for the tick, as the ticket asks: `pickable_tickets()`
    fetches the project's rows once and answers §2 for all of them in
    memory (the review's second round counted seven selects for five
    tickets when this asked `pickable()` one ticket at a time)."""
    verdicts = store.pickable_tickets(conn, project)
    return sum(1 for task in listing if verdicts.get(task["id"]))


def _spawn_worker(target, slot):
    """Start `factory.py TARGET --worker` as slot `slot`, sharing this
    process's stdout and stderr so one `tee` captures the whole pool;
    return the `Popen`, which the caller holds until `WAIT()` reports it
    (see `_wait_any()`). The command line is the scheduler's own,
    `--worker` appended, so the interpreter flags the operator launched
    with (`-u` above all) reach the child too."""
    program, argv = reexec_command()
    env = dict(os.environ, **{WORKER_SLOT_ENV: str(slot)})
    child = SPAWN([program, *argv[1:], "--worker"], env=env,
                  stdin=subprocess.DEVNULL)
    print(f"[holo2] started worker {slot} as pid {child.pid}")
    return child


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


# The mirror's status for each closed Linear state type: a ticket finished
# elsewhere is `merged`, one cancelled is `abandoned`.
RECONCILED_STATUS = {"completed": "merged", "canceled": "abandoned"}
RECONCILE_TRIGGER = {"completed": "linear_completed",
                     "canceled": "linear_cancelled"}


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
    `store.mirror_ticket()` itself, and dependencies are left as the store
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


def _reconcile_at_startup(target, conn, project, provider):
    """The two startup reconciles, GitHub before the board (KO-359 review).

    A person who merged a parked pull request on GitHub may have moved its
    ticket to Done on Linear as well. Asked first, the mirror reconcile
    would see Done, walk the ticket `merged` itself and take it out of the
    pull request reconcile's `blocked_on_operator` read: the run stayed
    parked with no outcome and no `mergeSha`, and Shipped never showed it.
    So the parked pull requests are read first and a merged one ships its
    run; the mirror repair then finds that ticket already `merged` and
    walks only the rest. Order alone is not enough: had GitHub failed on
    that first read, the mirror repair would still have seen Done and
    walked the ticket `merged` around its parked run, which no later pass
    could reach -- the pull request reconcile reads `blocked_on_operator`
    tickets only. So the mirror repair also leaves every ticket whose
    newest run is parked on a pull request to this reconcile, whatever
    the board says, and the next pass asks GitHub again.
    """
    _reconcile_pull_requests(target, conn, project, provider)
    _reconcile_mirror(conn, project, provider)


def _reconcile_mirror(conn, project, provider):
    """Walk the mirrored tickets Linear has since closed to their terminal
    status, one printed line each; nothing is written to Linear.

    The mirror is written when the loop claims a ticket and hears nothing
    when Linear later closes it elsewhere -- a ticket another target
    finished, or one the operator cancelled -- so it sat on the board as
    `ready` or `needs_spec` for good (KO-217, KO-137 and KO-138 on the
    daemon's board). Startup only, after the read-only sweep and the pull
    request reconcile (`_reconcile_at_startup()`): the five open statuses
    are read through the store for this project only (the provider knows
    one team, and another project's tickets are that project's loop to
    reconcile), a ticket with an active run is left to
    that run, and the provider is asked about the rest in one call. A
    closed one is walked along §3 edges (`walk_ticket`) with a `reconcile`
    intervention row on its most recent run first, in the same
    transaction -- record before acting. The row is re-read under that
    transaction's lock and must still be where the open read saw it, with
    no run: another process on the same store can claim or move a ticket
    while the provider is being asked, and a verdict on the stale read
    would mark a ticket merged under a live run. A ticket that never ran
    has no run to carry the row (`interventions.runId` is NOT NULL), so
    its printed line is its only record and says so. A ticket parked on a
    pull request is left to the pull request reconcile whatever the board
    says (KO-359 review): a Done there means a person merged the pull
    request, and only GitHub's answer closes the parked run out with its
    merge commit's sha -- so it stays `blocked_on_operator` until GitHub
    can be asked, rather than walked `merged` around a run no later pass
    would reach. A provider that
    cannot answer -- no network, no key -- skips the reconcile in one line
    and the loop goes on as before: this is a repair of the mirror, not a
    gate on the work.
    """
    tickets = []
    for ticket in store.read.open_tickets(conn, project):
        if ticket.activeRunId is not None:
            continue
        if ticket.status == "blocked_on_operator" \
                and _parked_pull_request(conn, ticket.id) is not None:
            # GitHub's verdict, not the board's: a Done here is a person
            # who merged the pull request, and `_reconcile_pull_requests()`
            # closes the run out with the merge commit's sha when GitHub
            # can be asked. Walking the ticket `merged` around a parked run
            # would strand that run (KO-359 review).
            print(f"[holo2] reconcile left {ticket.linearIdentifier} to its"
                  " pull request: the run parked on it is GitHub's to close")
            continue
        tickets.append(ticket)
    if not tickets:
        return
    try:
        closed = provider.closed_identifiers([t.linearIdentifier for t in tickets])
    except Exception as e:  # any transport failure: the board could not be asked
        print(f"[holo2] reconcile skipped: the board could not be asked which"
              f" mirrored tickets it has closed ({e})")
        return
    for ticket in tickets:
        state = closed.get(ticket.linearIdentifier)
        if state not in RECONCILED_STATUS:
            continue
        to_status = RECONCILED_STATUS[state]
        line = (f"[holo2] reconciled {ticket.linearIdentifier}:"
                f" {ticket.status} -> {to_status} (Linear {state})")
        with store.transaction(conn):
            now = store.read.ticket_by_id(conn, ticket.id)
            if now is None or now.status != ticket.status \
                    or now.activeRunId is not None:
                # Moved or claimed while the board was being asked: the
                # verdict was formed on a row that no longer holds.
                print(f"[holo2] reconcile left {ticket.linearIdentifier}"
                      f" alone: it moved while the board was asked")
                continue
            if now.lastRunId is not None:
                store.record_intervention(
                    conn, now.lastRunId, "reconcile",
                    f"Linear holds {ticket.linearIdentifier} {state};"
                    f" mirror walked {ticket.status} -> {to_status}",
                    source="supervisor", trigger=RECONCILE_TRIGGER[state])
            else:
                line += "; no run to record the intervention against"
            store.walk_ticket(conn, ticket.id, to_status)
        print(line)


# How the ticket's question begins once its pull request was closed on
# GitHub without merging: the run stays parked, and the skip line reads
# this rather than the `--approve` that would merge nothing.
PR_CLOSED_QUESTION = "PR closed without merge: "

# The question the merge gate parks a ticket on when merging `main` into the
# branch conflicts (KO-342); the skip line names the way back, `--requeue`
# (KO-365), rather than reading the question out.
GATE_CONFLICT_QUESTION = "merge conflict with main on: "


def _reconcile_pull_requests(target, conn, project, provider):
    """Ask GitHub about every pull request this project's parked runs wait
    on, and land the ones a person merged there (KO-359).

    A run parked on its pull request waits for `--approve`; the operator
    merges the pull request by hand after a coworker's review instead, and
    the run sat parked, the ticket In Progress, Shipped without it. This
    runs at loop startup, before the mirror reconcile so a ticket the
    merger also moved to Done still ships its run, and at the top of every
    later pass -- each serial claim, each scheduler tick -- over the
    project's `blocked_on_operator` tickets whose newest run holds a
    `prUrl` and is still parked in
    `awaiting_merge_approval`. One `pr.pull_status()` read per ticket. A
    merged pull request is that approval: `_land_github_merge()` ends the
    run merged with the merge commit's sha and walks the ticket to
    `merged`, Done on the board. One closed without merging leaves the run
    parked and makes the question `PR closed without merge: URL`
    (`_note_closed_pr()`); an open one changes nothing. A GitHub error is
    one printed line for that ticket and the pass goes on to the next, as
    the mirror reconcile skips a board that cannot be asked: this lands
    work already landed, it does not gate the work in the queue.

    Since KO-362 the same read also carries the pull request's
    `updatedAt` and review-thread count, held against what the last
    babysit pass recorded on the run (`runs.prSeenAt`,
    `runs.prSeenThreads`): an open pull request that moved past them has
    review activity nobody has answered, and `_rebabysit()` sends the
    run back to the babysitter exactly as `--babysit KO-n` does, at most
    once per `[merge] pr_poll_sec` per pull request. The read's
    `rateLimit` is remembered in `GITHUB_BUDGET`: under `RATE_FLOOR`
    points, the tick reads no pull request at all and prints one line
    naming the reset. Returns the Linear ids of the tickets sent back,
    so the serial loop can claim them again this pass.
    """
    sent = set()
    if _budget_low():
        return sent
    poll_ms = merge_config(target).pr_poll_sec * 1000
    for ticket in store.read.blocked_tickets(conn, project):
        if not ticket.prUrl or ticket.runId is None:
            continue
        pull = pr.parse_pr_url(ticket.prUrl)
        if pull is None or _parked_phase(conn, ticket.runId) is None:
            continue
        try:
            status = pr.pull_status(target, pull)
        except Exception as e:  # noqa: BLE001 - any transport failure
            print(f"[holo2] {ticket.linearIdentifier}: {pull.url} could not"
                  f" be read ({e}); the run stays parked")
            continue
        GITHUB_BUDGET.remember(status)
        low = _budget_low()
        if status.merged:
            _land_github_merge(target, conn, provider, ticket, pull, status)
        elif status.closed:
            _note_closed_pr(conn, ticket, pull)
        elif not low:
            # A babysit round is many reads and writes: not on a budget
            # that is already low.
            issue = _rebabysit(conn, ticket, pull, status, poll_ms)
            if issue is not None:
                sent.add(issue)
        if low:
            return sent
    return sent


# The GraphQL budget below which the tick stops reading parked pull requests
# until the reset GitHub named (KO-362). The budget is 5000 points an hour
# per token and each read here is one point; the babysitter's own rounds are
# the spend worth protecting, so the floor is well above one tick's reads.
RATE_FLOOR = 500


class GitHubBudget:
    """What the last parked pull request read said of the token's GraphQL
    budget: `remaining` points and the `reset_at` GitHub named, or None
    for either when nothing has been read yet or the answer carried no
    `rateLimit`. One per process: the token is the process's."""

    def __init__(self):
        self.remaining = None
        self.reset_at = None

    def remember(self, status):
        if status.rate_remaining is not None:
            self.remaining = status.rate_remaining
            self.reset_at = status.rate_reset

    def low(self, now=None):
        """Under `RATE_FLOOR` with the reset still ahead. A reset that has
        passed, or one GitHub did not name, forgets the reading: the next
        read learns the budget again."""
        if self.remaining is None or self.remaining >= RATE_FLOOR:
            return False
        reset = _iso_epoch(self.reset_at)
        if reset is None or (time() if now is None else now) >= reset:
            self.remaining = self.reset_at = None
            return False
        return True


GITHUB_BUDGET = GitHubBudget()


def _iso_epoch(text):
    """`text`, GitHub's ISO 8601 timestamp, as epoch seconds; None for
    anything else."""
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _budget_low():
    """Whether the tick's pull request reads stop here, with the one line
    that says so when they do."""
    if not GITHUB_BUDGET.low():
        return False
    print(f"[holo2] GitHub's GraphQL budget is down to"
          f" {GITHUB_BUDGET.remaining} points; no parked pull request is read"
          f" until it resets at {GITHUB_BUDGET.reset_at}")
    return True


def _pr_seen(target, pull):
    """`(updatedAt, thread count, checks, review)` as the pull request
    reads now -- `store.record_pr_seen()`'s tuple -- for the park to
    record after the pass's own writes; None when GitHub could not be
    asked, which the park records as nothing seen."""
    try:
        status = pr.pull_status(target, pull)
    except Exception as e:  # noqa: BLE001 - any transport failure
        print(f"[holo2] {pull.url} could not be read after the pass ({e});"
              " the park records no activity mark")
        return None
    GITHUB_BUDGET.remember(status)
    return _seen(status)


def _seen(status):
    """`store.record_pr_seen()`'s tuple from one `PullStatus`."""
    return (status.updated_at, status.threads, status.checks, status.review)


def _rebabysit(conn, ticket, pull, status, poll_ms):
    """Send the run parked on `pull` back to the babysitter when the pull
    request has review activity the last pass did not see; the ticket's
    Linear id when it was sent, None otherwise (KO-362).

    Activity is an `updatedAt` past the run's `prSeenAt` or a thread
    count above its `prSeenThreads`. A run with no `prSeenAt` -- parked
    by a module older than the column, or after a read that failed --
    has nothing to compare against: what the read saw is recorded and
    the tick moves on, so the next one can tell. A read that starts no
    round still refreshes the checks rollup and review decision beside
    the mark (`/attention`'s facts, KO-368) without moving the mark
    itself, so a check turning green or a review landing shows on the
    item as soon as the next tick reads it. The interval is per
    pull request, measured from the park (`runs.lastHeartbeat`, the
    park's stamp): activity within `poll_ms` of it is named and waits.
    Otherwise `store.babysit()`'s one transaction -- its
    intervention row, source `supervisor` (the loop's own machinery, not
    a person), naming what moved, the run ended with its resume
    point at the merge gate, the ticket walked to `ready` -- with the
    mark advanced in the same transaction, so the same activity is not
    sent twice; the loop's next claim resumes the candidate on its pull
    request and makes another round of passes, whose park records what
    it saw after its own writes. A refusal is the ticket having moved
    while GitHub was asked: one line, nothing written.
    """
    identifier, run_id = ticket.linearIdentifier, ticket.runId
    row = conn.execute("SELECT prSeenAt, prSeenThreads, lastHeartbeat,"
                       " (SELECT linearIssueId FROM tickets WHERE id = ?)"
                       " FROM runs WHERE id = ?",
                       (ticket.id, run_id)).fetchone()
    if row is None or status.updated_at is None:
        return None
    seen_at, seen_threads, parked_ms, issue = row
    mark = _seen(status)
    if seen_at is None:
        store.record_pr_seen(conn, run_id, mark, parked_only=True)
        return None
    grew = (status.threads is not None and seen_threads is not None
            and status.threads > seen_threads)
    if status.updated_at <= seen_at and not grew:
        store.record_pr_seen(conn, run_id, mark, parked_only=True,
                             facts_only=True)
        return None
    waited_ms = int(time() * 1000) - (parked_ms or 0)
    if waited_ms < poll_ms:
        store.record_pr_seen(conn, run_id, mark, parked_only=True,
                             facts_only=True)
        print(f"[holo2] {identifier}: {pull.url} has new review activity;"
              f" the next babysit round waits"
              f" {-(-(poll_ms - waited_ms) // 1000)}s ([merge] pr_poll_sec)")
        return None
    threads = "?" if status.threads is None else status.threads
    note = (f"new review activity on {pull.url}: updated"
            f" {status.updated_at} (last seen {seen_at}), {threads} review"
            f" threads (last seen {seen_threads})")
    try:
        with store.transaction(conn):
            store.record_pr_seen(conn, run_id, mark)
            store.babysit(conn, ticket.id, note, source="supervisor")
    except store.ApproveRefused as refused:
        print(f"[holo2] {identifier}: {pull.url} has new review activity but"
              f" the ticket moved while GitHub was asked ({refused}); left"
              " alone")
        return None
    print(f"[holo2] {identifier}: {pull.url} has new review activity"
          f" (updated {status.updated_at}, {threads} review threads); run"
          f" {run_id} sent back to the babysitter")
    return issue


def _parked_phase(conn, run_id):
    """The run's `(branch, phase)` if it is parked awaiting merge approval,
    None otherwise: the reconcile acts on that run alone."""
    row = conn.execute("SELECT branch, phase FROM runs WHERE id = ?",
                       (run_id,)).fetchone()
    if row is None or row[1] != "awaiting_merge_approval":
        return None
    return row


def _parked_pull_request(conn, ticket_id):
    """The `prUrl` of the ticket's newest run when that run is parked
    awaiting merge approval on a pull request, None otherwise: the ticket
    the pull request reconcile owns and the mirror reconcile leaves."""
    row = conn.execute(
        "SELECT r.prUrl FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.id = ? AND r.phase = 'awaiting_merge_approval'"
        " AND r.prUrl IS NOT NULL", (ticket_id,)).fetchone()
    return None if row is None else row[0]


def _land_github_merge(target, conn, provider, ticket, pull, status):
    """Close out the run parked on `pull` as merged: a person merged it on
    GitHub, and that is the `--approve` the park was waiting for.

    One transaction, record before acting: the ticket and the run are
    re-read under the write lock and must still be where the open read saw
    them -- parked, no live run, the same newest run -- or another process
    moved them while GitHub was being asked and nothing is written. Then
    an `approve` intervention naming who merged it, the run released
    `merged` with the pull request's merge commit as `mergeSha`
    (`awaiting_merge_approval -> done`), the question cleared and the
    ticket walked to `merged`. Outside the lock: the board's Done, the
    merged ledger line with the merger's login, the local worktree and
    branch removed as `_merge_pr()` removes them after an API merge (a
    refusal is debris, not a failure), and the findings window rendered so
    the run appears in Shipped.
    """
    identifier, run_id = ticket.linearIdentifier, ticket.runId
    who = status.merged_by or "someone"
    sha = status.merge_sha
    short = sha[:12] if sha else "an unrecorded sha"
    with store.transaction(conn):
        now = store.read.ticket_by_id(conn, ticket.id)
        parked = _parked_phase(conn, run_id)
        if now is None or now.status != "blocked_on_operator" \
                or now.activeRunId is not None or now.lastRunId != run_id \
                or parked is None:
            print(f"[holo2] {identifier}: {pull.url} is merged on GitHub but"
                  " the ticket moved while it was asked; left alone")
            return
        branch = parked[0]
        store.record_intervention(
            conn, run_id, "approve",
            f"{pull.url} merged on GitHub by {who} as {short}; the run is"
            " closed out as merged", source="human", trigger="manual")
        store.release(conn, run_id, "merged", merge_sha=sha)
        conn.execute("UPDATE tickets SET blockedQuestion = NULL WHERE id = ?",
                     (ticket.id,))
        store.walk_ticket(conn, ticket.id, "merged")
    mirror_push(conn, ticket.id, provider)
    ledger(conn, run_id, identifier, "merge",
           f"MERGED through {pull.url} as {sha} by {who} on GitHub (branch"
           f" {branch} deleted locally; local main not moved).", provider)
    if branch:
        try:
            sh(["git", "worktree", "remove", "--force",
                str(worktree_path(target, branch))], target.path)
            sh(["git", "branch", "-D", branch], target.path)
        except RuntimeError as e:
            print(f"[holo2] post-merge cleanup left debris: {e}")
    refresh_findings(target, conn)
    print(f"[holo2] {identifier}: {pull.url} was merged on GitHub by {who}"
          f" as {short}; run {run_id} closed out as merged")


def _note_closed_pr(conn, ticket, pull):
    """The pull request was closed on GitHub without merging: the run stays
    parked -- the branch and its candidate are still a person's to decide
    on -- and the ticket's question becomes `PR closed without merge: URL`,
    which the skip line then reads. Idempotent: a question already saying
    so is left as it is, so the pass after this one writes and prints
    nothing. The run's event stream carries the change first."""
    question = f"{PR_CLOSED_QUESTION}{pull.url}"
    if (ticket.blockedQuestion or "").startswith(question):
        return
    with store.transaction(conn):
        store.record_event(conn, ticket.runId, "pull_request",
                           f"{pull.url} was closed on GitHub without"
                           " merging; the run stays parked for a person")
        conn.execute("UPDATE tickets SET blockedQuestion = ? WHERE id = ?"
                     " AND status = 'blocked_on_operator'",
                     (question, ticket.id))
    print(f"[holo2] {ticket.linearIdentifier}: {pull.url} was closed on"
          " GitHub without merging; the run stays parked")


def skip_line(identifier, strikes, pr_url, question):
    """The admit step's one line for a ticket the store holds parked.

    Pure, so the wording is tested without a store. A pull request wins:
    the run behind it is parked alive, so its URL and the `--approve` that
    merges it are the whole story whatever failed before it -- unless the
    question says the pull request was closed without merging
    (`PR_CLOSED_QUESTION`), when there is nothing an `--approve` would
    merge and the question is the line. A merge-gate conflict
    (`GATE_CONFLICT_QUESTION`) names its way back, `--requeue` once the
    branch is resolved (KO-365). Then the
    question a module parked the ticket on -- `merge?`, say --
    first line only, and *before* the strike count: the run that parked it
    may also have been the failure that reached `MAX_FAILED_RUNS`, and the
    conflict is what the operator has to resolve, not the count. The
    strike form is for the escalation's own park, whose question
    `is_strike_question()` recognises, or for a park with no question at
    all once the count has tripped; a park with neither is still a
    human's, and says so.
    """
    closed = (question or "").strip().startswith(PR_CLOSED_QUESTION)
    if pr_url and not closed:
        return (f"{identifier} is parked on PR {pr_url} awaiting"
                f" --approve {identifier}; skipping it")
    if (question or "").strip().startswith(GATE_CONFLICT_QUESTION):
        return (f"{identifier} is parked on a merge-gate conflict; resolve"
                f" the branch and --requeue {identifier}; skipping it")
    if question and question.strip() and not is_strike_question(question):
        first = question.strip().splitlines()[0]
        return (f"{identifier} is parked on a question: {first};"
                " skipping it")
    if strikes >= MAX_FAILED_RUNS:
        return (f"{identifier} struck out after {strikes} failures;"
                " a human owns it now")
    return f"{identifier} is parked for a human; skipping it"


def _admit_ticket(target, conn, project, provider, task, seen):
    """The questions asked of a ticket before the lease and before any run
    row exists. Returns the mirrored ticket id, or None for a ticket this
    pass refuses -- `main()` skips it and takes the next one.

    Before the lease, before the mirror: a ticket that has already
    burned its attempts is refused here rather than claimed and then
    discovered to be unworkable, so the escalation costs no run row
    of its own. `escalate()` blocks it if this is the pass that
    crossed the threshold, and says so again on every later pass —
    which is what makes a Linear state a human dragged back to Todo
    unable to buy the ticket another run.

    Skipped rather than stopped on, which is not the call the rest
    of this loop makes: a stop here would be permanent. The ticket
    sorts where it sorts and is offered first on every invocation,
    so stopping on it would starve every ticket behind it until a
    human noticed — and a ticket parked *for* a human is the one
    case where there is nothing for this loop to wait on.
    The mirror comes first, and the questions are asked of the row
    it leaves: the live body is what the run would work from, and
    the row a previous pass left behind can say `ready` about a
    ticket whose criteria or verify command have since been edited
    out. `mirror_ticket()` is an upsert with no lease, so a ticket
    refused below has cost nothing but a refreshed row.
    """
    # First question, asked of the body itself: does it pass the
    # template validator? The store's gates below judge the row —
    # criteria present, a verify command present — and a body with
    # both can still be unfilled template (KO-165: placeholders in
    # the title, the summary and the first criterion, no What
    # line). One printed line names the first problem, the mirror
    # lands in `needs_spec` as an under-specced body would, and the
    # next candidate is tried; no run row is opened for it. The
    # target's path goes along so a body naming a path this
    # repository gitignores is refused here too (KO-222).
    problem = body_problem(task, target.path)
    if problem:
        mirror_task(conn, project, task, specced=False)
        print(f"[holo2] {task['id']} skipped: {problem}")
        return None
    ticket_id = mirror_task(conn, project, task)
    # The lease is per ticket (KO-341): a ticket another live run holds
    # is that run's, and the answer is the next candidate, not a stop.
    # Asked here, before `pickable()`, so the refusal reads as the lease
    # it is -- the same sentence `store.claim()` uses when the race is
    # lost a moment later -- and points at the startup sweep, which is
    # where the holder's last signs of life are.
    held = store.read.ticket_by_id(conn, ticket_id).activeRunId
    if held is not None:
        _skip_held(f"ticket {task['id']}: lease already held by run {held}",
                   seen)
        return None
    # The board's lease (KO-351): another writer's store is not readable
    # from here, but its `holo:HOST` label is, and a ticket carrying one
    # is that writer's for as long as the label stays. Asked after the
    # store's own lease so a ticket this store holds reads as the store
    # lease it is. This writer's own label is not asked about here: the
    # store just said no live run holds the ticket, so one is stale, and
    # `_lease_on_board()` takes it off once the store lease is held.
    others = foreign_lease_holders(task.get("labels"), lease_host(target))
    if others:
        print(f"[holo2] {task['id']} is leased by {others[0]} on the board;"
              " skipping it")
        return None
    if escalate(conn, ticket_id, provider):
        # The skip is the same whatever parked the ticket; the line says
        # which (KO-345): a strike-out, a pull request awaiting `--approve`,
        # or a question -- so a ticket parked for the operator's merge is
        # not reported as a failure that never happened.
        ticket = store.read.ticket_by_id(conn, ticket_id)
        pr_url = None
        if ticket.lastRunId is not None:
            row = conn.execute("SELECT prUrl FROM runs WHERE id = ?",
                               (ticket.lastRunId,)).fetchone()
            pr_url = row[0] if row else None
        print("[holo2] " + skip_line(task["id"],
                                     len(failure_history(conn, ticket_id)),
                                     pr_url, ticket.blockedQuestion))
        return None
    # Same place, the store's own question: §2's `pickable()`. The
    # board and the store can disagree about whether a ticket is
    # workable — a failed run leaves its mirror `in_flight` on
    # purpose, and a body edit or a hand-dragged column offers the
    # ticket again as ready — and claiming on the board's word alone
    # produced a run row that existed only to be refused by the
    # `in_flight` transition below (holophyte-bugs.md #4). Asked
    # before the claim so the refusal costs no run row and no
    # failure, and asked of the row just mirrored so an under-specced
    # body is refused whether the ticket is new or was `ready` last
    # time the store saw it: the `ready -> in_flight` move below does
    # not re-read the lists, and this is the only gate that does.
    #
    # A skipped ticket is still re-projected. The refusal used to fall
    # out of the claim, and the claim's refusal path pushed the store's
    # status back at the board as one more attempt at unsticking it —
    # a `merged` ticket whose Done push never landed is offered again
    # *because* the board is behind, and skipping it silently would
    # leave it Ready on the board for good. Best-effort, like every
    # push: a status with no board state (`needs_spec`) pushes nothing.
    verdict = store.pickable(conn, ticket_id)
    if not verdict:
        status = store_status(conn, ticket_id)
        print(f"[holo2] {task['id']} is {status} in the store,"
              f" not claimable ({verdict.reason}); skipping it")
        mirror_push(conn, ticket_id, provider)
        return None
    return ticket_id


class _Held:
    """`_claim_run()`'s answer for a ticket another live run holds: its
    own object, because the loop skips to the next candidate rather than
    stopping as it does for None."""


HELD = _Held()


def _skip_held(refusal, seen):
    """One line for a ticket another run holds, and a pointer at the
    startup sweep when it had anything to say about the holder."""
    print(f"[holo2] {refusal}; skipping it")
    if seen.trips or seen.watched:
        print("[holo2] the sweep above shows the lease holder's"
              " last signs of life")


def _refuse_claim(conn, task, run_id, reason):
    """Give the store lease of a run the board would not lease back --
    `infra`, no work started, no strike -- and say the loop stops."""
    refused = InfraFailure(reason)
    store.release(conn, run_id, "failed", str(refused),
                  outcome_class=outcome_class_of(refused))
    print(f"[holo2] {task['id']}: {refused}; stopping for a human")
    return None


def _lease_on_board(target, conn, provider, task, ticket_id, run_id):
    """The board half of the claim (KO-351), under the store lease run
    `run_id` just took: take off a stale label of this writer's, add the
    label, read the issue's labels back, decide. Returns True when the
    claim stands, `HELD` when another writer holds the ticket, None when
    the loop must stop.

    The order is the whole design. The store lease is the atomic one, so
    it goes first and nothing here touches the board without it. This
    writer's own `holo:HOST` label already on the listing is stale -- the
    store, asked at admission and again by the claim, has no live run
    behind it: a run that ended with the board down -- and comes off now,
    under the lease that proves no sibling loop on this store holds the
    ticket, before the fresh one is written; a board that will not release
    it leaves a `warning` row, and the add below re-asserts the same name.
    Then the add and the read-back, four ways:

    - The add raises: a raise is not proof that nothing landed -- Linear
      can apply the mutation and then time out on the response -- so this
      writer's label is taken off best-effort, once, and then the store
      lease goes back and the loop stops for a human. A label left
      behind by a refused claim would otherwise be a lease every other
      writer honours until a human notices it.
    - The read-back raises: the add may have landed, so the same
      best-effort removal, once, and then the same release and stop.
    - The read-back shows another writer's `holo:` label, taken between
      the listing and this write: that writer holds the ticket, whichever
      add landed first. This writer's own label comes off -- only that one
      -- the store lease goes back without a strike, the skip line names
      the holder, and the loop takes the next ticket. (Two writers reading
      each other back both yield; the ticket is free again and the next
      pass takes it.)
    - The read-back shows no other writer: the claim stands.
    """
    issue_id, label = task["issue_id"], lease_label(target)
    if lease_host(target) in lease_holders(task.get("labels")):
        print(f"[holo2] {task['id']} carries this writer's lease label {label}"
              " with no live run; removing the stale label and claiming")
        drop_lease_label(conn, ticket_id, provider, issue_id, label)
    try:
        provider.label_issue(issue_id, label)
        have = provider.issue_labels(issue_id)
    except Exception as e:  # noqa: BLE001 - the add may have landed either way
        drop_lease_label(conn, ticket_id, provider, issue_id, label)
        return _refuse_claim(conn, task, run_id, "the board did not take the"
                             f" lease label {label} ({e}); no work started")
    others = foreign_lease_holders(have, lease_host(target))
    if others:
        drop_lease_label(conn, ticket_id, provider, issue_id, label)
        store.release(conn, run_id, "failed",
                      f"the board showed {others[0]}'s lease label at claim;"
                      " no work started", outcome_class="infra")
        print(f"[holo2] {task['id']} is leased by {others[0]} on the board;"
              " skipping it")
        return HELD
    return True


def _claim_run(target, conn, project, provider, task, ticket_id, seen):
    """The lease, the board's lease label and the `ready -> in_flight`
    move. Returns the claimed run id, `HELD` when another run or another
    writer took the ticket first, or None when the loop must stop rather
    than start a run."""
    # The store half and the board half of the lease under one turn
    # (`lease_turn()`): a close-out of this store looks at the store and
    # then takes its label off the board under the same turn, so no claim
    # can land -- lease taken, label written -- between that look and
    # that removal and have its fresh label stripped.
    with lease_turn(target):
        try:
            run_id = store.claim(conn, project, ticket_id)
        except store.ClaimConflict as e:
            # Before any branch or worktree exists: another loop on this
            # target won the race for this ticket, so this one moves on
            # to the next. The lease is the ticket's, so working beside
            # the holder on a different ticket is the design, not a
            # conflict.
            _skip_held(str(e), seen)
            return HELD
        # The board half of the lease, right after the store half and
        # before the run is anything another writer could collide with.
        leased = _lease_on_board(target, conn, provider, task, ticket_id,
                                 run_id)
    if leased is not True:
        return leased
    # §3's `ready -> in_flight`, and the first thing the board is told
    # about this run: the claim is the moment the ticket starts being
    # worked, and the projection replaces the state call the provider
    # used to make on its own.
    if not mirror_status(conn, ticket_id, "in_flight", provider):
        # The store refused the move, so this ticket is not `ready`
        # and no work may start on it. The ordinary cause is a board
        # that is behind the store — a ticket already `merged` whose
        # Done push did not land is still non-terminal in Linear and
        # so is offered again on the next pass — and §1 is exactly
        # that the store, not the column, decides. Running anyway
        # would re-implement merged work, once per pass for as long
        # as the board stays stale.
        #
        # So: give the lease straight back, re-project the status the
        # ticket really has as one more best-effort attempt at
        # unsticking the board, and stop. Stopping rather than taking
        # the next ticket is the same call as a refused claim above —
        # store and board disagree about what is workable, and a
        # human wants to know — and it is also what keeps a stale
        # ticket the re-push cannot move (an unmapped status, a Linear
        # that is down) from being claimed round and round forever.
        refused = InfraFailure("ticket was not ready when the run"
                               " was claimed; no work started")
        store.release(conn, run_id, "failed", str(refused),
                      outcome_class=outcome_class_of(refused))
        release_lease_label(target, conn, ticket_id, provider, run_id)
        # This refusal is a failed run, but an `infra` one: no work
        # started, so it says nothing about the ticket and does not
        # count towards parking it. The threshold is still checked
        # here, for the `work` failures already on the ticket. A
        # ticket the escalation just parked was pushed to the board
        # by that move; the re-push is only for one that stayed where
        # it was.
        if not escalate(conn, ticket_id, provider):
            mirror_push(conn, ticket_id, provider)
        print("[holo2] claimed ticket is not in a status work starts"
              " from; stopping for a human")
        return None
    return run_id


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


def _reexec(target, conn, project):
    """Replace the process image with a fresh `factory.py` from the merged
    code, through the `EXEC` seam. Returns only when a test's EXEC does."""
    sha = sh(["git", "rev-parse", "--short", "HEAD"], target.path)
    # The note the supervisor watches for, written before the exec because
    # nothing can be written after a failed one: the sweep reports this
    # restart if no claim, heartbeat or exit note follows it within the
    # grace window.
    store.record_loop_restart(conn, project, sha)
    conn.close()
    reexec_self("merged a change to the factory itself;"
                f" re-executing from {sha}", EXEC)


def report(target, conn=None, out=None, now=None):
    """Print the target store's estimate-vs-actual table. Returns nothing.

    `--report`'s whole body: it reads rows and prints them, so no ticket is
    claimed, no worktree is cut and no provider is imported -- which is what
    makes it safe to run against the store of a loop that is still working.

    The one write it can make is `open_store()`'s migration: a store older
    than the run row's estimate column is brought up to the schema this
    queries instead of failing on the missing column, and the round counts an
    older module never stamped are recomputed from the rounds themselves
    rather than reported as zero. A target with no store at all is not created
    for the sake of an empty table; it is reported.

    Below the table, one line naming the `[report] findings` mode, so an
    operator can see whether this target has opted into rendering
    FINDINGS.md at close-out (`repo`) or not (`none`), then one on the target's
    supervisor -- see `supervisor_liveness_line()`, always last. `now` is
    the clock the heartbeat's age is taken against, injectable so a test
    can place a beat in time.
    """
    out = out or sys.stdout
    if conn is None and not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return
    owned = conn is None
    conn = conn if conn is not None else open_store(target)
    try:
        print("\n".join(report_lines(conn, target)), file=out)
        print(f"findings: {report_config(target).findings}", file=out)
        print(supervisor_liveness_line(target, conn, now), file=out)
    finally:
        if owned:
            conn.close()


def requeue(target, identifier, note, out=None, provider=None):
    """Put the failed ticket `identifier` back in the queue. Returns nothing.

    `--requeue`'s whole body, and off every other mode's write path: it opens
    the store, does `store.requeue()`'s one transaction, prints the requeued
    line and exits. The identifier is the Linear one (`KO-n`), resolved in
    this target's store; an identifier the store has not mirrored, or one it
    holds more than once, is a `SystemExit` naming it, as is every refusal
    `store.requeue()` makes -- and in all of those nothing is written. A
    target with no store has nothing to requeue and says so the same way.

    With a `provider`, the board lease label comes off too (KO-351): this
    writer's `holo:HOST`, taken off before the store's transaction makes
    the ticket claimable, and only while the store names no other live run
    on the ticket, so a claim that follows finds its own label untouched
    whatever the order of the two. Best-effort: the
    failed run's close-out should already have removed it, and a board that
    was down then gets one more chance here; one still down leaves a label
    this writer's next claim treats as stale.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        failed_run = _requeue_candidate(conn, ticket_id)
        if failed_run is not None:
            release_lease_label(target, conn, ticket_id, provider, failed_run)
        try:
            run_id = store.requeue(conn, ticket_id, note)
        except (store.RequeueRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} requeued after run {run_id}", file=out)
    finally:
        conn.close()


def _requeue_candidate(conn, ticket_id):
    """The failed run `store.requeue()` would requeue `ticket_id` after, or
    None when it would refuse: a read of the same rows, made first so the
    run's lease label can come off while the ticket is still `in_flight`.
    `store.requeue()` re-reaches the verdict inside its own transaction."""
    ticket = store.read.ticket_by_id(conn, ticket_id)
    if ticket is None or ticket.activeRunId is not None \
            or ticket.lastRunId is None:
        return None
    row = conn.execute("SELECT outcome, outcomeReason FROM runs WHERE id = ?",
                       (ticket.lastRunId,)).fetchone()
    if not row or row[0] != "failed":
        return None
    if ticket.status == "in_flight" or (
            ticket.status == "blocked_on_operator"
            and store.is_gate_conflict(row[1])):
        return ticket.lastRunId
    return None

def approve(target, identifier, note, out=None):
    """Release the ticket `identifier` parked for merge approval. Returns
    nothing.

    `--approve`'s whole body, `--requeue`'s twin: it opens the store, does
    `store.approve()`'s one transaction -- the `approve` intervention row
    carrying `note`, the parked run ended with its resume point at the merge
    gate, the ticket walked to `ready` -- prints what it did and exits. The
    loop's next claim of the ticket takes the preserved candidate straight to
    the merge gate. Every refusal `store.approve()` makes is a `SystemExit`
    naming the ticket's state, and nothing is written then; an identifier the
    store has not mirrored, or a target with no store, is refused the same
    way.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id = store.approve(conn, ticket_id, note)
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} approved: run {run_id} released from"
              " awaiting_merge_approval and the ticket is ready; the loop's"
              " next claim resumes its candidate at the merge gate",
              file=out)
    finally:
        conn.close()


def babysit_ticket(target, identifier, note, out=None):
    """Send the ticket `identifier`, parked on its pull request, back to the
    babysitter. Returns nothing.

    `--babysit`'s whole body and `approve()`'s twin: `store.babysit()`'s
    one transaction -- its intervention row carrying `note`, the
    parked run ended with its resume point at the merge gate, the ticket
    walked to `ready` -- printed and done. The loop's next claim of the
    ticket resumes the candidate on its PR and makes another round of
    passes: new threads verdicted and answered, checks awaited; a PR that
    comes up ready under `[merge] approve = "human"` parks again for the
    human's `--approve`. The refusals are `--approve`'s, as `SystemExit`.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id = store.babysit(conn, ticket_id, note)
        except (store.ApproveRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} sent back to the babysitter: run {run_id}"
              " released from awaiting_merge_approval and the ticket is"
              " ready; the loop's next claim resumes its candidate on the"
              " pull request", file=out)
    finally:
        conn.close()


def repoint(target, identifier, sha, note, out=None):
    """Move the ticket `identifier`'s parked candidate to `sha`. Returns
    nothing.

    `--repoint`'s whole body, `--approve`'s sibling for the rebuilt-branch
    case: it opens the store, does `store.repoint()`'s one transaction --
    the `repoint` intervention row carrying `note`, the narrative event
    naming both shas, `candidateSha` moved -- prints the old and new shas
    and exits. The branch itself is the operator's git work, done before
    this; the merge gate the next approval resumes into holds the branch to
    the new sha exactly as it held it to the old. Every refusal
    `store.repoint()` makes is a `SystemExit` naming the ticket and the
    reason, and nothing is written then; an identifier the store has not
    mirrored, or a target with no store, is refused the same way.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id, old_sha = store.repoint(conn, ticket_id, sha, note)
        except (store.RepointRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} re-pointed: run {run_id}'s candidate"
              f" moved from {old_sha} to {sha}; the merge gate now holds the"
              " branch to the new sha", file=out)
    finally:
        conn.close()


def _operator_store(target):
    """The store an operator command writes to, or the exit for a target
    that has none: nothing to requeue, approve or re-point, and no file
    made for the sake of saying so."""
    if not target.store_path.exists():
        raise SystemExit(f"[holo2] no store at {target.store_path}")
    return open_store(target)


def _ticket_by_identifier(target, conn, identifier):
    """The store's ticket id for the Linear identifier `KO-n`, or the exit
    for one the store has not mirrored or holds more than once."""
    rows = conn.execute(
        "SELECT id FROM tickets WHERE linearIdentifier = ?",
        (identifier,)).fetchall()
    if not rows:
        raise SystemExit(
            f"[holo2] {identifier}: no such ticket in {target.store_path}")
    if len(rows) > 1:
        raise SystemExit(
            f"[holo2] {identifier} names {len(rows)} tickets in"
            f" {target.store_path}; refusing to pick one")
    return rows[0][0]
