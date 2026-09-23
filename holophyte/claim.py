"""The claim and the worktree cut (KO-389).

`_claim_next()` walks the board's ready listing to the first ticket this
process may run: `_admit_ticket()` asks the questions a ticket answers
before a run row exists -- the template's `body_problem()`, the store's
lease and `pickable()`, the board's foreign `holo:HOST` labels, the
escalation's `skip_line()` -- and `_claim_run()` takes the store lease and
the board's lease label under one `lease_turn()`, returning `HELD` when
another run or writer got there first. The worktree half is the run's
first phase: `_cut_worktree()` cuts the branch at a `_refresh_main()`ed
main or hands `reuse_leftover()` a failed run's preserved worktree, and
`_setup_worktree()` runs the target's `[worktree] setup` table through
`run_worktree_setup()`. `merge_conflicts()`, `conflict_brief()` and
`_resolve_merge_conflict()` are the mid-merge hand-off the cut leaves for
the implementer.

Moved verbatim out of `holophyte/loop.py`; the run stages and the merge
gate a claimed run is dispatched into stay there. `_timed`, `_is_ancestor`,
`_sync_branch_from_origin` and `GATE_CONFLICT_QUESTION` are imported back
inside their callers, the house back-import pattern (`holophyte/pool.py`,
`holophyte/pullrequest.py`), so a `holophyte.loop` attribute patch lands.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

import store
import store.read
import store.tickets
from holophyte.board import (
    MAX_FAILED_RUNS,
    body_problem,
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
    mirror_key,
    mirror_push,
    mirror_status,
    mirror_task,
    release_lease_label,
    store_status,
)
from holophyte.config import (
    branch_prefix,
    setup_commands,
    setup_timeout,
    worktree_environment,
)
from holophyte.config_tables import sweep_config
from holophyte.environment_git import (
    environment_temporary_directory,
    exclude_environment,
    factory_identity,
    paths,
    stage_work,
    unstage_environment,
)
from holophyte.gates import (
    InfraFailure,
    RunFailure,
    outcome_class_of,
    run_verify,
    sh,
)
from holophyte.merge_lock import live_merge_lock
from holophyte.redact import redact_values
from holophyte.redact import safe_print as print
from holophyte.run import Run
from holophyte.runs import heartbeat_while, set_phase
from holophyte.target import worktree_path


def timeout_report(cmd, expired):
    """Read one `subprocess.TimeoutExpired` as a failure report.

    A hung command is a failed command, and the caller can only act on it
    if it arrives as the same `(ok, report)` a non-zero exit arrives as;
    output printed before the cap fired is kept, trimmed to the 2000
    characters a passing verify keeps, with silence reported as silence.
    """
    out = expired.output or ""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    out = out.strip()[-2000:]
    return (f"[verify] command timed out after {expired.timeout:g}s: {cmd}\n"
            + (out or "(no output before the timeout)"))


def write_worktree_environment(target, wt):
    """Replace .env atomically, never following an existing symlink or hardlink."""
    values = worktree_environment(target)
    if values is None:
        return
    exclude_environment(wt)
    # Git metadata keeps interrupted writes outside subsequent `git add -A`.
    git_dir = environment_temporary_directory(wt)
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=git_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write("".join(f"{name}={value}\n" for name, value in values.items()))
        os.replace(temporary, Path(wt) / ".env")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_worktree_setup(target, wt, conn=None, run_id=None):
    """Run the target's setup commands in the fresh worktree `wt`.

    Returns `(ok, report)` using `run_verify()` and `setup_timeout_sec`.
    Commands run in order, stopping on failure or a timeout's process-tree kill.
    Setup and hook failures discard fresh branches but preserve reused worktrees.
    A target that names no setup runs nothing and records
    no phase, so an absent table leaves the run byte-identical to today's.
    """
    try:
        write_worktree_environment(target, wt)
    except (SystemExit, InfraFailure) as error:
        return False, redact_values(str(error))
    except OSError:
        return False, "[holo2] worktree environment file could not be written"
    commands = setup_commands(target)
    timeout = setup_timeout(target)
    if commands:
        set_phase(conn, run_id, "working",
                  f"worktree setup: {len(commands)} command(s) in {wt}")
    for n, command in enumerate(commands, 1):
        try:
            ok, out = run_verify(command, wt, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            ok, out = False, timeout_report(command, e)
        if not ok:
            return False, (f"[holo2] worktree setup command {n} of "
                           f"{len(commands)} FAILED: {redact_values(command)}\n"
                           f"{redact_values(out)}")
        print(f"[holo2] worktree setup {n}/{len(commands)} ok: {command}")
    if (Path(wt) / ".githooks").is_dir():
        try:
            # Without this extension --worktree writes the shared config.
            sh(["git", "config", "extensions.worktreeConfig", "true"], wt)
            sh(["git", "config", "--worktree", "core.hooksPath", ".githooks"], wt)
        except RuntimeError as error:
            return False, f"[holo2] worktree setup hooks configuration FAILED:\n{error}"
    return True, ""


def reuse_leftover(target, wt, branch, conn=None, run_id=None,
                   provider=None, task_id=None, sync_origin=True):
    """Ready leftover worktree `wt` for a new run on `branch`; (ok, reason).
    Preserved work survives. Refuse unregistered directories; commit
    uncommitted changes as WIP. Reset the branch to main
    only when the leftover verifiably holds nothing — a clean tree and a
    tip main already contains — and preserved commits keep theirs with a
    moved-on main merged in, since review routes and the merge both require
    main to be an ancestor of the candidate. A merge stopped on conflicts
    is left mid-merge for the implementer turn to resolve first
    (`merge_conflicts()` names the paths); a worktree off its branch while
    the branch holds commits is a human's call, refused with the state
    named. Nothing is deleted here.
    The local copy is not the branch's truth (KO-410): a pull-request
    target shares it with origin and the operator, so under `sync_origin`
    — the claim's reclaim of a failed run's leftover — a target with an
    `origin` runs the babysit resume's fetch-and-compare (KO-379) before
    main is merged in and before the emptiness test, whose reset would
    leave a remote ahead unread. A remote ahead fast-forwards the local
    branch and worktree first; equal or behind changes nothing; diverged
    is refused naming both shas. The approved candidate's resume passes
    `sync_origin` False: its worktree is held to the sha the park recorded
    (`_candidate_drift()`), and a fast-forward would move the branch onto
    unreviewed commits. Targets without `origin` skip this step.
    """
    from holophyte.loop import _sync_branch_from_origin
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
    unstage_environment(target, wt)
    dirty = sh(["git", "status", "--porcelain", *paths(target)], cwd=wt)

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=wt, capture_output=True).returncode == 0

    # `checkout -B` below moves `branch` to wherever HEAD is, so a worktree
    # sitting off the branch while the branch holds commits of its own
    # would silently orphan them; whose tip is the work is a human's call.
    head_ref = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    branch_held = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=wt, capture_output=True).returncode == 0
    if head_ref != branch and branch_held and not is_ancestor(branch, "HEAD"):
        return False, (f"worktree {wt} is on {head_ref} while branch"
                       f" {branch} holds commits it does not; a human"
                       " reconciles them before this ticket is run again")
    # `-B` with no start point parks `branch` at the HEAD we are on without
    # touching the tree, so it cannot die on uncommitted files the way
    # `-B branch main` does.
    sh(["git", "checkout", "-B", branch], cwd=wt)
    if dirty:
        stage_work(target, wt)
        # The configured identity when the target has one, the factory's
        # pinned one otherwise so a target with no committer configured
        # cannot raise here; the message says the commit is the factory's.
        sh(["git", *factory_identity(wt), "commit", "-m",
            f"WIP: uncommitted leftovers preserved on reuse of {branch}"],
           cwd=wt)
        print(f"[holo2] preserved uncommitted leftovers as a WIP commit"
              f" on {branch}")
    if (sync_origin
            and "origin" in sh(["git", "remote"], target.path).splitlines()):
        try:
            _sync_branch_from_origin(
                target, conn, run_id, provider, task_id, branch, wt,
                diverged=("branch {branch} diverged from origin: local"
                          " {local}, remote {remote}; reconcile by hand"))
        except RunFailure as e:
            return False, str(e)
    if not dirty and is_ancestor("HEAD", "main"):
        # Verifiably empty: a clean tree, and the branch tip — parked at
        # HEAD by the `-B` above — holding nothing main does not already
        # have, on the remote's side of the fetch too or the fast-forward
        # would have moved it. The one case where resetting loses no work.
        sh(["git", "checkout", "-B", branch, "main"], cwd=wt)
        return True, ""
    if not is_ancestor("main", "HEAD"):
        # Preserved commits under a main that moved on: the review routes
        # and the merge gate both require main to be an ancestor of the
        # candidate, so left diverged the branch would stall the ticket on
        # each rerun. A conflict is left in the tree for the implementer:
        # parking it for a person cost an operator round-trip per add/add
        # overlap in a test file (KO-355), and the first verify fails the
        # run if it is still there.
        r = subprocess.run(["git", *factory_identity(wt),
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
    tree is not mid-merge. `MERGE_HEAD` is the mid-merge marker, and a
    merge whose conflicts were staged but never committed still counts as
    unresolved -- the paths are then read from the merge's two parents."""
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
    """The paragraph opening an implementer brief whose worktree was left
    mid-merge by `reuse_leftover()`; empty when there is nothing to resolve."""
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


