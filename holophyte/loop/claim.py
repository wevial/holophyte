import os  # noqa: F401
import re
import subprocess
from pathlib import Path
from time import monotonic

import store
import store.read
import store.tickets
from holophyte.board.projection import (
    MAX_FAILED_RUNS,
    body_problems,
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
    note_problems,
    on_pull_request,
    release_lease_label,
    store_status,
)
from holophyte.config.config_tables import sweep_config
from holophyte.config.project import worktree_path
from holophyte.config.worktree_settings import branch_prefix
from holophyte.environment_git import (
    factory_identity,
    paths,
    stage_work,
    unstage_environment,
)
from holophyte.loop.claim_store import claim_from_store, store_mode, superseded
from holophyte.loop.gates import (
    InfraFailure,
    RunFailure,
    outcome_class_of,
    sh,
)
from holophyte.loop.run import Run
from holophyte.loop.runs import heartbeat_while, set_phase
from holophyte.loop.task_worktree import (  # noqa: F401
    retire_worktree,
    run_worktree_setup,
    timeout_report,
    write_capture_ignore,
    write_worktree_environment,
)
from holophyte.redact import safe_print as print
from holophyte.review import freshness
from holophyte.review.freshness import park_stale, skip_labelled_stale, stale_reasons


def reuse_leftover(project, wt, branch, conn=None, run_id=None,
                   provider=None, task_id=None, sync_origin=True):
    """An approved candidate passes `sync_origin` False: it stays at its parked sha."""
    from holophyte.loop.branch_sync import _sync_branch_from_origin
    sh(["git", "worktree", "prune"], project.path)
    r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                       cwd=project.path, capture_output=True, text=True)
    # Exact resolved paths: slugs are truncated titles, and git prints resolved paths.
    registered = {str(Path(line[len("worktree "):]).resolve())
                  for line in r.stdout.splitlines()
                  if line.startswith("worktree ")}
    if str(Path(wt).resolve()) not in registered:
        return False, (f"leftover directory {wt} exists but is not a"
                       " registered worktree; a human moves it aside or"
                       " removes it before this ticket is run again")
    unstage_environment(project, wt)
    dirty = sh(["git", "status", "--porcelain", *paths(project)], cwd=wt)

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=wt, capture_output=True).returncode == 0

    # `checkout -B` below would orphan commits the branch holds beyond HEAD.
    head_ref = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=wt)
    branch_held = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=wt, capture_output=True).returncode == 0
    if head_ref != branch and branch_held and not is_ancestor(branch, "HEAD"):
        return False, (f"worktree {wt} is on {head_ref} while branch"
                       f" {branch} holds commits it does not; a human"
                       " reconciles them before this ticket is run again")
    # No start point: `-B branch main` would die on uncommitted files.
    sh(["git", "checkout", "-B", branch], cwd=wt)
    if dirty:
        stage_work(project, wt)
        sh(["git", *factory_identity(wt), "commit", "-m",
            f"WIP: uncommitted leftovers preserved on reuse of {branch}"],
           cwd=wt)
        print(f"[holo2] preserved uncommitted leftovers as a WIP commit"
              f" on {branch}")
    if (sync_origin
            and "origin" in sh(["git", "remote"], project.path).splitlines()):
        try:
            _sync_branch_from_origin(
                project, conn, run_id, provider, task_id, branch, wt,
                diverged=("branch {branch} diverged from origin: local"
                          " {local}, remote {remote}; reconcile by hand"))
        except RunFailure as e:
            return False, str(e)
    if not dirty and is_ancestor("HEAD", "main"):
        sh(["git", "checkout", "-B", branch, "main"], cwd=wt)
        return True, ""
    if not is_ancestor("main", "HEAD"):
        r = subprocess.run(["git", *factory_identity(wt),
                            "merge", "--no-edit", "main"],
                           cwd=wt, capture_output=True, text=True)
        if r.returncode != 0:
            conflicts = merge_conflicts(wt)
            if not conflicts:
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


