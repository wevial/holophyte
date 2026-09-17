from time import monotonic

import store
import store.read
from holophyte import babysitter, pr
from holophyte.board import block_ticket, ledger
from holophyte.config_tables import merge_config, sweep_config
from holophyte.gates import MergeParked, RunFailure, sh
from holophyte.reconcile import _pr_seen
from holophyte.runs import heartbeat_while, set_phase


def _resume_on_pr(target, conn, run_id, provider, task_id, issue_id, task,
                  branch, wt, carried, started, verify_cmd, contracts,
                  budget_min, body, criteria=()):
    """The resumed run of a candidate open as a pull request: the babysitter
    again, from the branch as it stands, with the release's answer
    (`carried.approved`) deciding what a green, quiet PR does.

    What the babysitter may merge without another review is not the branch
    as it stands but the sha an independent judgement covered: the
    operator approves the parked sha; babysit carries the park's
    `approvedSha` -- the reviewer's approval, or None when the park had
    none to record (a fix the reviewer rejected, a store older than the
    column). A branch at any other sha is reviewed again before the merge
    API is called; that is `_babysit()`'s `reviewed`.

    The park's verify was a process ago, so the
    babysitter is told no sha is verified (`verified=None`) and runs the
    merge gate -- the ticket's verify commands, then the drift check --
    on the candidate before the merge API is called."""
    from holophyte.loop import _sync_branch_from_origin

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
    merge_sha = babysitter._babysit(target, conn, run_id, provider, task_id,
                                    issue_id, task, branch, wt, sha, beat_s,
                                    url, f"{task}\n\n{body}" if body else task,
                                    verify_cmd, contracts, budget_min,
                                    criteria, approved=carried.approved,
                                    reviewed=reviewed, verified=None,
                                    fix_note=(None if carried.approved else
                                              store.read.babysit_note(
                                                  conn, carried.run_id)))
    return _landed_pr(conn, run_id, provider, task_id, task, branch, url,
                      merge_sha, started, budget_min, 0)


# The most of `git diff main...HEAD` a written-PR turn is shown, in
# characters; past it the diff is cut and the prompt says so (KO-336).
PR_TEXT_DIFF_CAP = 60_000
# The wall clock a written-PR turn gets, in minutes, unless less of the
# run's box is left: a description, not an implementation.
PR_TEXT_BUDGET_MIN = 5
# Where a repository keeps its pull request template, in the order the
# first present wins (KO-430). GitHub fills a web-UI PR's body from the
# file; the factory opens through the API, so the written turn has to be
# handed the file itself.
PR_TEMPLATE_FILES = (".github/pull_request_template.md",
                     ".github/PULL_REQUEST_TEMPLATE.md",
                     "PULL_REQUEST_TEMPLATE.md")


def _pr_template(wt):
    """The task worktree's pull request template, capped like a
    conventions file with the same note when cut; empty when the
    repository has none."""
    for name in PR_TEMPLATE_FILES:
        path = wt / name
        if path.is_file():
            text = path.read_text(errors="replace").strip()
            if len(text) > babysitter.CONVENTIONS_CAP:
                text = (text[:babysitter.CONVENTIONS_CAP]
                        + f"\n\n[{name} truncated here]")
            return text
    return ""


def _written_pr_text(target, conn, run_id, task_id, task, branch, body,
                     beat_s, wt, started, budget_min, issue_url):
    """One implementer turn writes the PR title and body from the diff.
    Return `(title, body)`, using a Summary stub when the reply is unusable
    or the turn runs out of time, with one printed line saying so.

    The turn is given the diff against `main` (capped at `PR_TEXT_DIFF_CAP`,
    with a note when cut), the ticket, the repository's `AGENTS.md` and
    `CLAUDE.md` when the worktree root has them, the repository's pull
    request template when it has one (`_pr_template()`), with the
    instruction to fill its sections, and the target's `pr_style`
    instructions; it answers with a line `TITLE: ...` and the body after it.
    The body carries `Linear: KO-n` and the issue URL as its last line, and
    no FINDINGS entry: the description is the repository's, the entry is the
    factory's. The budget is `PR_TEXT_BUDGET_MIN` or what is left of the
    run's box, whichever is less, and at least one minute.
    """
    from holophyte.loop import _timed

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
    template = _pr_template(wt)
    if template:
        parts.append("Pull request template, fill its sections:\n\n"
                     + template)
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
    reply, timed_out = _timed(target, conn, run_id, beat_s, wt, minutes,
                              goal)
    parsed = None if timed_out else pr.parse_pr_text(reply)
    if parsed is None or not parsed[1]:
        why = ("the turn ran out of time" if timed_out
               else "the reply has no `TITLE:` line, an empty title, or a"
               f" title over {pr.PR_TITLE_MAX} characters, or an empty body")
        print(f"[holo2] written PR text refused for {task_id}: {why};"
              " opening the pull request with the ticket's title and a stub")
        return pr.pr_title(task_id, task), pr.pr_body_stub(
            {"id": task_id, "body": body}, why, issue_url)
    title, text = parsed
    return title, pr.pr_body_written(text, task_id, issue_url)


