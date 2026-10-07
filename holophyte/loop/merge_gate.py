import contextlib
import subprocess
from dataclasses import replace

import store
import store.read
from holophyte.babysit.babysitter import _babysit
from holophyte.board.projection import block_ticket, ledger, merge_drift, mirror_task
from holophyte.config.config_tables import merge_config, sweep_config, verify_config
from holophyte.environment_git import refuse_environment_history
from holophyte.loop import run as run_state
from holophyte.loop.claim import _resolve_merge_conflict, reuse_leftover
from holophyte.loop.gates import (
    MergeLockHeld,
    MergeParked,
    RunFailure,
    run_verify,
    sh,
    with_baseline,
)
from holophyte.loop.runs import heartbeat_while, set_phase, warn_on_run
from holophyte.loop.stop import end_aborted, stop_if_requested
from holophyte.pr.pullrequest import _prepare_pr, _push_and_open, _resume_on_pr
from holophyte.redact import safe_print as print
from holophyte.review.findings import commit_findings
from holophyte.review.reproduce import tests_only_line
from holophyte.story.story_drift import advance_generation, review_refresh, shared_files


class DriftRequeued(store.RunEnded):
    pass


def _resume_at_merge_gate(run, carried, verify_cmd,
                          contracts, body, criteria=(),
                          issue_url=None):
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, issue_id, task = run.task_id, run.issue_id, run.task
    branch, wt, started, budget_min = run.branch, run.wt, run.started, run.budget_min
    from holophyte.loop.branch_sync import _candidate_drift
    store.set_branch(conn, run_id, branch)
    merge = merge_config(project)
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
    # No origin sync: the approval is of the recorded sha, not of a remote ahead.
    ok, why = reuse_leftover(project, wt, branch, conn=conn, run_id=run_id,
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
    if sha == sh(["git", "rev-parse", "main"], project.path):
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to merge the approved candidate"
               f" for: {task}\n{branch} holds nothing"
               " beyond main; nothing to merge.", provider)
        raise RunFailure(f"approved candidate on {branch} holds nothing"
                         " beyond main; nothing to merge")
    store.record_event(conn, run_id, "approved_candidate",
                       f"resuming run {carried.run_id}'s approved candidate"
                       f" {branch} at {sha[:12]} at the merge gate;"
                       " no implementer or reviewer runs")
    print(f"[holo2] {task_id}: approved candidate {branch} at {sha[:12]}"
          f" from run {carried.run_id}; skipping to the merge gate")
    beat_s = sweep_config(project).heartbeat_stale_ms / 2000
    ticket = f"{task}\n\n{body}" if body else task
    if merge.mode == "pr":
        ok, sha = _merge_gate(project, conn, run_id, provider, task_id,
                              issue_id, branch, wt, beat_s, sha, verify_cmd,
                              contracts, ticket, budget_min, sync_main=False)
        title, text = _prepare_pr(project, conn, run_id, task_id, task, branch,
                                  body, beat_s, wt, started, budget_min,
                                  issue_url,
                                  lead=tests_only_line(conn, carried.run_id))
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
                                  verify_cmd, contracts, ticket, budget_min)
            if carried.paused and merge.approve == "human":
                _park_for_approval(conn, run_id, provider, task_id, branch, sha)
            return run_state.land(replace(run, sha=sha), ok)
    run = replace(run, sha=sha, pr_url=url)
    run = _babysit(run, beat_s, ticket, verify_cmd, contracts, criteria,
                   reviewed=sha, verified=sha)
    return run_state.land(run, True)


@contextlib.contextmanager
def _gate_lock(project, conn, run_id, provider, task_id, branch, sha, beat_s):
    try:
        with project.locks.merge(conn, run_id, beat_s):
            yield
    except MergeLockHeld as e:
        _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                      f"merge lock: {e}", f"MERGE GATE DID NOT RUN: {e}.",
                      park_kind="merge_lock")
        raise


