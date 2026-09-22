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
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep, time
from time import monotonic as retry_clock

import review_runner
import store
import store.read
import ticket_template
from holophyte import failure_reason, pr_status
from holophyte import run as run_state
from holophyte.agents import agent, record_session, review_refs, transport_failure
from holophyte.babysitter import _babysit
from holophyte.board import (
    ledger,
)
from holophyte.claim import (
    _cut_worktree,
    _setup_worktree,
    claimed_run,
    conflict_brief,
    merge_conflicts,
)
from holophyte.config import (
    budget_scale,
)
from holophyte.config_tables import (
    loop_config,
    merge_config,
    sweep_config,
)
from holophyte.dispatch import SWEPT
from holophyte.environment_git import paths, stage_work, unstage_environment
from holophyte.gates import (
    GroupKill,
    InfraFailure,
    RunFailure,
    record_unreviewed_verification,
    run_verify,
    sh,
    with_baseline,
)
from holophyte.gates import (
    MergeParked as MergeParked,
)
from holophyte.merge_gate import (
    _gate_lock,
    _merge_gate,
    _park_for_approval,
    _resume_at_merge_gate,
)
from holophyte.pr_media import implementer_brief as _capture_brief
from holophyte.pullrequest import (
    _open_pr,
    _park_on_pr,
)
from holophyte.redact import known_secrets, redact_prose
from holophyte.redact import safe_print as print
from holophyte.review import (
    _review_reply,
    criteria_brief,
    criteria_findings,
    evidence_brief,
)
from holophyte.runs import (
    RunSwept,
    heartbeat_while,
    record_round,
    review_round_cap,
    set_phase,
)
from store.working import effective_work

# The paths a run works against, plus the config they carry, are a `Target`
# (below): built once by `cli()` from the command line and passed to every
# function that needs one, so the derivation lives in one place and the
# command line is the only thing that chooses a target. Importing this module
# used to read `sys.argv[1]`, which made every `python3 -m unittest discover`
# retarget the factory at a directory called "discover"; now importing it
# chooses no target at all.