def _open_pr(target, conn, run_id, task_id, task, branch, body, beat_s,
             wt, started, budget_min, issue_url=None):
    """`[merge] mode = "pr"`: push the approved candidate and open its pull
    request; return the PR's URL.

    `git push origin BRANCH`, then the PR with its written title and body,
    so a PR never names a branch the remote does not hold. Either
    refusing is `InfraFailure` out of `holophyte.pr`: the route gave out,
    not the ticket, so no strike is spent and the branch and worktree stay
    exactly as after a refused merge. Nothing touches main.

    Between the two, one GraphQL read asks whether the branch is already
    the head of an open pull request (KO-407): a run requeued onto a
    branch its failed predecessor opened as a PR would otherwise be
    refused by the create with everything else done right. A hit is the
    run's `pr_url`, adopted rather than opened again -- the babysit pass
    that follows is the same one a created PR gets, and a park through
    `_park_on_pr()` records it like every other PR park.

    Before the push, `_written_pr_text()` writes the title and body from
    the diff. An unusable reply falls back to the ticket title and a short
    Summary stub explaining the failure, followed by the Linear link.

    Both calls leave the machine and block for as long as the remote takes,
    so they run under `heartbeat_while()` like every other wait: a slow push
    is not a dead loop for the supervisor to sweep before the URL is on the
    run (KO-259 review round 1).
    """
    title, text = _written_pr_text(target, conn, run_id, task_id, task,
                                   branch, body, beat_s, wt, started,
                                   budget_min, issue_url)
    # Still the `merge_gate` phase: the push and the create are the mode's
    # way out of the gate, named on the stream rather than as a phase move.
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"pushing {branch} to {pr.REMOTE} and opening its"
                           " pull request")
    adopted = False
    with heartbeat_while(conn, run_id, beat_s):
        pr.push_branch(target, branch)
        print(f"[holo2] pushed {branch} to {pr.REMOTE}")
        # A branch already open as a pull request is adopted, not opened
        # again: `gh pr create` refuses with one still open, which is how
        # a requeued run used to fail after doing everything right.
        url = pr.open_pull_request(target, branch)
        if url is None:
            url = pr.create_pull_request(target, branch, title, text)
            print(f"[holo2] pull request open: {url}")
        else:
            adopted = True
            print(f"[holo2] {branch} is already open as {url};"
                  " adopting it")
    if conn is not None and run_id is not None:
        with store.transaction(conn):
            store.record_event(
                conn, run_id, "pull_request",
                f"adopted the branch's open pull request: {url}"
                if adopted else f"pull request open: {url}")
            if adopted:
                # The hit is the run's `prUrl` from this moment, not only
                # from a later park: a run that merges through the adopted
                # PR without parking still names it on its row.
                conn.execute("UPDATE runs SET prUrl = ? WHERE id = ?",
                             (url, run_id))
    return url


def _park_human(target, conn, run_id, provider, task_id, branch, sha, pull,
                human, listed, reviewed):
    """Park the run on the threads the pass found `HUMAN`, each quoted in
    the ticket's question, with `listed` as the open threads."""
    quoted = "\n\n".join(babysitter.quoted(t) for _, t, _ in human)
    _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                "a thread needs a human's answer; nothing was posted on"
                f" it:\n{quoted}", listed, reviewed=reviewed)


def _merge_pr(target, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
              pull, reviewed=None, retry_conflicts=False):
    """Merge the pinned candidate, clean up, and return its merge sha.
    Park on refusal unless the babysitter opts into raising 405 conflicts;
    operator approval retains the default park on every refusal."""
    set_phase(conn, run_id, "merging", f"merging {pull.url} through the"
              " pull request API")
    try:
        with heartbeat_while(conn, run_id, beat_s):
            merge_sha = pr.merge_pull_request(target, pull, sha)
    except pr.MergeRefused as refused:
        if (retry_conflicts and "405" in str(refused)
                and "merge conflicts" in str(refused).lower()):
            raise
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