def _park_at_gate(conn, run_id, provider, task_id, branch, sha, question,
                  ledger_text, park_kind="question"):
    if conn is not None and run_id is not None:
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        if not block_ticket(conn, ticket_id, provider, question, park_kind=park_kind):
            print(f"[holo2] {task_id} could not be moved to"
                  " blocked_on_operator; failing the run anyway")
    ledger(conn, run_id, task_id, "failure",
           f"{ledger_text} Branch {branch} preserved at {sha}.", provider)


def _unwind_merge(wt, sha):
    """`merge --abort` refuses a staged resolution; the reset to `sha` does not."""
    subprocess.run(["git", "merge", "--abort"], cwd=wt,
                   capture_output=True, text=True)
    if (subprocess.run(["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                       cwd=wt, capture_output=True).returncode == 0
            or sh(["git", "rev-parse", "HEAD"], cwd=wt) != sha):
        sh(["git", "reset", "--hard", sha], cwd=wt)
        print(f"[holo2] merge --abort could not unwind the failed"
              f" resolution; reset to {sha[:12]}")


def _is_ancestor(cwd, a, b):
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b],
                          cwd=cwd, capture_output=True).returncode == 0


def _merge_ref(wt, ref):
    """`ancestor`, `merged`, or `conflicted` with `wt` left mid-merge."""
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


def _sync_main_into_branch(project, conn, run_id, provider, task_id, branch,
                           wt, sha, beat_s, ticket, budget_min, ref="main"):
    status, detail = _merge_ref(wt, ref)
    if status == "ancestor":
        return sha
    print(f"[holo2] {ref} moved past {branch}; merging {ref} into the"
          " branch before the gate's verify")
    if status == "conflicted":
        failure_kind = "unclassified"
        if detail:
            merged, failure_kind = _resolve_merge_conflict(
                project, conn, run_id, branch, wt, sha, detail, ticket,
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
    return (f"The worktree is mid-merge. Merging main into {branch} -- the"
            f" branch pull request {pull.url} is open on, which GitHub"
            f" reports conflicting -- stopped on conflicts in:"
            f" {', '.join(conflicts)}. Resolve each one keeping both"
            " sides' intent (the branch's work and main's new lines both"
            " stay), then commit the merge with a message naming both"
            " sides. That commit is the whole turn: no other work, no"
            " rebase, no force-push.")


def _merge_gate(project, conn, run_id, provider, task_id, issue_id, branch, wt,
                beat_s, sha, verify_cmd, contracts, ticket, budget_min,
                sync_main=True):
    set_phase(conn, run_id, "merge_gate", "pre-merge verify, then the autonomy gate")
    reviewed, shared = sha, None
    if sync_main:
        shared = shared_files(conn, run_id, wt, sha)
        sha = _sync_main_into_branch(project, conn, run_id, provider, task_id,
                                     branch, wt, sha, beat_s, ticket,
                                     budget_min)
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts,
                             verify_config(project).timeout_sec, conn=conn,
                             run_id=run_id, project=project)
        ok, out = with_baseline(project, wt, verify_cmd, ok, out,
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
    if shared:
        review_refresh(project, conn, run_id, provider, task_id, branch, wt,
                       reviewed, sha, beat_s, ticket, verify_cmd, out, shared)

    # A body edited since the claim is a contract this candidate never answered.
    store_mode = getattr(provider, "store_mode", False) is True
    if store_mode:
        _end_if_canceled(conn, run_id, provider, task_id)
    drift, live = merge_drift(conn, run_id, provider, issue_id)
    if drift and store_mode:
        _requeue_drift(conn, run_id, provider, task_id, live, drift, branch,
                       sha)
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


def _end_if_canceled(conn, run_id, provider, task_id):
    """A raise or any answer but canceled is no evidence; the gate goes on."""
    if conn is None or run_id is None:
        return
    try:
        answer = provider.states([task_id]).get(task_id)
    except Exception as e:  # noqa: BLE001 - no evidence, never the gate
        warn_on_run(conn, run_id, f"could not ask the board whether {task_id}"
                                  f" was canceled ({e}); the gate goes on")
        return
    if answer and answer["state"] == "canceled":
        note = f"{task_id} was canceled on the board"
        print(f"[holo2] {note}; ending the run at the merge gate")
        store.abort(conn, run_id, note, source="factory",
                    trigger="linear_cancelled")
        end_aborted(conn, run_id)


def _requeue_drift(conn, run_id, provider, task_id, live, drift, branch, sha):
    fields = ", ".join(drift)
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    (project_id,) = conn.execute("SELECT projectId FROM tickets WHERE id = ?",
                                 (ticket_id,)).fetchone()
    mirror_task(conn, project_id, live)
    revision = store.read.ticket_revisions(conn, ticket_id)[0].revision
    note = (f"{task_id} changed while the run was working ({fields});"
            f" requeued on revision {revision}, branch {branch} preserved at"
            f" {sha[:12]}")
    with store.transaction(conn):
        store.record_intervention(conn, run_id, "requeue", note,
                                  source="factory")
        store.release(conn, run_id, "abandoned", note, candidate_sha=sha)
        store.walk_ticket(conn, ticket_id, "ready")
    print(f"[holo2] {note}")
    ledger(conn, run_id, task_id, "note",
           "MERGE REQUEUED: the ticket drifted from the contract this run"
           f" was claimed under ({fields}). Branch {branch} preserved at"
           f" {sha}; the next run works revision {revision} from it.",
           provider)
    raise DriftRequeued(run_id, "abandoned", note)


def _park_for_approval(conn, run_id, provider, task_id, branch, sha):
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    if not block_ticket(conn, ticket_id, provider, "merge?"):
        # Parks anyway: an unmirrored ticket must not make the loop merge.
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


def _merge(project, conn, run_id, provider, task_id, task, branch, wt, sha):
    from holophyte.commit_hygiene import strip_attribution

    strip_attribution(project, wt, branch)
    sha = sh(["git", "rev-parse", branch], wt)
    checked = refuse_environment_history(project, branch, action="merge")
    commit_findings(project, f"FINDINGS: {task_id} review records")

    set_phase(conn, run_id, "merging", f"--no-ff merge of {branch} into main")
    mr = subprocess.run(["git", "merge", "--no-ff", checked, "-m",
                         f"Merge {branch}: {task}"], cwd=project.path,
                        capture_output=True, text=True)
    if mr.returncode != 0:
        _resolve_no_ff_conflict(project, conn, run_id, provider, task_id,
                                branch, sha)
    # Merged: a cleanup refusal below must not make this a failed run.
    merge_sha = sh(["git", "rev-parse", "HEAD"], project.path)
    advance_generation(conn, run_id)
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], project.path)
        sh(["git", "branch", "-d", branch], project.path)
    except RuntimeError as e:
        print(f"[holo2] post-merge cleanup left debris: {e}")
    return merge_sha