def run_task(target, task, conn=None, run_id=None, provider=None):
    """Pass the claimed Run through the stages, stopping if the store ended it.

    Accept a Run as `target`, or the historical target/task call. Claimed tasks
    carry `_run`; direct storeless calls construct the same value here.

    An illegal phase edge fails as infrastructure, preserving the worktree.
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
        run = target if isinstance(target, run_state.Run) else task.get("_run")
        run = run or claimed_run(
            target, task, conn, run_id, provider, clock=monotonic)
        return _run_stages(run, task)
    except store.IllegalTransition as refused:
        raise InfraFailure(str(refused)) from refused
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


def _run_stages(run, task):
    """task: dict from a provider — {id, title, verify, budget_min}.

    Each task works in its own git worktree (the target stays on main, untouched),
    so a dirty/failed task can never block the repo or the next ticket.

    `conn` and `run_id` are the store and the claimed run the loop took the
    lease with. Every stage boundary below records its phase against them
    through `set_phase()`, and every review or adjudication turn records its
    round through `record_round()`, so in-flight state outlives the process:
    the run row, its rounds and its event stream say what the loop was doing
    and what the reviewer found, instead of that living only in this frame and
    in prose. Both are None on a direct call with no store, which runs the same stages
    and records nothing.

    `provider` is the board the ticket came from, and the run needs it for one
    question only: at the merge gate, has the ticket's contract been edited
    since the claim froze it? A None provider — a direct call, a stub with no
    re-read — simply skips that check, and the gate is what it was.

    The body is the sequence of phase functions below -- worktree, setup,
    implement, review rounds (or the terminal adjudication after them), the
    merge gate, the merge -- each a plain function over the same values this
    frame threads, in the order they ran when this was one function (KO-211).
    """
    target, conn, run_id, provider = run.target, run.conn, run.run_id, run.provider
    task_id, issue_id, started = run.task_id, run.issue_id, run.started
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
    branch, wt = run.branch, run.wt
    # The approved candidate: `--approve` ended the ticket's parked run with
    # its resume point at the merge gate, and its worktree still stands.
    # Nothing to implement or review -- the candidate was, and a person said
    # merge -- so the run reuses the worktree and goes straight to the gate.
    carried = _approved_candidate(conn, run_id)
    if carried is not None and wt.exists():
        return _resume_at_merge_gate(
            run, carried, verify_cmd, contracts, body,
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
                               verify_cmd, contracts, cap, criteria)

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
            sha = sh(["git", "rev-parse", branch], wt)
        elif merge.approve == "human":
            # The human half of the gate, when the target asks for one: the
            # candidate is approved and verified, and a person says "merge".
            _park_for_approval(conn, run_id, provider, task_id, branch, sha)
        else:
            return run_state.land(replace(run, sha=sha, rnd=rnd), ok)
    run = replace(run, sha=sha, rnd=rnd, pr_url=url)
    run = _babysit(run, beat_s, ticket, verify_cmd, contracts, criteria,
                   reviewed=sha, verified=sha, just_pushed=True)
    return run_state.land(run, True)


def _approved_candidate(conn, run_id):
    """The ticket's prior run whose approved candidate this run carries, or
    None: a direct call with no store carries nothing."""
    if conn is None:
        return None
    ticket_id = store.read.run_snapshot(conn, run_id).ticketId
    return store.read.approved_candidate(conn, ticket_id, run_id)


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
        pull = pr_status.parse_pr_url(url)
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


def _scale_note(target, budget_min):
    """The ` (45 min at scale 1.5)` a budget line carries when the
    target's `[agents] budget_scale` stretches the ticket's estimate for
    the implementer harness -- nothing when the scale is 1, so the line
    is byte-identical to the one it always was."""
    scale = budget_scale(target)
    if scale == 1:
        return ""
    return f" ({budget_min * scale:g} min at scale {scale:g})"


def _timed(target, conn, run_id, beat_s, wt, budget_min, goal, *,
           role="implement", argv=None):
    """Run one turn under its scaled wall-clock budget.

    Return `(output, timed_out)`; retain output and reap children on timeout.
    The sweep hook kills the same group if the run is swept."""
    # The sweep's hook: a beat that finds the run ended kills the turn's
    # whole process group, the same kill the budget sends, and the block
    # raises `RunSwept` for `run_task()` once the turn has stopped.
    # Preserve the requested role for turn attribution; agent() owns routing.
    session_role = role
    kill = GroupKill()
    try:
        with heartbeat_while(conn, run_id, beat_s, on_swept=kill):
            output = agent(target, role, goal, wt,
                          timeout=budget_min * budget_scale(target) * 60,
                          on_start=kill.arm, conn=conn, run_id=run_id,
                          **({"argv": argv} if argv is not None else {}))
            timed_out = False
    except subprocess.TimeoutExpired as expired:
        print(f"[holo2] task exceeded {budget_min} min budget"
              f"{_scale_note(target, budget_min)}")
        partial = expired.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        partial = partial.strip()
        print(f"[holo2] {'writer' if role == 'write' else 'implementer'}"
              " output before the budget fired:\n"
              + (partial[-2000:] or "(no output before the budget fired)"))
        output, timed_out = partial, True
    record_session(target, conn, run_id, session_role, output)
    return output, timed_out


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
    """Refuse dispatch when effective work plus the scaled turn exceeds the cap.

    The ceiling remains timeBoxMs × budget_scale × run_cap. Preserve candidate
    and findings diagnostics; unmeasured or storeless runs have no known spend."""
    if conn is None or run_id is None:
        return
    run = store.read.run_snapshot(conn, run_id)
    if run is None or not run.timeBoxMs or not budget_min:
        return
    scale = budget_scale(target)
    cap = sweep_config(target).run_cap
    box_ms = run.timeBoxMs * scale
    spent_ms = effective_work(run, int(time() * 1000))
    if spent_ms is None:
        return
    if spent_ms + budget_min * scale * 60000 <= box_ms * cap:
        return
    reason = (f"out of time: {spent_ms / 60000:.1f} min spent of a "
              f"{box_ms / 60000:.0f} min box (cap {cap:g}x); candidate "
              f"preserved at {sha[:12]}; open findings: "
              f"{_open_findings(conn, run_id)}")
    store.record_event(conn, run_id, "run_cap", reason)
    raise RunFailure(reason, "budget")


# How much of the implementer's final output a no-commit turn keeps on the
# run (KO-375): the last characters, where a refusal or a "this contract
# cannot be met" explanation ends up.
OUTPUT_TAIL = 4000


def _record_implementer_output(conn, run_id, out, secrets=()):
    """Record the output tail as a detail event before discarding a worktree.

    The summary is the last non-empty line; the payload is the last OUTPUT_TAIL
    characters, redacted with config/environment secrets and secret names."""
    if conn is None or run_id is None:
        return
    text = redact_prose((out or "").strip(), secrets)
    summary = text.splitlines()[-1] if text else "(implementer printed nothing)"
    store.record_event(conn, run_id, "implementer_output", summary,
                       level="detail", payload=text[-OUTPUT_TAIL:])


def _transport_timed(target, conn, run_id, beat_s, wt, budget_min, goal):
    """Retry transport loss once, sharing the original turn's wall-clock cap."""
    scale = budget_scale(target)
    deadline = retry_clock() + budget_min * scale * 60
    remaining = budget_min
    for attempt in range(2):
        out, timed_out = _timed(target, conn, run_id, beat_s, wt,
                                remaining, goal)
        signature = transport_failure(getattr(out, "exit_code", 0), out)
        if timed_out or signature is None:
            return out, timed_out
        _record_implementer_output(conn, run_id, out,
                                   known_secrets(target.config()))
        reason = f"implementer transport failure ({signature})"
        if attempt:
            raise InfraFailure(f"{reason} after retry; branch preserved")
        note = f"{reason}; retrying once in 30s"
        print(f"[holo2] {note}")
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "transport_retry", note)
        with heartbeat_while(conn, run_id, beat_s):
            sleep(min(30, max(0, deadline - retry_clock())))
        remaining = (deadline - retry_clock()) / (scale * 60)
        if remaining <= 0:
            raise InfraFailure(f"{reason}; retry budget exhausted; branch preserved")