def _resolve_merge_conflict(project, conn, run_id, branch, wt, sha, conflicts,
                            ticket, beat_s, budget_min):
    from holophyte.loop.implement import _timed
    from holophyte.loop.merge_gate import _is_ancestor

    paths = ", ".join(conflicts)
    set_phase(conn, run_id, "merge_gate",
              f"implementer resolving the merge conflict on {paths}")
    _, timed_out = _timed(project, conn, run_id, beat_s, wt, budget_min,
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
        return None, "unclassified"
    # The gate's verify reads the tree, so the merged sha must hold all of it.
    if sh(["git", "status", "--porcelain"], cwd=wt):
        return None, "unclassified"
    return head, None


def _refresh_main(project, run_id=None, conn=None):
    if "origin" not in sh(["git", "remote"], project.path).splitlines():
        return

    def is_ancestor(a, b):
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                              cwd=project.path, capture_output=True).returncode == 0

    beat_s = sweep_config(project).heartbeat_stale_ms / 2000
    with project.locks.merge(conn, run_id, beat_s,
                            operation="fetch before the cut", wait_phase="working"):
        fr = subprocess.run(["git", "fetch", "origin"], cwd=project.path,
                            capture_output=True, text=True)
        if fr.returncode != 0:
            raise InfraFailure("git fetch origin failed before the cut:"
                               f" {fr.stderr.strip() or fr.stdout.strip()}")
        if subprocess.run(["git", "rev-parse", "--verify", "-q", "origin/main"],
                          cwd=project.path, capture_output=True).returncode != 0:
            return
        local = sh(["git", "rev-parse", "main"], project.path)
        remote = sh(["git", "rev-parse", "origin/main"], project.path)
        if is_ancestor("origin/main", "main"):
            return
        if is_ancestor("main", "origin/main"):
            head = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], project.path)
            if head == "main":
                sh(["git", "merge", "--ff-only", "origin/main"], project.path)
            else:
                sh(["git", "update-ref", "refs/heads/main", remote, local],
                   project.path)
            print(f"[holo2] main fast-forwarded to origin/main:"
                  f" {local[:12]} -> {remote[:12]}")
            return
    raise InfraFailure(f"main diverged from origin/main: main is at {local},"
                       f" origin/main is at {remote}; neither contains the"
                       " other, so no branch was cut -- a person reconciles"
                       " the checkout with origin before this ticket is run"
                       " again")


def _cut_worktree(project, conn, run_id, provider, task_id, task, branch, wt):
    # Recorded first: the files panel finds a working run's worktree by its branch.
    if conn is not None:
        store.set_branch(conn, run_id, branch)
    set_phase(conn, run_id, "working", f"cutting {branch} and implementing")
    if wt.exists():
        ok, why = reuse_leftover(project, wt, branch, conn=conn,
                                 run_id=run_id, provider=provider,
                                 task_id=task_id)
        if not ok:
            ledger(conn, run_id, task_id, "failure",
                   f"FAILED to reuse leftover worktree for: {task}\n"
                   f"{why}\nNothing was deleted.", provider)
            raise RunFailure(f"cannot reuse leftover worktree: {why}")
        return (not sh(["git", "status", "--porcelain", *paths(project)], cwd=wt)
                and sh(["git", "rev-parse", "HEAD"], cwd=wt)
                == sh(["git", "rev-parse", "main"], project.path))
    if sh(["git", "branch", "--list", branch], project.path):
        why = (f"branch {branch} already exists with no worktree; a"
               " human moves it aside or deletes it before this ticket"
               " is run again")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to cut a fresh worktree for: {task}\n"
               f"{why}\nNothing was deleted.", provider)
        raise RunFailure(f"cannot cut a fresh worktree: {why}")
    _refresh_main(project, run_id, conn)
    sh(["git", "worktree", "add", "--detach", str(wt), "main"], project.path)
    sh(["git", "checkout", "-b", branch], cwd=wt)
    return True


