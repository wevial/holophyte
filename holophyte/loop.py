"""The loop: worktree setup and reuse, `run_task`, `main`, `report`, the re-exec.

`main()` drives one pass of the factory -- claim, mirror, lease, `run_task()`,
close out, repeat -- and re-executes `factory.py` through the `EXEC` seam
(`reexec_self` from `holophyte.reexec`) after merging a change to the factory
itself (`self_hosted()`). `run_task()`
is the loop body: the worktree (`reuse_leftover()` for a leftover,
`run_worktree_setup()` for the `[worktree] setup` table, checked at startup by
`check_worktree_setup()`), the implement/review/adjudicate turns, the verify
gate, the `--no-ff` merge. `report()` is `--report`'s whole body. Imports the
package modules, `store`, `store.read`, `review_runner`, `provider` and the
standard library; nothing from `factory`.

Seventh and last slice of the phase-2 module split; moved verbatim from
`factory.py`, which is now the entry point that imports `holophyte.cli`.
"""
import os
import re
import subprocess
import sys
from pathlib import Path
from time import monotonic, time

import review_runner
import store
import store.read
from holophyte.agents import agent
from holophyte.board import (
    block_ticket,
    body_problem,
    close_out_failure,
    escalate,
    ledger,
    merge_drift,
    mirror_key,
    mirror_push,
    mirror_status,
    mirror_task,
    release_run,
    store_status,
)
from holophyte.config import (
    branch_prefix,
    loop_config,
    merge_config,
    setup_commands,
    setup_timeout,
    sweep_config,
)
from holophyte.findings import commit_findings, refresh_findings
from holophyte.gates import (
    InfraFailure,
    MergeParked,
    RunFailure,
    outcome_class_of,
    run_verify,
    sh,
)
from holophyte.reexec import reexec_self
from holophyte.report import report_lines
from holophyte.review import criteria_brief, criteria_findings
from holophyte.runs import (
    MAX_ROUNDS,
    heartbeat_while,
    open_store,
    record_round,
    set_phase,
    warn_on_run,
)
from holophyte.supervisor import (
    SWEEP_HINT,
    supervisor_liveness_line,
    sweep,
    sweep_lines,
)

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


def check_worktree_setup(target):
    """Parse the `[worktree] setup` table before the loop claims work.

    `check_agent_commands()`'s sibling, here for the same reason: a table read
    for the first time inside a run would abandon a claimed ticket, a cut
    branch and a held project lease over something startup could have said in
    one sentence. It parses through `setup_commands()`, so a table this
    accepts is exactly a table a run would accept.

    What it deliberately does not settle is the commands themselves. They are
    shell, not argv -- `run_verify()` runs them the way it runs a ticket's
    verify command -- and they are written against a worktree that does not
    exist yet, so there is nothing here to resolve them against. Startup
    settles the shape of the table; the worktree settles the rest. The cap
    the commands run under is checked here too, for the same reason.
    """
    setup_commands(target)
    setup_timeout(target)
    branch_prefix(target)


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
    otherwise stall the ticket on every rerun. A merge conflict, and a
    worktree sitting off the branch while the branch holds commits of its
    own, are a human's calls and are refused with the state named. Nothing
    is ever deleted here.
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
        # in preserves the commits and restores the invariant; a conflict is
        # a human's merge to resolve, refused with the tree put back.
        r = subprocess.run(["git", "-c", "user.name=holophyte",
                            "-c", "user.email=holophyte@factory.invalid",
                            "merge", "--no-edit", "main"],
                           cwd=wt, capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=wt,
                           capture_output=True)
            return False, (f"preserved commits on {branch} conflict with a"
                           " main that moved on; a human resolves the merge"
                           " before this ticket is run again")
        print(f"[holo2] merged the moved-on main into preserved branch"
              f" {branch}")
    return True, ""