def _resolve_merge_conflict(target, conn, run_id, branch, wt, sha, conflicts,
                            ticket, beat_s, budget_min):
    """The conflict-resolution turn `reuse_leftover()` hands the
    implementer at claim time, run from the merge gate's mid-merge
    worktree `wt` (KO-404); the merged sha when the turn leaves the merge
    committed over a clean tree, plus the failure kind when not.

    The conflict is the same wherever it is met, so the hand-off is the
    same one: mid-merge worktree, the paths named, the ticket along for
    context -- the branch already holds its work, so resolving the merge
    and committing it is the whole task, and the turn counts against the
    run's budget as a fix round does. A timeout, a tree still mid-merge,
    a HEAD that does not hold both `main` and the candidate's pre-merge
    `sha`, or uncommitted edits on the merge commit all resolve nothing:
    the gate's verify reads the worktree, so the sha it goes on to must
    be the merge of the candidate it claimed.
    """
    from holophyte.loop import _timed
    from holophyte.merge_gate import _is_ancestor

    paths = ", ".join(conflicts)
    set_phase(conn, run_id, "merge_gate",
              f"implementer resolving the merge conflict on {paths}")
    _, timed_out = _timed(target, conn, run_id, beat_s, wt, budget_min,
                          f"The merge gate merged main into the candidate"
                          f" branch {branch} and the merge stopped on"
                          f" conflicts in: {paths}. The ticket's work is"
                          " already committed on the branch, so resolving"
                          " the merge is the whole task: resolve each"
                          " conflict keeping both sides' intent (the"
                          " branch's work and main's new lines both stay),"
                          " then commit the merge with a message naming"
                          " both sides. Make no other change; a gate that"
                          " still finds the merge unresolved fails the"
                          " run.\n\nThe ticket the branch"
                          f" answers:\n\n{ticket}")
    if timed_out or merge_conflicts(wt):
        return None, "budget" if timed_out else "unclassified"
    head = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if not (_is_ancestor(wt, "main", head) and _is_ancestor(wt, sha, head)):
        # The turn ended the merge itself rather than resolving it -- or
        # reset the candidate's work away: a HEAD main alone reaches would
        # send the gate's verify over main, not the merge.
        return None, "unclassified"
    if sh(["git", "status", "--porcelain"], cwd=wt):
        # The merge commit landed but the turn left uncommitted edits;
        # the verify would read them and the merged sha does not hold
        # them.
        return None, "unclassified"
    return head, None