def _setup_worktree(project, conn, run_id, provider, task_id, task, branch, wt,
                    fresh, beat_s):
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_worktree_setup(project, wt, conn, run_id)
    if ok:
        return
    print(out)
    # Ledger first: a failed deletion must not also lose why the run stopped.
    if fresh:
        ledger(conn, run_id, task_id, "failure",
               f"FAILED worktree setup for: {task}\nNo agent ran;"
               f" branch {branch} holds nothing and is"
               f" discarded.\n\n{out}", provider)
        sh(["git", "worktree", "remove", "--force", str(wt)], project.path)
        sh(["git", "branch", "-D", branch], project.path)
        raise InfraFailure("worktree setup failed; no agent ran and the"
                           " empty branch was discarded")
    ledger(conn, run_id, task_id, "failure",
           f"FAILED worktree setup for: {task}\nNo agent ran; "
           f"reused worktree {wt} left in place with its "
           f"work.\n\n{out}", provider)
    raise InfraFailure(f"worktree setup failed; no agent ran; reused"
                       f" worktree and branch {branch} left in place with"
                       " their work")


def _park_unlisted(conn, project_id, listed):
    if listed is None:
        return
    listed = set(listed)
    waiting = []
    for ticket_id, _ in store.read.ready_tickets(conn, project_id):
        with store.transaction(conn):
            # Re-read under the write lock: a sibling's claim or park may have landed.
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


def _claim_next(project, conn, project_id, provider, order, skip, seen):
    from holophyte.admission import held_line
    if store_mode(project):
        return claim_from_store(project, conn, project_id, provider, order,
                                skip, seen)
    while True:
        line = held_line(conn, project_id)
        if line:
            print(line)
            return None, None, None
        task = provider.claim_next(skip=skip, order=order)
        if not task:
            _park_unlisted(conn, project_id,
                           getattr(provider, "last_listing", None))
            return None, None, None
        ticket_id = _admit_ticket(project, conn, project_id, provider, task, seen)
        if ticket_id is None:
            skip.add(task["id"])
            continue
        run_id = _claim_run(project, conn, project_id, provider, task, ticket_id,
                            seen)
        if run_id is HELD:
            skip.add(task["id"])
            continue
        if run_id is not None:
            task = dict(task, _run=claimed_run(project, task, conn, run_id, provider))
            freshness.carry_warning(conn, run_id, task)
        return task, ticket_id, run_id


def skip_line(identifier, strikes, pr_url, question, park_kind=None):
    from holophyte.loop.merge_gate import GATE_CONFLICT_QUESTION

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


def _admit_ticket(project, conn, project_id, provider, task, seen):
    pr = on_pull_request(conn, project_id, task)
    problems = body_problems(task, project.path, on_pull_request=pr)
    if problems:
        refused = mirror_task(conn, project_id, task, specced=False)
        if (getattr(provider, "store_mode", False) is True
                and not superseded(conn, refused, task)):
            note_problems(conn, refused, "validation", task["body"], problems)
        print(f"[holo2] {task['id']} skipped: {problems[0]}")
        return None
    if skip_labelled_stale(conn, project_id, task):
        return None
    stale = [] if pr else stale_reasons(project.path, task.get("body"), conn, provider)
    if stale:
        park_stale(project, conn, project_id, provider, task, stale)
        return None
    ticket_id = mirror_task(conn, project_id, task)
    held = store.read.ticket_by_id(conn, ticket_id).activeRunId
    if held is not None:
        _skip_held(f"ticket {task['id']}: lease already held by run {held}",
                   seen)
        return None
    others = foreign_lease_holders(task.get("labels"), lease_host(project))
    if others:
        print(f"[holo2] {task['id']} is leased by {others[0]} on the board;"
              " skipping it")
        return None
    if escalate(conn, ticket_id, provider):
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
    # A refused ticket is re-pushed: a board behind the store offers it again.
    verdict = store.tickets.pickable(conn, ticket_id)
    if not verdict:
        status = store_status(conn, ticket_id)
        print(f"[holo2] {task['id']} is {status} in the store,"
              f" not claimable ({verdict.reason}); skipping it")
        mirror_push(conn, ticket_id, provider)
        return None
    if not (pr or _critic_admits(project, conn, project_id, provider, task)):
        return None
    return ticket_id