def run_task(target, task, conn=None, run_id=None, provider=None):
    """Run `task` through `_run_stages()`, and stop if the store ended the run.

    The one catch for `store.RunEnded`. The supervisor's `act_on_trip()` --
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
    wt = target.worktrees / slug
    # The approved candidate: `--approve` ended the ticket's parked run with
    # its resume point at the merge gate, and its worktree still stands.
    # Nothing to implement or review -- the candidate was, and a person said
    # merge -- so the run reuses the worktree and goes straight to the gate.
    carried = _approved_candidate(conn, run_id)
    if carried is not None and wt.exists():
        return _resume_at_merge_gate(
            target, conn, run_id, provider, task_id, issue_id, task, branch,
            wt, carried, started, verify_cmd, contracts, budget_min)
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
    sha = _implement(target, conn, run_id, task, branch, wt, fresh, beat_s,
                     start_sha, ticket, verify_cmd, budget_min)

    # 2. review rounds (MAX_ROUNDS). Verify runs before each review and its
    # result goes into the brief; every round that is not a clean approval —
    # round 2 included — gets a fix round, because a round-2 blocker is the
    # cheapest fix in the loop and used to need a human to close it out.
    sha, rnd, approved = _review_rounds(
        target, conn, run_id, provider, task_id, branch, wt, beat_s, base_sha,
        sha, ticket, verify_cmd, contracts, criteria, budget_min)
    if not approved:
        _terminal_adjudication(target, conn, run_id, provider, task_id, task,
                               branch, wt, beat_s, base_sha, sha, ticket,
                               verify_cmd, contracts)

    # 4. pre-merge verify (catches fix-round regressions), then merge. Both
    # happen under `merge_gate`: §4's gate node is the one edge out of a
    # passing review, and this verify is the mechanical half of what the gate
    # asks. Under the `personal` autonomy profile the human half is a no-op,
    # so the run passes through the node rather than around it and a failed
    # pre-merge verify is a run stopped at the gate.
    ok = _merge_gate(target, conn, run_id, provider, task_id, issue_id, branch,
                     wt, beat_s, sha, verify_cmd, contracts)
    # The human half of the gate, when the target asks for one: the
    # candidate is approved and verified, and a person says "merge".
    if merge_config(target).approve == "human":
        _park_for_approval(conn, run_id, provider, task_id, branch, sha)
    return _land(target, conn, run_id, provider, task_id, task, branch, wt,
                 sha, ok, started, budget_min, rnd)


def _approved_candidate(conn, run_id):
    """The ticket's prior run whose approved candidate this run carries, or
    None: a direct call with no store carries nothing."""
    if conn is None:
        return None
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    return store.read.approved_candidate(conn, ticket_id, run_id)


def _resume_at_merge_gate(target, conn, run_id, provider, task_id, issue_id,
                          task, branch, wt, carried, started, verify_cmd,
                          contracts, budget_min):
    """The approved candidate's run: the preserved worktree, the pre-merge
    verify against the main of today, the merge. No implementer, no reviewer.

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
    why = _candidate_drift(wt, branch, carried.sha)
    if why is not None:
        ledger(provider, task_id, f"FAILED to merge the approved candidate"
                                  f" for: {task}\n{why}\nNothing was"
                                  " committed or deleted; a human reconciles"
                                  " the worktree before this ticket is run"
                                  " again.")
        raise RunFailure(f"approved candidate on {branch} is not what was"
                         f" approved: {why}")
    ok, why = reuse_leftover(target, wt, branch)
    if not ok:
        ledger(provider, task_id, f"FAILED to reuse the approved candidate's"
                                  f" worktree for: {task}\n{why}\nNothing"
                                  " was deleted.")
        raise RunFailure(f"cannot reuse the approved candidate's worktree:"
                         f" {why}")
    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if sha == sh(["git", "rev-parse", "main"], target.path):
        ledger(provider, task_id, f"FAILED to merge the approved candidate"
                                  f" for: {task}\n{branch} holds nothing"
                                  " beyond main; nothing to merge.")
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
    ok = _merge_gate(target, conn, run_id, provider, task_id, issue_id, branch,
                     wt, beat_s, sha, verify_cmd, contracts)
    return _land(target, conn, run_id, provider, task_id, task, branch, wt,
                 sha, ok, started, budget_min, 0)