def _refresh_main(target, run_id=None, conn=None):
    """Fetch `origin` and fast-forward the checkout's `main` when
    `origin/main` is ahead, so every branch is cut from everything already
    on `main` anywhere (KO-378). Three cases after the fetch: `origin/main`
    already an ancestor of `main` (equal, or the local-mode checkout ahead
    on unpushed merges) and nothing changes; `main` an ancestor of
    `origin/main` and it is fast-forwarded -- never reset, origin is not a
    local-mode target's source of truth; neither, and the two diverged:
    the refusal names both shas and the run fails before any cut. A target
    with no `origin` skips the step. A failed fetch is the network's
    failure, not the ticket's, so it is `InfraFailure`; so is a divergence,
    which a person untangles and which says nothing about the ticket. Under
    the merge lock, so a fast-forward and a `--no-ff` merge never
    interleave.
    """
    if "origin" not in sh(["git", "remote"], target.path).splitlines():
        return

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=target.path, capture_output=True).returncode == 0

    beat_s = sweep_config(target).heartbeat_stale_ms / 2000
    with live_merge_lock(target, conn, run_id, beat_s,
                         operation="fetch before the cut", wait_phase="working"):
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
    # cutting the worktree is already this run doing the ticket's work, so
    # a crash in it belongs to `working`. The branch is recorded first:
    # the files panel reads `runs.branch` to find the worktree, and a
    # `working` run without one answered 409 for the phase (KO-304).
    if conn is not None:
        store.set_branch(conn, run_id, branch)
    set_phase(conn, run_id, "working", f"cutting {branch} and implementing")
    if wt.exists():
        # leftover from a previous failed run — reuse it so preserved work
        # survives; the branch check below still gates on commits.
        ok, why = reuse_leftover(target, wt, branch, conn=conn,
                                 run_id=run_id, provider=provider,
                                 task_id=task_id)
        if not ok:
            ledger(conn, run_id, task_id, "failure",
                   f"FAILED to reuse leftover worktree for: {task}\n"
                   f"{why}\nNothing was deleted.", provider)
            raise RunFailure(f"cannot reuse leftover worktree: {why}")
        # Whether the leftover holds anything, decided from content: an
        # empty reuse was reset to main and reads as a fresh cut, so the
        # close-outs must neither claim preservation over nothing nor
        # keep an empty leftover alive forever.
        return (not sh(["git", "status", "--porcelain", *paths(target)], cwd=wt)
                and sh(["git", "rev-parse", "HEAD"], cwd=wt)
                == sh(["git", "rev-parse", "main"], target.path))
    # The mirror leftover: the branch exists but its directory does not
    # (a FAIL close-out preserves both). `checkout -b` would die on it and
    # deleting it could destroy preserved commits — so the run fails
    # cleanly, the same answer as the unregistered directory.
    if sh(["git", "branch", "--list", branch], target.path):
        why = (f"branch {branch} already exists with no worktree; a"
               " human moves it aside or deletes it before this ticket"
               " is run again")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to cut a fresh worktree for: {task}\n"
               f"{why}\nNothing was deleted.", provider)
        raise RunFailure(f"cannot cut a fresh worktree: {why}")
    _refresh_main(target, run_id, conn)
    sh(["git", "worktree", "add", "--detach", str(wt), "main"], target.path)
    sh(["git", "checkout", "-b", branch], cwd=wt)
    return True