def _implement(target, conn, run_id, task_id, task, branch, wt, fresh, beat_s,
               start_sha, ticket, verify_cmd, budget_min, conflicts=()):
    """Implement the ticket and return its SHA; open with reuse conflicts."""
    commands = (f"\n\nThese verify commands must pass before review and again "
                f"before merge:\n\n{verify_cmd}" if verify_cmd else "")
    # A reclaimed run can already be old; refuse a turn that would exceed
    # its remaining budget.
    _check_run_cap(target, conn, run_id, budget_min, start_sha)
    out, timed_out = _transport_timed(
        target, conn, run_id, beat_s, wt, budget_min,
        conflict_brief(branch, conflicts)
        + f"Implement this task in this repo:\n\n{ticket}{commands}\n\n"
        "The ticket above is the contract, acceptance criteria "
        "included; the task is done only when they hold. Commit your "
        "work with a clear message. Stay strictly on-scope; do not "
        "expand the task. Commit messages carry no tool attribution or co-author "
        "lines for an AI." + _capture_brief(target, ticket))
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
        # one `??` line, reporting files as directories in the event below.
        # The kill can land inside `git add` itself — KO-391's turn died
        # mid-staging — and SIGKILL does no cleanup, so the interrupted
        # operation leaves `index.lock` and the add below would refuse
        # it with "File exists". The turn's whole process group was
        # reaped before `TimeoutExpired` reached here, so a lock in this
        # worktree can only be the dead turn's: remove it before unstaging.
        lock = Path(wt, sh(["git", "rev-parse", "--git-path",
                            "index.lock"], cwd=wt))
        lock.unlink(missing_ok=True)
        unstage_environment(target, wt)
        dirty = sh(["git", "status", "--porcelain", "-uall", *paths(target)],
                   cwd=wt).splitlines()
        if dirty:
            stage_work(target, wt)
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
                             " and worktree were discarded",
                             "budget" if timed_out else "no_commits")
        # A reused worktree holds work some earlier run preserved; this
        # run's implementer adding nothing is no reason to destroy it.
        raise RunFailure(f"implementer made no new commits; preserved work"
                         f" kept on {branch} at {start_sha[:12]}",
                         "budget" if timed_out else "no_commits")
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
                         f"{branch} at {head[:12]}", "budget")
    return sh(["git", "rev-parse", "HEAD"], cwd=wt)