def _candidate_drift(wt, branch, approved):
    """Why the worktree at `wt` is not the candidate `approved` names, or
    None when it is: a clean tree with HEAD and `branch` both on that sha.

    `approved` is None only for a run parked by a module older than
    `runs.candidateSha`; with nothing recorded there is nothing to hold the
    tree to, so the refusal names that instead of merging on trust.
    """
    if approved is None:
        return ("the park recorded no candidate sha, so nothing vouches for"
                f" what {branch} now holds")
    dirty = sh(["git", "status", "--porcelain"], cwd=wt)
    if dirty:
        return (f"the worktree holds uncommitted changes on top of the"
                f" approved {approved[:12]}:\n{dirty}")
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if head != approved:
        return (f"the worktree is at {head[:12]}, not the approved"
                f" {approved[:12]}")
    tip = sh(["git", "rev-parse", "--verify", "--quiet",
              f"refs/heads/{branch}"], cwd=wt)
    if tip != approved:
        return (f"branch {branch} is at {tip[:12]}, not the approved"
                f" {approved[:12]}")
    return None


def _land(target, conn, run_id, provider, task_id, task, branch, wt, sha, ok,
          started, budget_min, rnd):
    """The merge and the merged ledger line; returns the merge commit's sha.
    Shared by the ordinary run and the approved candidate's."""
    merge_sha = _merge(target, conn, run_id, provider, task_id, task, branch,
                       wt, sha)
    # Nothing tells Linear the ticket is done here any more. The merge makes
    # the ticket `merged` in the store, and `main()` projects that status onto
    # the board through `mirror_push()` once the run has been released — one
    # writer of the workflow state instead of a call from the middle of a run
    # that has not finished ending yet.
    # One greppable line of timing data per merged ticket: the estimate stays
    # write-only otherwise, and a future burndown script reads this format.
    actual_min = (monotonic() - started) / 60
    ledger(provider, task_id, f"MERGED to main (branch {branch} deleted). "
                 f"Verify: {'passed' if ok else 'n/a'}.\n"
                 f"actual: {actual_min:.1f} min · estimate: {budget_min} min · "
                 f"rounds: {rnd}")
    # The task's own commit of FINDINGS.md is `main()`'s, not this frame's:
    # the run's close-out entry exists only once the run has been released,
    # which happens after this returns.
    print(f"[holo2] merged: {task}")
    # The merge commit itself, for the close-out to stamp on the run: truthy,
    # so every caller that read this as "did it merge" still does.
    return merge_sha


def _cut_worktree(target, conn, run_id, provider, task_id, task, branch, wt):
    """The worktree phase: cut `branch` at `wt`, or reuse the leftover there.

    Returns whether the worktree is fresh -- holds nothing beyond main -- so
    the close-outs after it neither claim preservation over nothing nor keep
    an empty leftover alive forever.
    """
    # §4's one edge out of `claimed`, taken before the first git command:
    # cutting the worktree is already this run doing the ticket's work, so a
    # crash in it belongs to `working` and not to a run that still looks
    # freshly claimed.
    set_phase(conn, run_id, "working", f"cutting {branch} and implementing")
    if wt.exists():
        # leftover from a previous failed run — reuse it so preserved work
        # survives; the branch check below still gates on commits.
        ok, why = reuse_leftover(target, wt, branch)
        if not ok:
            ledger(provider, task_id, f"FAILED to reuse leftover worktree for: {task}\n"
                                      f"{why}\nNothing was deleted.")
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
        ledger(provider, task_id, f"FAILED to cut a fresh worktree for: {task}\n"
                                  f"{why}\nNothing was deleted.")
        raise RunFailure(f"cannot cut a fresh worktree: {why}")
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
        ledger(provider, task_id,
               f"FAILED worktree setup for: {task}\nNo agent ran;"
               f" branch {branch} holds nothing and is"
               f" discarded.\n\n{out}")
        sh(["git", "worktree", "remove", "--force", str(wt)], target.path)
        sh(["git", "branch", "-D", branch], target.path)
        raise InfraFailure("worktree setup failed; no agent ran and the"
                           " empty branch was discarded")
    ledger(provider, task_id, f"FAILED worktree setup for: {task}\nNo agent ran; "
                              f"reused worktree {wt} left in place with its "
                              f"work.\n\n{out}")
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
    try:
        with heartbeat_while(conn, run_id, beat_s):
            return agent(target, "implement", goal, wt,
                         timeout=budget_min * 60)
    except subprocess.TimeoutExpired as expired:
        print(f"[holo2] task exceeded {budget_min} min budget")
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        partial = partial.strip()[-2000:]
        print("[holo2] implementer output before the budget fired:\n"
              + (partial or "(no output before the budget fired)"))
        return None