def _setup_worktree(target, conn, run_id, provider, task_id, task, branch, wt,
                    fresh, beat_s):
    """The setup phase: the target's `[worktree] setup` table, run in `wt`.

    The worktree exists and nothing has been dispatched into it yet, the
    only moment the target's own setup can run: an implementer whose
    toolchain is missing burns its whole budget discovering that, and a
    worktree borrowing the main checkout's environment tests something
    other than the branch it is on. A failure here is the run's failure,
    and no agent ran, so it is the factory's plumbing, not evidence about
    the ticket: `InfraFailure`, no strike spent. A fresh-cut branch is
    discarded -- nothing on it to keep -- while a reused worktree may hold
    preserved work and is left exactly as found.
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


def _retirement_remote_tip(target, branch):
    """Read and fetch the current remote tip, distinguishing absence from failure."""
    remote = subprocess.run(
        ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        cwd=target.path, capture_output=True, text=True, timeout=60)
    if remote.returncode == 2:
        return None
    if remote.returncode:
        raise RuntimeError("remote verification failed (git ls-remote): "
                           + remote.stderr.strip())
    tip = remote.stdout.split()[0]
    fetched = subprocess.run(
        ["git", "fetch", "--no-tags", "--no-write-fetch-head", "origin", tip],
        cwd=target.path, capture_output=True, text=True, timeout=60)
    if fetched.returncode:
        raise RuntimeError("remote verification failed (git fetch): "
                           + fetched.stderr.strip())
    return tip


def retire_worktree(target, branch):
    """Remove a clean, backed-up task checkout; return a refusal or None.

    Keep the branch. A remote read must succeed before its tip is trusted;
    stale remote-tracking refs are not evidence that work is still backed up.
    """
    from holophyte.target import worktree_path

    wt = worktree_path(target, branch)
    if wt.resolve() == target.worktrees.resolve() or wt.is_symlink():
        return "worktree is not a task checkout"
    if not wt.resolve().is_relative_to(target.worktrees.resolve()):
        return "worktree is outside the target's worktrees directory"
    try:
        if not wt.exists():
            sh(["git", "worktree", "prune"], target.path)
            return None
        if sh(["git", "status", "--porcelain", "--untracked-files=all"], wt).strip():
            return "uncommitted work"
        if sh(["git", "symbolic-ref", "HEAD"], wt).strip() != f"refs/heads/{branch}":
            return "worktree is not on the recorded task branch"
        head = sh(["git", "rev-parse", "HEAD"], wt).strip()
        def reachable(ref):
            return subprocess.run(
                ["git", "merge-base", "--is-ancestor", head, ref],
                cwd=target.path, capture_output=True).returncode == 0
        backed_up = reachable("refs/heads/main")
        if not backed_up:
            tip = _retirement_remote_tip(target, branch)
            backed_up = tip is not None and reachable(tip)
        if not backed_up:
            return "commits exist nowhere else (not confirmed on remote branch or main)"
        sh(["git", "worktree", "remove", str(wt)], target.path)
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        return f"worktree retirement refused: {error}"
    return None


def _park_unlisted(conn, project, listed):
    """Walk each `ready` mirror row the board's ready listing no longer
    names to `blocked_on_deps`; one printed line names them (KO-425).

    The mirror is a cache of the board's ready column and nothing used to
    invalidate it on this edge: a ticket the operator pulled to Backlog, or
    one a new relation blocked, kept a `ready` row `ready_tickets()` counted
    as owed a loop on every sweep. `listed` is the identifiers the claim's
    ready listing held (`provider.claim_next()`'s `last_listing`, or the
    scheduler's `_mirror_queue()` listing), so no second board ask is spent;
    a caller that saw no listing passes None and parks nothing.

    `blocked_on_deps`, not `blocked_on_operator`: the wait is on the board,
    so the way back is the board's -- a ticket the listing names again is
    walked back to `ready` when the pass mirrors it, no operator step.
    Rows the listing still names are left alone, whatever kept them off
    this pass's claim. Each row is re-read under its `transaction()` so a
    sibling's claim or park landing in between is honored, and the `note`
    lands on the ticket's last run when it has one, all in one transaction.
    """
    if listed is None:
        return
    listed = set(listed)
    waiting = []
    for ticket_id, _ in store.read.ready_tickets(conn, project):
        with store.transaction(conn):
            # The listing and the `ready_tickets()` read are snapshots;
            # the row is re-read and re-judged under the write lock, so a
            # sibling's claim or park landing in between is honored.
            ticket = store.read.ticket_by_id(conn, ticket_id)
            if (ticket.status != "ready" or ticket.activeRunId is not None
                    or ticket.linearIdentifier in listed):
                continue
            store.walk_ticket(conn, ticket_id, "blocked_on_deps")
            if ticket.lastRunId is not None:
                store.record_ledger(
                    conn, ticket.lastRunId, "note",
                    f"{ticket.linearIdentifier} left the board's ready"
                    " column without being claimed; the mirror waits on"
                    " the board")
            waiting.append(ticket.linearIdentifier)
    if waiting:
        print(f"[holo2] {len(waiting)} mirror rows left the board's ready"
              f" column; waiting on the board: {', '.join(waiting)}")


def _claim_next(target, conn, project, provider, order, skip, seen):
    """Walk the board's queue to the first ticket this process may run and
    lease it. Returns `(task, ticket_id, run_id)`: `task` None when the
    queue is exhausted, `run_id` None when the claim said stop for a human
    (`_claim_run()`). Every ticket refused on the way -- unadmitted, or
    leased by another run mid-claim -- is added to `skip`, so the caller's
    next ask is the one after it. The ask that finds nothing reconciles
    the mirror first (`_park_unlisted()`, KO-425)."""
    from holophyte.admission import held_line
    while True:
        line = held_line(conn, project)
        if line:
            print(line)
            return None, None, None
        task = provider.claim_next(skip=skip, order=order)
        if not task:
            _park_unlisted(conn, project,
                           getattr(provider, "last_listing", None))
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
        if run_id is not None:
            # Carry the value through the existing provider-task dispatch seam;
            # do not mutate the provider's task or rebuild the run at each phase.
            task = dict(task, _run=claimed_run(target, task, conn, run_id, provider))
        return task, ticket_id, run_id


def skip_line(identifier, strikes, pr_url, question, park_kind=None):
    """The admit step's one line for a ticket the store holds parked.

    Pure, so the wording is tested without a store. A pull request wins:
    the run behind it is parked alive, so its URL and the `--approve` that
    merges it are the whole story whatever failed before it -- unless the
    park kind says the PR was closed unmerged, when
    there is nothing an `--approve` would merge and the question is the
    line. A merge-gate conflict (`GATE_CONFLICT_QUESTION`) names its way
    back, `--requeue` once the branch is resolved (KO-365). Then a module's
    question -- `merge?`, say -- first line only, before the strike count:
    the parking run may itself have reached `MAX_FAILED_RUNS`, and the
    conflict is what the operator resolves, not the count. The strike form
    is the escalation's own park, which `is_strike_question()` recognises,
    or a questionless park once the count tripped; a park with neither is
    still a human's, and says so.
    """
    from holophyte.merge_gate import GATE_CONFLICT_QUESTION

    closed = park_kind == "pull_request_closed"
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

    Refused rather than claimed-then-discovered, so the escalation costs
    no run row of its own; `escalate()` blocks a ticket whose attempts
    burned out and says so again on every later pass — which is what
    makes a drag back to Todo unable to buy the ticket another run.

    Skipped rather than stopped on: a stop would starve every ticket
    behind it until a human noticed, and a ticket parked *for* a human
    is the one case with nothing to wait on. The mirror comes first and
    the questions are asked of the row it leaves -- the live body is what
    the run would work from, and a stale row can say `ready` about a
    ticket since edited under-spec. `mirror_ticket()` is an upsert with
    no lease, so a refusal below has cost nothing but a refreshed row.
    """
    # First question, asked of the body itself: does it pass the
    # template validator? A body carrying both lists can still be
    # unfilled template (KO-165: placeholders in the title, the
    # summary and the first criterion, no What line). The mirror lands
    # in `needs_spec` as an under-specced body would; no run row is
    # opened. The target's path goes along so a body naming a path
    # this repository gitignores is refused here too (KO-222) -- unless
    # the last run is on a pull request, whose candidate holds the paths
    # main lacks (KO-598, KO-655).
    problem = body_problem(task, target.path,
                           on_pull_request=_on_pull_request(conn, project, task))
    if problem:
        mirror_task(conn, project, task, specced=False)
        print(f"[holo2] {task['id']} skipped: {problem}")
        return None
    # The mirror also walks a `blocked_on_deps` row the board lists again
    # back to `ready` (KO-425), so the gates below judge it like any
    # other; a `blocked_on_operator` park is a human's and falls through
    # to `escalate()`'s skip.
    ticket_id = mirror_task(conn, project, task)
    # The lease is per ticket (KO-341): a ticket another live run holds
    # is that run's, and the answer is the next candidate, not a stop.
    # Asked before `pickable()` so the refusal reads as the lease it is
    # -- the same sentence `store.claim()` uses when the race is lost a
    # moment later -- and points at the startup sweep, where the holder's
    # last signs of life are.
    held = store.read.ticket_by_id(conn, ticket_id).activeRunId
    if held is not None:
        _skip_held(f"ticket {task['id']}: lease already held by run {held}",
                   seen)
        return None
    # The board's lease (KO-351): another writer's store is not readable
    # from here, but its `holo:HOST` label is, and a ticket carrying one
    # is that writer's for as long as the label stays. This writer's own
    # label is not asked: the store just said no live run holds the
    # ticket, so one is stale, and `_lease_on_board()` takes it off once
    # the store lease is held.
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
        pr_url, park_kind = None, None
        if ticket.lastRunId is not None:
            row = conn.execute("SELECT prUrl, parkKind FROM runs WHERE id = ?",
                               (ticket.lastRunId,)).fetchone()
            pr_url, park_kind = row if row else (None, None)
        print("[holo2] " + skip_line(task["id"],
                                     len(failure_history(conn, ticket_id)),
                                     pr_url, ticket.blockedQuestion, park_kind))
        return None
    # Same place, the store's own question: §2's `pickable()`. The
    # board and the store can disagree about whether a ticket is
    # workable — a failed run leaves its mirror `in_flight` on
    # purpose, and a body edit or a hand-dragged column offers the
    # ticket again as ready — and claiming on the board's word alone
    # produced a run row that existed only to be refused by the
    # `in_flight` transition below (holophyte-bugs.md #4). Asked
    # before the claim so the refusal costs no run row: the
    # `ready -> in_flight` move below does not re-read the lists,
    # and this is the only gate that does.
    #
    # A skipped ticket is still re-projected: the claim's refusal path
    # pushed the store's status back at the board as one more attempt
    # at unsticking it — a `merged` ticket whose Done push never
    # landed is offered again *because* the board is behind, and
    # skipping it silently would leave it Ready on the board for good.
    # Best-effort, like every push: an unmapped status pushes nothing.
    verdict = store.tickets.pickable(conn, ticket_id)
    if not verdict:
        status = store_status(conn, ticket_id)
        print(f"[holo2] {task['id']} is {status} in the store,"
              f" not claimable ({verdict.reason}); skipping it")
        mirror_push(conn, ticket_id, provider)
        return None
    return ticket_id


def _on_pull_request(conn, project, task):
    """Whether the mirrored ticket's last run holds a pull request URL.
    Asked before the mirror, so a ticket never mirrored is not on one."""
    row = conn.execute(
        "SELECT r.prUrl FROM tickets t JOIN runs r ON r.id = t.lastRunId"
        " WHERE t.linearIssueId = ? AND t.projectId = ?",
        (mirror_key(task), project)).fetchone()
    return bool(row and row[0])


class _Held:
    """`_claim_run()`'s answer for a ticket another live run holds: its own
    object -- the loop skips to the next candidate rather than stopping."""


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
                  outcome_class=outcome_class_of(refused),
                  failure_kind=refused.failure_kind)
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
    store has no live run behind it: a run that ended with the board down
    -- and comes off now, under the lease that proves no sibling loop on
    this store holds the ticket, before the fresh one is written; a board
    that will not release it leaves a `warning` row, and the add below
    re-asserts the same name. Then the add and the read-back, four ways:

    - The add raises: a raise is not proof that nothing landed -- Linear
      can apply the mutation and then time out on the response -- so this
      writer's label is taken off best-effort, once, and then the store
      lease goes back and the loop stops for a human. A label left behind
      by a refused claim would otherwise be a lease every other writer
      honours until a human notices it.
    - The read-back raises: the add may have landed, so the same
      best-effort removal, once, and the same release and stop.
    - The read-back shows another writer's `holo:` label, taken between
      the listing and this write: that writer holds the ticket, whichever
      add landed first. This writer's own label comes off, the store
      lease goes back without a strike, the skip line names the holder,
      and the loop takes the next ticket.
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
                      " no work started", outcome_class="infra", failure_kind="infra")
        print(f"[holo2] {task['id']} is leased by {others[0]} on the board;"
              " skipping it")
        return HELD
    return True