def _verify_brief(verify_cmd, ok, out):
    """Show ticket and baseline checks; omit the brief only if neither ran."""
    count = sum(row["source"] == "baseline"
                for row in getattr(out, "results", []))
    if not verify_cmd and not count:
        return ""
    return (f"The ticket's verification commands and the target's baseline "
            f"({count} commands) were run and "
            f"{'PASSED' if ok else 'FAILED with output below'}:\n{out}\n"
            + ("The suite has been run by the factory at this commit; do not run "
               "the full suite again, run only focused tests needed to check a "
               "specific concern.\n" if ok else ""))


def _changed_lines(wt):
    """Count changed lines against the merge base; binary files count as zero."""
    base = sh(["git", "merge-base", "main", "HEAD"], cwd=wt)
    total = 0
    for line in sh(["git", "diff", "--numstat", base, "HEAD"], cwd=wt).splitlines():
        added, removed, *_ = line.split("\t")
        total += sum(int(n) for n in (added, removed) if n.isdigit())
    return total


def _review_cap(target, conn, run_id, provider, task_id, wt):
    """Measure and record the review cap from candidate size and configuration."""
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
    """Verify, review and fix up to `cap` rounds; return sha, round, approval."""
    for rnd in range(1, cap + 1):
        set_phase(conn, run_id, "verifying", f"round {rnd}: verify before review")
        if rnd == 1:
            # A tree left mid-merge by `reuse_leftover()` is the park
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
            ok, out = run_verify(verify_cmd, wt, contracts, conn=conn, run_id=run_id,
                             target=target)
            ok, out = with_baseline(target, wt, verify_cmd, ok, out,
                                   conn, run_id)
        if ok:
            print(f"[holo2] verify ok before round {rnd}")
        else:
            print(f"[holo2] verify FAILED before round {rnd}:\n{out}")

        set_phase(conn, run_id, "reviewing", f"round {rnd} review")
        round_started = int(time() * 1000)
        with heartbeat_while(conn, run_id, beat_s):
            verdict, decision, first_reply = _review_reply(target,
                f"You are a READ-ONLY code reviewer. Review commit {sha} using "
                f"{review_refs(run_id)[0]} as the frozen base and "
                f"{review_refs(run_id)[1]} as the candidate "
                "in this repo against the ticket below. The ticket is the "
                "contract, acceptance criteria included: a candidate that "
                "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
                f"{ticket}\n\n"
                + _verify_brief(verify_cmd, ok, out)
                + criteria_brief(criteria)
                + evidence_brief(target, wt, task_id,
                                 ticket_template.parse(ticket).evidence_states)
                + "Do not modify anything. End your reply with exactly one "
                "line:\n"
                "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
                "If REQUEST_CHANGES, list only concrete blockers.", wt,
                base_sha, sha, conn, run_id, run_agent=agent, review_round=rnd)
        # Store even the round that ends the loop.
        record_round(target, conn, run_id, rnd, "review", verdict, verify_cmd,
                     ok, out,
                     started_at=round_started, criteria=criteria, root=wt,
                     prior_reply=first_reply)
        if decision == "MALFORMED":
            reason = "reviewer returned no verdict line twice"
            print(f"[holo2] round {rnd}: {reason}")
            raise InfraFailure(f"{reason}; candidate preserved at {sha}",
                               "review_route")

        # Unmet criteria or nonexistent named witnesses block approval.
        unwitnessed = criteria_findings(verdict, criteria, wt)
        if unwitnessed:
            print(f"[holo2] round {rnd}: {len(unwitnessed)} criteria not "
                  "witnessed; treating as REQUEST_CHANGES")
        if ok and not unwitnessed and decision == "APPROVE":
            ledger(conn, run_id, task_id, "round",
                   f"Round {rnd}: APPROVE\nReviewer verdict:\n{verdict}",
                   provider)
            return sha, rnd, True

        # Refuse a fix turn if the run has no budget left.
        _check_run_cap(target, conn, run_id, budget_min, sha)
        set_phase(conn, run_id, "addressing", f"round {rnd}: addressing findings")
        from holophyte.fix_session import fix_turn
        fixes, timed_out = fix_turn(
            target, conn, run_id, beat_s, wt, budget_min, ticket, verdict, sha,
            timed=_timed, check_cap=_check_run_cap)
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: REQUEST_CHANGES -> fix round\n"
               f"Reviewer findings:\n{verdict}\n\n"
               f"Implementer response:\n{fixes}", provider)
        if timed_out or sh(["git", "rev-parse", "HEAD"], cwd=wt) == sha:
            print(f"[holo2] fix round timed out or made no progress; "
                  f"leaving branch {branch} at {sha} for a human.")
            findings = store.read.rounds_of(conn, run_id)[-1].findings if conn else None
            pending = json.loads(findings) if findings else []
            raise RunFailure(failure_reason.fix_round(
                pending, timed_out, f"branch {branch} preserved at {sha[:12]}"))
        sha = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    return sha, rnd, False