def _implement(target, conn, run_id, task, branch, wt, fresh, beat_s,
               start_sha, ticket, verify_cmd, budget_min):
    """The implementer phase: one turn against `ticket`, then the no-commit
    gate. Returns the candidate's sha."""
    commands = (f"\n\nThese verify commands must pass before review and again "
                f"before merge:\n\n{verify_cmd}" if verify_cmd else "")
    out = _timed(target, conn, run_id, beat_s, wt, budget_min,
                 f"Implement this task in this repo:\n\n{ticket}{commands}\n\n"
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


def _review_rounds(target, conn, run_id, provider, task_id, branch, wt, beat_s,
                   base_sha, sha, ticket, verify_cmd, contracts, criteria,
                   budget_min):
    """The review phase: up to MAX_ROUNDS of verify, review and fix round.

    Returns `(sha, rnd, approved)`: the candidate's sha after the last fix
    round, the number of the round that ended the phase, and whether that
    round was a clean approval. `approved` False means both rounds and their
    fixes are spent and the terminal adjudication is next.
    """
    for rnd in range(1, MAX_ROUNDS + 1):
        set_phase(conn, run_id, "verifying", f"round {rnd}: verify before review")
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
        ledger(provider, task_id, f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
                     f"Reviewer findings:\n{verdict}\n\n"
                     f"Implementer response:\n{fixes}")
        if fixes is None or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            raise RunFailure(f"fix round {rnd} timed out or made no progress;"
                             f" branch {branch} preserved at {sha[:12]}")
        sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    return sha, rnd, False


def _terminal_adjudication(target, conn, run_id, provider, task_id, task,
                           branch, wt, beat_s, base_sha, sha, ticket,
                           verify_cmd, contracts):
    """3b. Terminal adjudication: both review rounds and their fixes are
    spent, so one fresh independent run issues a bare verdict on the
    final state. There is no further fix round under any outcome —
    anything but PASS preserves the branch and stops the loop.
    """
    set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        print(f"[holo2] verify FAILED before adjudication; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human:\n{out}")
        ledger(provider, task_id,
               f"FAILED verify before terminal adjudication after "
               f"{MAX_ROUNDS} review rounds; branch {branch} preserved "
               f"at {sha}\n\n{out}")
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
    record_round(target, conn, run_id, MAX_ROUNDS + 1, "adjudicate", reply,
                 verify_cmd, ok, out, started_at=round_started)
    try:
        decision = review_runner.terminal_verdict(
            reply, review_runner.ADJUDICATION_VERDICTS)
    except review_runner.ReviewBoundaryError:
        decision = "MALFORMED"  # no clean verdict — read as FAIL
    if decision != "PASS":
        print(f"[holo2] terminal adjudication: {decision}; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human. Task: {task}")
        ledger(provider, task_id,
               f"Terminal adjudication after {MAX_ROUNDS} review "
               f"rounds: {decision}; branch {branch} preserved at "
               f"{sha}\n\nAdjudicator reply:\n{reply}")
        raise RunFailure(f"terminal adjudication: {decision};"
                         f" branch {branch} preserved at {sha[:12]}")
    print("[holo2] terminal adjudication: PASS")
    ledger(provider, task_id, f"Terminal adjudication after {MAX_ROUNDS} review "
                 f"rounds: PASS\n\nAdjudicator reply:\n{reply}")


def _merge_gate(target, conn, run_id, provider, task_id, issue_id, branch, wt,
                beat_s, sha, verify_cmd, contracts):
    """The `merge_gate` phase: the pre-merge verify, then the drift check.
    Returns the verify's `ok`, for the merged ledger line."""
    set_phase(conn, run_id, "merge_gate", "pre-merge verify, then the autonomy gate")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
        print(f"[holo2] verify FAILED before merge; leaving branch {branch} "
              f"at {sha} for a human:\n{out}")
        ledger(provider, task_id, f"FAILED verify before merge; branch {branch} "
                                  f"preserved at {sha}\n\n{out}")
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
        ledger(provider, task_id, "MERGE REFUSED: the ticket drifted from the contract "
                                  f"this run was claimed under ({', '.join(drift)}). "
                                  f"Branch {branch} preserved at {sha}. Work it again "
                                  "against the body as it now reads, or restore the "
                                  "body the run was claimed under.")
        raise RunFailure(f"ticket drifted from the claimed contract"
                         f" ({', '.join(drift)}); branch {branch} preserved"
                         f" at {sha[:12]}")
    return ok


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
    ledger(provider, task_id, "AWAITING MERGE APPROVAL: review approved and "
                              f"verify passed; branch {branch} preserved at "
                              f"{sha} and not merged ([merge] approve = "
                              "\"human\"). Answer merge? to release it.")
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
        _resolve_merge_conflict(target, provider, task_id, branch, sha)
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


def _resolve_merge_conflict(target, provider, task_id, branch, sha):
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
    ledger(provider, task_id, f"MERGE ABORTED: conflict on {paths}. Branch "
                              f"{branch} preserved at {sha}. Rebase it on main "
                              "and re-run, or merge it by hand.")
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
    """One pass of the factory: claim, mirror, lease, `run_task()`, close out,
    repeat. The phases are the plain functions below, called in the order
    they ran when this was one function (KO-211)."""
    restart_after_merge = self_hosted(target)
    knobs = loop_config(target)
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
        # The tickets this pass has refused to claim. A blocked ticket keeps
        # its place in the board's ready set — `blocked_on_operator` projects
        # to Todo, the column a human picks work out of — so it is offered
        # again the moment it is skipped. Remembering the refusal is what
        # turns "not this one" into "the one after it" instead of the same
        # ticket forever.
        skip = set()
        while True:
            task = provider.claim_next(skip=skip, order=order)
            if not task:
                # The exit note, in the store before it is on the terminal:
                # a loop that was re-exec'd and found nothing to claim ends
                # here without ever heartbeating, and this is what tells the
                # sweep the restart came back.
                store.record_loop_return(conn, project)
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if failed else None
            ticket_id = _admit_ticket(target, conn, project, provider, task)
            if ticket_id is None:
                skip.add(task["id"])
                continue
            run_id = _claim_run(conn, project, provider, ticket_id, seen)
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


def _admit_ticket(target, conn, project, provider, task):
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
    if escalate(conn, ticket_id, provider):
        print(f"[holo2] {task['id']} is blocked by repeated failures;"
              " skipping it. a human owns it now")
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


def _claim_run(conn, project, provider, ticket_id, seen):
    """The lease and the `ready -> in_flight` move. Returns the claimed run
    id, or None when the loop must stop rather than start a run."""
    try:
        run_id = store.claim(conn, project, ticket_id)
    except store.ClaimConflict as e:
        # Before any branch or worktree exists: another loop holds the
        # project, so this one stops rather than working beside it.
        # The startup sweep's sighting turns the dead end into an
        # instruction: is the holder alive, and what to type if not.
        print(f"[holo2] claim refused, not starting a run: {e}")
        if seen.trips or seen.watched:
            print("[holo2] the sweep above shows the lease holder's"
                  " last signs of life")
        return None
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


class _Parked:
    """`_dispatch()`'s answer for a run parked awaiting merge approval:
    falsy, because nothing merged, and its own object, because the loop
    goes on rather than stopping on a failure."""

    def __bool__(self):
        return False


PARKED = _Parked()


def _dispatch(target, conn, run_id, provider, task, ticket_id):
    """One run of `task` under `run_id`, with its failure accounting and
    close-out. Returns whether the run merged, or `PARKED` for a run stopped
    at the gate by `[merge] approve = "human"`.

    `run_task()` answers with the merge commit's sha when it merged, and
    that sha is what the release stamps on the run; a bare `True` (the
    supervisor ended the run as merged, or a test's stand-in) merges the
    run without one."""
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
        # instead of a traceback with the reason lost to
        # release_run()'s generic default (KO-146 incident, run 9).
        # Collapsed to one line: sh()'s message carries the failed
        # command's whole output, and the reason lands verbatim in an
        # escalation comment's markdown bullet.
        reason = " ".join(f"{type(e).__name__}: {e}".split())
        print(f"[holo2] run crashed: {reason}")
    finally:
        if merged is PARKED:
            # Parked, alive, lease released: the run's own outcome is still
            # open, so there is no entry to render and no failure to count.
            pass
        elif merged:
            release_run(conn, run_id, True,
                        merge_sha=merged if isinstance(merged, str) else None)
            # `in_flight -> merged`, projected as Done. A run that did
            # not merge leaves the ticket in flight on purpose: the
            # branch is preserved for a human and the board should go
            # on saying the work is open, so there is nothing to push.
            mirror_status(conn, ticket_id, "merged", provider)
            # Close-out, and the first moment the run's own outcome is
            # a row: the window is regenerated here rather than inside
            # `run_task()` so the entry that ends the run is in it.
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
                                  outcome_class=outcome_class)
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

    Below the table, one line on the target's supervisor -- see
    `supervisor_liveness_line()`. `now` is the clock the heartbeat's age is
    taken against, injectable so a test can place a beat in time.
    """
    out = out or sys.stdout
    if conn is None and not target.store_path.exists():
        print(f"[holo2] no store at {target.store_path}", file=out)
        return
    owned = conn is None
    conn = conn if conn is not None else open_store(target)
    try:
        print("\n".join(report_lines(conn, target)), file=out)
        print(supervisor_liveness_line(target, conn, now), file=out)
    finally:
        if owned:
            conn.close()


def requeue(target, identifier, note, out=None):
    """Put the failed ticket `identifier` back in the queue. Returns nothing.

    `--requeue`'s whole body, and off every other mode's write path: it opens
    the store, does `store.requeue()`'s one transaction, prints the requeued
    line and exits. The identifier is the Linear one (`KO-n`), resolved in
    this target's store; an identifier the store has not mirrored, or one it
    holds more than once, is a `SystemExit` naming it, as is every refusal
    `store.requeue()` makes -- and in all of those nothing is written. A
    target with no store has nothing to requeue and says so the same way.
    """
    out = out or sys.stdout
    conn = _operator_store(target)
    try:
        ticket_id = _ticket_by_identifier(target, conn, identifier)
        try:
            run_id = store.requeue(conn, ticket_id, note)
        except (store.RequeueRefused, ValueError) as refused:
            raise SystemExit(f"[holo2] {refused}") from None
        print(f"[holo2] {identifier} requeued after run {run_id}", file=out)
    finally:
        conn.close()


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


def _operator_store(target):
    """The store an operator command writes to, or the exit for a target
    that has none: nothing to requeue or approve, and no file made for the
    sake of saying so."""
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