def _claim_run(target, conn, project, provider, task, ticket_id, seen):
    """The lease, the board's lease label and the `ready -> in_flight`
    move. Returns the claimed run id, `HELD` when another run or another
    writer took the ticket first, or None when the loop must stop rather
    than start a run."""
    # The store and board halves of the lease under one `lease_turn()`:
    # a close-out reads the store and strips its board label under the
    # same turn, so no claim can land between the look and the removal
    # and have its fresh label stripped.
    with lease_turn(target):
        try:
            run_id = store.claim(conn, project, ticket_id)
        except store.ClaimConflict as e:
            # Before any branch or worktree exists: another loop on this
            # target won the ticket, so this one moves on to the next.
            _skip_held(str(e), seen)
            return HELD
        # The board half of the lease, right after the store half and
        # before the run is anything another writer could collide with.
        if run_id is None:
            return None
        leased = _lease_on_board(target, conn, provider, task, ticket_id,
                                 run_id)
    if leased is not True:
        return leased
    # §3's `ready -> in_flight`, and the first thing the board is told
    # about this run: the projection replaces the state call the
    # provider used to make on its own.
    if not mirror_status(conn, ticket_id, "in_flight", provider):
        # The store refused the move, so this ticket is not `ready`
        # and no work may start on it. The ordinary cause is a board
        # behind the store — a `merged` ticket whose Done push did not
        # land is still non-terminal in Linear and is offered again —
        # and §1 is that the store, not the column, decides. Running
        # anyway would re-implement merged work once per pass.
        #
        # So: give the lease straight back, re-project the status as
        # one more best-effort attempt at unsticking the board, and
        # stop. Stopping is the same call as a refused claim — store
        # and board disagree about what is workable — and keeps a
        # stale ticket the re-push cannot move (an unmapped status, a
        # Linear that is down) from being claimed round and round.
        refused = InfraFailure("ticket was not ready when the run"
                               " was claimed; no work started")
        store.release(conn, run_id, "failed", str(refused),
                      outcome_class=outcome_class_of(refused),
                      failure_kind=refused.failure_kind)
        release_lease_label(target, conn, ticket_id, provider, run_id)
        # This refusal is a failed run, but an `infra` one: no work
        # started, so it does not count towards parking the ticket.
        # The threshold is still checked for the `work` failures
        # already on it; a ticket the escalation just parked was
        # pushed by that move, and the re-push is for one that stayed.
        if not escalate(conn, ticket_id, provider):
            mirror_push(conn, ticket_id, provider)
        print("[holo2] claimed ticket is not in a status work starts"
              " from; stopping for a human")
        return None
    return run_id


def claimed_run(target, task, conn=None, run_id=None, provider=None, *,
                clock=monotonic):
    """Name a run once, including direct callers without a store claim."""
    ident = re.sub(r"[^a-z0-9]+", "-", task["id"].lower()).strip("-")
    slug = re.sub(r"[^a-z0-9]+", "-", task["title"].lower())[:30].strip("-")
    branch = f"{branch_prefix(target)}/{ident}-{slug}"
    row = store.read.run_snapshot(conn, run_id) if conn is not None else None
    return Run(target, conn, run_id, provider, task["id"], mirror_key(task),
               task["title"], branch, worktree_path(target, branch),
               task["budget_min"], clock(), row.startedAt if row else None, clock=clock)