def _resolve_no_ff_conflict(project, conn, run_id, provider, task_id, branch,
                            sha):
    # The index, not the output: a grep would match a mention of the file.
    conflicted = sorted(
        p for p in subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"], cwd=project.path,
            capture_output=True, text=True).stdout.splitlines() if p.strip())
    if conflicted == ["FINDINGS.md"]:
        subprocess.run(["git", "checkout", "--theirs", "FINDINGS.md"],
                       cwd=project.path, capture_output=True, text=True)
        sh(["git", "add", "FINDINGS.md"], project.path)
        sh(["git", "commit", "--no-edit"], project.path)
        return
    # Abort before failing, so main is left as it was before the attempt.
    subprocess.run(["git", "merge", "--abort"], cwd=project.path,
                   capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=project.path,
                           capture_output=True, text=True).stdout.strip()
    paths = ", ".join(conflicted) or "(no unmerged paths reported)"
    why = (f"merge of {branch} into main conflicted on: {paths};"
           f" branch and worktree preserved")
    if dirty:
        why += (" — main is NOT clean after the abort: "
                + " ".join(dirty.split()))
    print(f"[holo2] {why}")
    ledger(conn, run_id, task_id, "failure",
           f"MERGE ABORTED: conflict on {paths}. Branch "
           f"{branch} preserved at {sha}. Rebase it on main "
           "and re-run, or merge it by hand.", provider)
    raise RunFailure(why)


GATE_CONFLICT_QUESTION = "merge conflict with main on: "