def _critic_admits(project, conn, project_id, provider, task):
    try:
        _refresh_main(project, conn=conn)
    except InfraFailure as e:
        freshness.WARNINGS.pop(task["id"], None)
        print(f"[holo2] {task['id']}: main not refreshed, so the critic is"
              f" not asked; the cut meets the failure: {e}")
        return True
    return freshness.critic_admits(project, conn, project_id, provider, task)


class _Held:
    pass


HELD = _Held()


def _skip_held(refusal, seen):
    print(f"[holo2] {refusal}; skipping it")
    if seen.trips or seen.watched:
        print("[holo2] the sweep above shows the lease holder's"
              " last signs of life")


def _refuse_claim(conn, task, run_id, reason):
    refused = InfraFailure(reason)
    store.release(conn, run_id, "failed", str(refused),
                  outcome_class=outcome_class_of(refused),
                  failure_kind=refused.failure_kind)
    print(f"[holo2] {task['id']}: {refused}; stopping for a human")
    return None


def _lease_on_board(project, conn, provider, task, ticket_id, run_id):
    issue_id, label = task["issue_id"], lease_label(project)
    if lease_host(project) in lease_holders(task.get("labels")):
        print(f"[holo2] {task['id']} carries this writer's lease label {label}"
              " with no live run; removing the stale label and claiming")
        drop_lease_label(conn, ticket_id, provider, issue_id, label)
    try:
        provider.label_issue(issue_id, label)
        have = provider.issue_labels(issue_id)
    except Exception as e:
        # A raise is no proof the add did not land, so the label comes off.
        drop_lease_label(conn, ticket_id, provider, issue_id, label)
        return _refuse_claim(conn, task, run_id, "the board did not take the"
                             f" lease label {label} ({e}); no work started")
    others = foreign_lease_holders(have, lease_host(project))
    if others:
        drop_lease_label(conn, ticket_id, provider, issue_id, label)
        store.release(conn, run_id, "failed",
                      f"the board showed {others[0]}'s lease label at claim;"
                      " no work started", outcome_class="infra", failure_kind="infra")
        print(f"[holo2] {task['id']} is leased by {others[0]} on the board;"
              " skipping it")
        return HELD
    return True


def _claim_run(project, conn, project_id, provider, task, ticket_id, seen,
               expected_revision=None, max_parallel=None):
    with lease_turn(project):
        if freshness.parked_since_admitted(conn, ticket_id, task):
            return HELD
        try:
            run_id = store.claim(conn, project_id, ticket_id, None,
                                 expected_revision, max_parallel)
        except store.ClaimConflict as e:
            _skip_held(str(e), seen)
            return HELD
        if run_id is None:
            return None
        leased = _lease_on_board(project, conn, provider, task, ticket_id,
                                 run_id)
    if leased is not True:
        return leased
    if not mirror_status(conn, ticket_id, "in_flight", provider):
        refused = InfraFailure("ticket was not ready when the run"
                               " was claimed; no work started")
        store.release(conn, run_id, "failed", str(refused),
                      outcome_class=outcome_class_of(refused),
                      failure_kind=refused.failure_kind)
        release_lease_label(project, conn, ticket_id, provider, run_id)
        if not escalate(conn, ticket_id, provider):
            mirror_push(conn, ticket_id, provider)
        print("[holo2] claimed ticket is not in a status work starts"
              " from; stopping for a human")
        return None
    return run_id


def claimed_run(project, task, conn=None, run_id=None, provider=None, *,
                clock=monotonic):
    ident = re.sub(r"[^a-z0-9]+", "-", task["id"].lower()).strip("-")
    slug = re.sub(r"[^a-z0-9]+", "-", task["title"].lower())[:30].strip("-")
    branch = f"{branch_prefix(project)}/{ident}-{slug}"
    row = store.read.run_snapshot(conn, run_id) if conn is not None else None
    return Run(project, conn, run_id, provider, task["id"], mirror_key(task),
               task["title"], branch, worktree_path(project, branch),
               task["budget_min"], clock(), row.startedAt if row else None, clock=clock)