def _terminal_adjudication(target, conn, run_id, provider, task_id, task,
                           branch, wt, beat_s, base_sha, sha, ticket,
                           verify_cmd, contracts, cap, criteria=()):
    """3b. Terminal adjudication: all `cap` review rounds and their fixes
    are spent, so one fresh independent run issues a bare verdict on the
    final state. There is no further fix round under any outcome —
    anything but PASS preserves the branch and stops the loop.
    """
    set_phase(conn, run_id, "verifying", "verify before terminal adjudication")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts, conn=conn, run_id=run_id,
                             target=target)
        ok, out = with_baseline(target, wt, verify_cmd, ok, out,
                               conn, run_id)
    if not ok:
        record_unreviewed_verification(conn, run_id, out)
        print(f"[holo2] verify FAILED before adjudication; leaving branch "
              f"{branch} (worktree {wt}) at {sha} for a human:\n{out}")
        ledger(conn, run_id, task_id, "failure",
               f"FAILED verify before terminal adjudication after "
               f"{cap} review rounds (the run's cap); branch {branch} preserved "
               f"at {sha}\n\n{out}", provider)
        raise RunFailure(failure_reason.verify(
            out, verify_cmd, f"before terminal adjudication; "
            f"branch {branch} preserved at {sha[:12]}"))
    print("[holo2] verify ok before adjudication")

    set_phase(conn, run_id, "reviewing", "terminal adjudication")
    round_started = int(time() * 1000)
    with heartbeat_while(conn, run_id, beat_s):
        reply = agent(target, "adjudicate",
            f"You are a READ-ONLY final adjudicator. Judge commit {sha} "
            f"using {review_refs(run_id)[0]} as the frozen base and "
            f"{review_refs(run_id)[1]} as the candidate "
            "in this repo against the ticket below. The ticket is the "
            "contract, acceptance criteria included: a candidate that "
            "leaves a criterion unmet or unwitnessed is not approvable.\n\n"
            f"{ticket}\n\n"
            + _verify_brief(verify_cmd, ok, out)
            + criteria_brief(criteria)
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
            base_sha=base_sha, candidate_sha=sha, conn=conn, run_id=run_id)
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
        raise RunFailure(failure_reason.adjudication(
            reply, criteria, decision,
            f"branch {branch} preserved at {sha[:12]}"))
    print("[holo2] terminal adjudication: PASS")
    ledger(conn, run_id, task_id, "adjudication",
           f"Terminal adjudication after {cap} review "
           f"rounds: PASS\n\nAdjudicator reply:\n{reply}", provider)
