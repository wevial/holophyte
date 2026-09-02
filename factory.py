#!/usr/bin/env python3
"""holo2: a minimal software factory.

Loop:
  0. Open the v2 store (WAL-mode SQLite, a sibling file of the target repo).
  1. Claim the first ready ticket from the board (a `provider.Provider`,
     Linear by default), mirror it into the store and take the project's
     run lease before any branch exists; the lease goes back when the run
     ends, merged or not.
  2. Spawn an implementer agent (goal-based) on a branch.
  3. Spawn a read-only reviewer agent on the committed result.
  4. If findings: implementer fixes, one narrow re-review, and a fix round for
     its findings too. Max 2 review rounds.
  5. If neither round approved: one terminal adjudication run — PASS or FAIL on
     the final state, no new findings, no further fixes.
  6. On approval or a terminal PASS: merge to main, check off the task, repeat.

`--report` runs none of the above: it prints the store's estimate-vs-actual
table for the target and exits, so the timing the runs recorded is a query
rather than a grep over FINDINGS.md. `--sweep` runs none of it either: it
reads the live runs and says which have tripped a mechanical condition -- a
dead heartbeat or a blown time box -- without touching one. `--sweep --act`
adds the acting: each tripped run is failed and its leases released through
the same close-out step 6's failures take, so a crashed run stops holding the
queue. Its branch and worktree are left where they are, for a human.
`--supervise` is that acting sweep on a timer: one long-lived process per
target, held to one by a lockfile, sweeping every minute until it is told to
stop.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from time import monotonic, time

import review_runner
import store
import store.read

# These names live in the `holophyte` package now (phase-2 split, one
# section per slice): imported back for the call sites still in this file, and
# dropped when `factory.py` becomes the thin entry point in the last slice.
from holophyte.agents import agent
from holophyte.board import (
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
    SUPERVISE_INTERVAL_SEC,
    check_agent_commands,
    check_config_keys,
    loop_config,
    setup_commands,
    setup_timeout,
    sweep_config,
)
from holophyte.findings import commit_findings, refresh_findings
from holophyte.gates import (
    InfraFailure,
    RunFailure,
    outcome_class_of,
    run_verify,
    sh,
)
from holophyte.report import report_lines
from holophyte.review import criteria_brief, criteria_findings
from holophyte.runs import (
    MAX_ROUNDS,
    open_store,
    record_round,
    set_phase,
    warn_on_run,
)
from holophyte.supervisor import (
    SWEEP_HINT,
    SupervisorHeld,
    supervise,
    supervisor_liveness_line,
    sweep,
    sweep_lines,
    sweep_report,
)
from holophyte.target import Target
from provider import LinearProvider

DEFAULT_TARGET = Path("/srv/dev/holo2test")
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


def run_task(  # noqa: C901 -- the loop body; slice 4f of the module split breaks it up
    target, task, conn=None, run_id=None, provider=None
):
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
    # a worktree, and a preserved `task/*` branch has to be traceable to its
    # ticket from `git branch` alone. The title portion keeps its own cap; the
    # identifier is added on top of it rather than eating into it.
    ident = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower())[:30].strip("-")
    slug = f"{ident}-{slug}"
    branch = f"task/{slug}"
    wt = target.worktrees / slug
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
        fresh = (not sh(["git", "status", "--porcelain"], cwd=wt)
                 and sh(["git", "rev-parse", "HEAD"], cwd=wt)
                 == sh(["git", "rev-parse", "main"], target.path))
    else:
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
        fresh = True

    # The worktree exists and nothing has been dispatched into it yet, which
    # is the only moment the target's own setup can run: an implementer whose
    # toolchain is missing burns its whole budget discovering that, and a
    # worktree that silently borrows the main checkout's environment tests
    # something other than the branch it is on. A failure here is the run's
    # failure. A branch this run cut fresh is discarded -- no agent ran, so
    # there is no work on it to keep -- while a reused worktree may hold
    # preserved work the setup failure says nothing about, and is left
    # exactly as found. Either way no agent ran, so the failure is the
    # factory's plumbing, not evidence about the ticket: it closes out as
    # `InfraFailure` and does not spend one of the ticket's strikes.
    ok, out = run_worktree_setup(target, wt, conn, run_id)
    if not ok:
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

    # The review base is main, not the HEAD reuse entered on: preserved
    # commits were never approved, so the reviewer must see them inside the
    # diff. Identical on a fresh cut, where HEAD is main.
    base_sha = sh(["git", "rev-parse", "main"], target.path)
    # Where this run started, WIP commit and preserved commits included. The
    # no-commit gate below compares against this rather than main, so carried
    # leftovers cannot stand in for the implementer's own progress.
    start_sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    def timed(target, goal):
        """Run one agent turn with the budget as its wall-clock cap; None on
        timeout.

        The budget is the dispatch's own timeout, not an alarm around it: an
        alarm interrupted the wait but left the implementer and its children
        running, so a run recorded as over budget kept committing into the
        worktree. `agent()` kills the whole group before raising, and what
        the turn printed before the kill is kept in the log.
        """
        try:
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

    # 1. implement — the ticket verbatim: title, then the approved body, then
    # the verify commands the gate will actually run. The same `ticket` text is
    # what the reviewer and the adjudicator below judge against, so all three
    # turns are held to one contract. A ticket with no body
    # (a file-backed task line, a stub provider) degrades to the title alone.
    ticket = f"{task}\n\n{body}" if body else task
    commands = (f"\n\nThese verify commands must pass before review and again "
                f"before merge:\n\n{verify_cmd}" if verify_cmd else "")
    out = timed(target,
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

    sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)

    def verify_brief(ok, out):
        """The verify result as the reviewer sees it — omitted when the ticket
        declares no command, so the brief never implies a gate that never ran."""
        if not verify_cmd:
            return ""
        return (f"A mechanical verification command was run and "
                f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n")

    # 2. review rounds (MAX_ROUNDS). Verify runs before each review and its
    # result goes into the brief; every round that is not a clean approval —
    # round 2 included — gets a fix round, because a round-2 blocker is the
    # cheapest fix in the loop and used to need a human to close it out.
    for rnd in range(1, MAX_ROUNDS + 1):
        set_phase(conn, run_id, "verifying", f"round {rnd}: verify before review")
        ok, out = run_verify(verify_cmd, wt, contracts)
        if ok:
            print(f"[holo2] verify ok before round {rnd}")
        else:
            print(f"[holo2] verify FAILED before round {rnd}:\n{out}")

        set_phase(conn, run_id, "reviewing", f"round {rnd} review")
        round_started = int(time() * 1000)
        verdict = agent(target, "review",
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate as "
            "the candidate "
            "in this repo against the ticket below. The ticket is the "
            "contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + verify_brief(ok, out)
            + criteria_brief(criteria)
            + "Do not modify anything. End your reply with exactly one line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha=base_sha, candidate_sha=sha)
        # Before the approval check, so the round that ends the loop is stored
        # like every other one: a review the store has no row for is a round
        # §6 cannot compare the next one against.
        record_round(target, conn, run_id, rnd, "review", verdict, verify_cmd,
                     ok, out,
                     started_at=round_started, criteria=criteria)

        # A criterion the reviewer left not met or unwitnessed is a blocker
        # whatever the verdict line says (KO-165 was approved with one unmet).
        unwitnessed = criteria_findings(verdict, criteria)
        if unwitnessed:
            print(f"[holo2] round {rnd}: {len(unwitnessed)} criteria not "
                  "witnessed; treating as REQUEST_CHANGES")
        if (ok and not unwitnessed
                and review_runner.terminal_verdict(verdict) == "APPROVE"):
            break

        # 3. implementer addresses findings (same branch, new commit)
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        fixes = timed(target,
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
    else:
        # 3b. Terminal adjudication: both review rounds and their fixes are
        # spent, so one fresh independent run issues a bare verdict on the
        # final state. There is no further fix round under any outcome —
        # anything but PASS preserves the branch and stops the loop.
        set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
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
        reply = agent(target, "adjudicate",
            f"You are a READ-ONLY final adjudicator. Judge commit {sha} using "
            "refs/review/base as the frozen base and refs/review/candidate as "
            "the candidate "
            "in this repo against the ticket below. The ticket is the "
            "contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + verify_brief(ok, out)
            + "This candidate has already had its review rounds and their "
            "fixes; no further fix round exists. Your job is a verdict on the "
            "state as it stands, not a review.\n"
            "Do not modify anything. Do NOT list findings, request changes, or "
            "propose follow-up work — a reply that reads as a findings list is "
            "not a verdict and is treated as FAIL. Give at most one short "
            "paragraph of justification, then exactly one final line:\n"
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

    # 4. pre-merge verify (catches fix-round regressions), then merge. Both
    # happen under `merge_gate`: §4's gate node is the one edge out of a
    # passing review, and this verify is the mechanical half of what the gate
    # asks. Under the `personal` autonomy profile the human half is a no-op,
    # so the run passes through the node rather than around it and a failed
    # pre-merge verify is a run stopped at the gate.
    set_phase(conn, run_id, "merge_gate", "pre-merge verify, then the autonomy gate")
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
        else:
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
    # The merge has landed: the branch holds nothing main does not, so the
    # worktree's stray untracked files are not preserved work — and a cleanup
    # refusal must not re-classify merged work as a failed run.
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], target.path)
        sh(["git", "branch", "-d", branch], target.path)
    except RuntimeError as e:
        print(f"[holo2] post-merge cleanup left debris: {e}")
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
    return True


def self_hosted(target):
    """Whether `target` is the repository this very module was imported from.

    Decided once at startup by `main()`: a loop working on the factory's own
    checkout keeps running the pre-merge code after every merge, so each
    dogfooded fix is invisible to the loop that merged it until someone
    restarts it (gembox 2026-09-02, run 17 cut a worktree without the
    ticket id run 16 had just merged support for).
    """
    return Path(__file__).resolve().parent == target.path.resolve()


def main(target, provider):  # noqa: C901 -- same, slice 4f
    restart_after_merge = self_hosted(target)
    stop_on_failure = loop_config(target).stop_on_failure
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
        # Startup self-sweep, read-only: it records what it saw — a first
        # strike on anything silent — so the *next* invocation or a
        # `--sweep --act` can act on the second sighting. Nothing is failed
        # from here; one sample is not evidence (STALE_STRIKES). One sweep,
        # printed once, per invocation: the refused-claim handler below
        # points back at these lines rather than re-sweeping (which would
        # count one silence twice) or reprinting (which would look like it
        # had).
        seen = sweep(target, conn, int(time() * 1000))
        if seen.trips or seen.watched or seen.restarts:
            print("\n".join(sweep_lines(seen)))
            if seen.trips:
                print(SWEEP_HINT.format(target=target.path))
        # The tickets this pass has refused to claim. A blocked ticket keeps
        # its place in the board's ready set — `blocked_on_operator` projects
        # to Todo, the column a human picks work out of — so it is offered
        # again the moment it is skipped. Remembering the refusal is what
        # turns "not this one" into "the one after it" instead of the same
        # ticket forever.
        skip = set()
        while True:
            task = provider.claim_next(skip=skip)
            if not task:
                # The exit note, in the store before it is on the terminal:
                # a loop that was re-exec'd and found nothing to claim ends
                # here without ever heartbeating, and this is what tells the
                # sweep the restart came back.
                store.record_loop_return(conn, project)
                print("[holo2] Linear has no ready tickets. done.")
                return 1 if failed else None
            # Before the lease, before the mirror: a ticket that has already
            # burned its attempts is refused here rather than claimed and then
            # discovered to be unworkable, so the escalation costs no run row
            # of its own. `escalate()` blocks it if this is the pass that
            # crossed the threshold, and says so again on every later pass —
            # which is what makes a Linear state a human dragged back to Todo
            # unable to buy the ticket another run.
            #
            # Skipped rather than stopped on, which is not the call the rest
            # of this loop makes: a stop here would be permanent. The ticket
            # sorts where it sorts and is offered first on every invocation,
            # so stopping on it would starve every ticket behind it until a
            # human noticed — and a ticket parked *for* a human is the one
            # case where there is nothing for this loop to wait on.
            # The mirror comes first, and the questions are asked of the row
            # it leaves: the live body is what the run would work from, and
            # the row a previous pass left behind can say `ready` about a
            # ticket whose criteria or verify command have since been edited
            # out. `mirror_ticket()` is an upsert with no lease, so a ticket
            # refused below has cost nothing but a refreshed row.
            #
            # First question, asked of the body itself: does it pass the
            # template validator? The store's gates below judge the row —
            # criteria present, a verify command present — and a body with
            # both can still be unfilled template (KO-165: placeholders in
            # the title, the summary and the first criterion, no What
            # line). One printed line names the first problem, the mirror
            # lands in `needs_spec` as an under-specced body would, and the
            # next candidate is tried; no run row is opened for it.
            problem = body_problem(task)
            if problem:
                mirror_task(conn, project, task, specced=False)
                print(f"[holo2] {task['id']} skipped: {problem}")
                skip.add(task["id"])
                continue
            ticket_id = mirror_task(conn, project, task)
            if escalate(conn, ticket_id, provider):
                print(f"[holo2] {task['id']} is blocked by repeated failures;"
                      " skipping it. a human owns it now")
                skip.add(task["id"])
                continue
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
                skip.add(task["id"])
                continue
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
                return
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
                return
            merged = False
            reason = None
            outcome_class = "work"
            try:
                merged = run_task(target, task, conn, run_id, provider)
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
                if merged:
                    release_run(conn, run_id, merged)
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
                sha = sh(["git", "rev-parse", "--short", "HEAD"], target.path)
                # sys.orig_argv is the exact original command line, so
                # interpreter flags (-u above all: without it a tee'd log
                # goes block-buffered and looks hung) survive the restart.
                argv = list(sys.orig_argv) or [sys.executable, *sys.argv]
                # `os.execv` does not search PATH, and `orig_argv[0]` is
                # whatever the operator typed -- usually the bare `python3`.
                # Resolve it the way the shell did; a name PATH cannot find
                # falls back to the interpreter actually running this code.
                program = argv[0]
                if os.sep not in program:
                    program = shutil.which(program) or sys.executable
                # flush=True: execv replaces the process image without
                # running Python's buffered-stdout flush, so under a
                # redirected (block-buffered) stdout the line would be lost.
                print("[holo2] merged a change to the factory itself;"
                      f" re-executing from {sha}: {argv}", flush=True)
                # The note the supervisor watches for, written before the
                # exec because nothing can be written after a failed one:
                # the sweep reports this restart if no claim, heartbeat or
                # exit note follows it within the grace window.
                store.record_loop_restart(conn, project, sha)
                conn.close()
                EXEC(program, argv)
                return  # only a test's EXEC returns
    finally:
        conn.close()


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
        print("\n".join(report_lines(conn)), file=out)
        print(supervisor_liveness_line(target, conn, now), file=out)
    finally:
        if owned:
            conn.close()


def cli(argv=None):
    """Parse the command line and run the mode it names.

    An explicit parser rather than `sys.argv[1]`, which is what the target
    path used to be read from at import: every first argument was a repository
    path, so `--help` named a repository called "--help" and a mistyped flag
    started a real loop somewhere unintended. Both are now argparse errors,
    and the module can be imported without a command line at all.
    """
    parser = argparse.ArgumentParser(
        prog="factory.py",
        description="Holophyte: a minimal Linear-driven software factory.")
    parser.add_argument(
        "target", nargs="?", default=str(DEFAULT_TARGET),
        help="repository the loop works in (default: %(default)s)")
    # The read-only modes, exclusive of each other: each one prints its table
    # and exits, so a command line naming both is a mistake argparse should
    # answer rather than a silent choice between them.
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--report", action="store_true",
        help="print the target store's estimate-vs-actual table and exit; "
             "reads only -- claims no ticket, cuts no worktree, calls nobody")
    modes.add_argument(
        "--sweep", action="store_true",
        help="print the live runs that have tripped a mechanical condition "
             "(dead heartbeat, blown time box, stuck review) and exit; acts "
             "on none of them unless --act says to")
    modes.add_argument(
        "--supervise", action="store_true",
        help="run the acting sweep on an interval ([supervisor] "
             "sweep_interval_sec, default %ds) until SIGINT/SIGTERM, as the "
             "target's one supervisor: a second one for the same target "
             "exits naming the first" % SUPERVISE_INTERVAL_SEC)
    # Not a mode of its own: it says what `--sweep` does with what it finds,
    # so it is refused rather than ignored anywhere else. Silently doing
    # nothing would be the worse answer for the operator who typed
    # `--act` meaning to clean up and got a read-only pass.
    parser.add_argument(
        "--act", action="store_true",
        help="with --sweep: fail each tripped run and release its leases, "
             "leaving its branch and worktree for a human")
    args = parser.parse_args(argv)
    if args.act and not args.sweep:
        parser.error("--act says what --sweep does with the runs it finds; "
                     "it has nothing to act on by itself")
    target = Target.locate(args.target)
    # Read the target's config here, with the command line parsed and nothing
    # claimed yet: a malformed file is a startup error about the repository
    # this invocation names, and `--help` never had to touch a config at all.
    target.config()
    # And the `[supervisor]` table is checked in the same breath, for every
    # mode: the loop's startup self-sweep, `--sweep` and `--supervise` all
    # read it, and a threshold outside its constraint is the same kind of
    # mistake as a file that does not parse -- an error about the config,
    # before anything is claimed, rather than a sweep with numbers nobody
    # chose. Unknown keys in any table the factory reads are refused in the
    # same window: a typo the factory ignored would leave the operator
    # believing a knob is set that is not.
    check_config_keys(target)
    sweep_config(target)
    loop_config(target)
    if args.report:
        return report(target)
    # The board, built once here and handed down: nothing below reaches for
    # Linear by name. Construction touches neither the network nor the
    # module's configuration, so a read-only sweep still calls nobody; the
    # first call that posts to the board is what reads it.
    board = LinearProvider()
    # Same window and the same reasons as `--report`: it reads runs and prints
    # them, so no route has to resolve and nobody is called. `--act` fails
    # runs rather than dispatching them, so it needs no route either.
    if args.sweep:
        return sweep_report(target, act=args.act, provider=board)
    # The acting sweep on a timer. Like `--sweep --act` it dispatches nothing
    # and so resolves no route; unlike it, it takes the target's supervisor
    # lock first, and a target that already has one is an exit, not a loop.
    if args.supervise:
        try:
            return supervise(target, board)
        except SupervisorHeld as held:
            # With the liveness line, so the refusal is actionable: a held
            # lock and a fresh heartbeat is a watcher doing its job; a held
            # lock and a stale one is a watcher to go and look at.
            raise SystemExit(
                f"{held}\n{supervisor_liveness_line(target)}") from None
    # And, on the path that actually dispatches agents, every route the config
    # names resolves before the loop claims a ticket. `--report` skips this: it
    # calls nobody, so a reviewer that is not installed on the machine reading
    # the table is not that reading's problem.
    check_agent_commands(target)
    # Same window, same reason: the `[worktree]` table is read here rather
    # than by the first run that cuts a worktree with it.
    check_worktree_setup(target)
    return main(target, board)


if __name__ == "__main__":
    sys.exit(cli())
