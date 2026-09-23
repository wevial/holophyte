import re
from dataclasses import replace
from time import monotonic

import store
import store.read
import ticket_template
from holophyte import babysitter, merge_queue, pr, pr_activity, pr_media, pr_status
from holophyte import run as run_state
from holophyte.board import block_ticket, ledger
from holophyte.config_tables import merge_config, sweep_config
from holophyte.gates import MergeParked, RunFailure, sh
from holophyte.reconcile import _pr_seen
from holophyte.redact import safe_print as print
from holophyte.runs import heartbeat_while, set_phase
from holophyte.stop import resume_babysit_fix, stop_if_requested


def _resume_on_pr(run, carried, verify_cmd, contracts, body, criteria=()):
    """The resumed run of a candidate open as a pull request: the babysitter
    again, from the branch as it stands, with the release's answer
    (`carried.approved`) deciding what a green, quiet PR does.

    What the babysitter may merge without another review is not the branch
    as it stands but the sha an independent judgement covered: the
    operator approves the parked sha; babysit reads the ticket's
    latest independent verdict and its run's `approvedSha`. GitHub rounds
    never approve a candidate; a rejection or missing coverage requires
    another review even if the carried approval metadata says otherwise.
    A branch at any other sha is reviewed again before the merge
    API is called; that is `_babysit()`'s `reviewed`.

    The park's verify was a process ago, so the
    babysitter is told no sha is verified (`verified=None`) and runs the
    merge gate -- the ticket's verify commands, then the drift check --
    on the candidate before the merge API is called."""
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, task, branch, wt = run.task_id, run.task, run.branch, run.wt
    from holophyte.loop import _sync_branch_from_origin

    url = carried.pr_url
    reviewed = carried.sha if carried.approved else None
    if not carried.approved:
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        verdict = store.read.last_independent_verdict(conn, ticket_id)
        reviewed = verdict[1] if verdict and verdict[0] == "pass" else None
    if sh(["git", "status", "--porcelain"], cwd=wt):
        ledger(conn, run_id, task_id, "failure",
               f"FAILED to babysit {url} for: {task}\nthe worktree holds"
               " uncommitted changes; nothing was committed or deleted, and"
               " a human reconciles it before this ticket is run again.",
               provider)
        raise RunFailure(f"worktree of {branch} holds uncommitted changes;"
                         f" not babysitting {url}")
    sha = _sync_branch_from_origin(project, conn, run_id, provider, task_id,
                                   branch, wt, url, reviewed)
    with store.transaction(conn):
        store.set_pull_request(conn, run_id, url, carried.sha)
        store.record_event(conn, run_id, "pull_request",
                           f"resuming run {carried.run_id}'s candidate {branch}"
                           f" at {sha[:12]} on {url}"
                           + (" after an approval" if carried.approved
                              else " for another babysit pass"))
    print(f"[holo2] {task_id}: candidate {branch} is open as {url};"
          " babysitting it")
    beat_s = sweep_config(project).heartbeat_stale_ms / 2000
    set_phase(conn, run_id, "merge_gate", f"babysitting {url}")
    sha, pushed = resume_babysit_fix(
        project, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
        pr_status.parse_pr_url(url), f"{task}\n\n{body}" if body else task,
        verify_cmd, contracts, run.budget_min, carried)
    run = replace(run, sha=sha, pr_url=url)
    run = babysitter._babysit(
        run, beat_s, f"{task}\n\n{body}" if body else task,
        verify_cmd, contracts, criteria, approved=carried.approved,
        reviewed=reviewed, verified=None, just_pushed=pushed,
        fix_note=(None if carried.approved else
                  store.read.babysit_note(conn, carried.run_id)))
    return run_state.land(run, True)


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


def _written_pr_text(project, conn, run_id, task_id, task, branch, body,
                     beat_s, wt, started, budget_min, issue_url, *, refresh=None):
    """One writer turn explains the candidate in a PR title and body.
    Return `(title, body)`, using a Summary stub when the reply is unusable
    or the turn runs out of time, with one printed line saying so. A refresh
    returns None on refusal so its caller keeps the existing body.

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
        " Markdown. Explain what the change does for a user or caller and why"
        " first. Note decisions, risks and anything surprising. Do not narrate"
        " the diff. Do not list files, styles, class names, renames or tests."
        " Use this repository's own style; do not paste the ticket, and do not add"
        " a link to the ticket -- the loop appends one. Do not edit, commit"
        " or run anything: answer with the text only.",
    ]
    if merge_config(project).ui_paths:
        parts.append("The loop appends captured Evidence after this turn. Do not"
                     " describe screenshots you have not seen or add an"
                     " Evidence section.")
    template = _pr_template(wt)
    if template:
        parts.append("Pull request template, fill its sections:\n\n"
                     + template)
    style = merge_config(project).pr_style.strip()
    if style:
        parts.append(f"Style instructions from the project's configuration:"
                     f"\n{style}")
    for name, text in babysitter.conventions(wt):
        parts.append(f"The repository's {name}:\n\n{text}")
    parts.append(f"The ticket:\n\n{body or task}")
    parts.append(f"The diff against main (`git diff main...HEAD`):\n\n"
                 f"```diff\n{diff}\n```")
    if refresh is not None:
        current, answered = refresh
        parts.extend([
            "the description as it stands:\n\n" + current,
            "what this fix answered:\n\n" + answered,
            "Rewrite the description to explain the current behaviour and reasons."
            " The title"
            " will be ignored. Do not include Linear, Evidence, or appended bot"
            " blocks. Follow the same prose rules above.",
        ])
        if merge_config(project).pr_changes_log:
            parts.append("Under `## Changes since first review`, give one bullet for"
                         " this fix: what changed in behaviour, one line only."
                         " Omit earlier rounds; the loop preserves them.")
        else:
            parts.append("Do not include a Changes since first review section.")
    goal = "\n\n".join(parts)
    left = budget_min - (monotonic() - started) / 60
    minutes = max(1, min(PR_TEXT_BUDGET_MIN, int(left)))
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"writing the pull request text for {branch}"
                           " from the diff")
    reply, timed_out = _timed(project, conn, run_id, beat_s, wt, minutes,
                              goal, role="write")
    stop_if_requested(conn, run_id, "merge_gate")
    parsed = None if timed_out else pr.parse_pr_text(reply)
    if parsed is None or not parsed[1]:
        why = ("the turn ran out of time" if timed_out
               else "the reply has no `TITLE:` line, an empty title, or a"
               f" title over {pr.PR_TITLE_MAX} characters, or an empty body")
        if refresh is not None:
            print(f"[holo2] written PR text refused for {task_id}: {why};"
                  " leaving the pull request body unchanged")
            return None
        print(f"[holo2] written PR text refused for {task_id}: {why};"
              " opening the pull request with the ticket's title and a stub")
        return pr.pr_title(task_id, task), pr.pr_body_stub(
            {"id": task_id, "body": body}, why, issue_url)
    title, text = parsed
    return title, (text if refresh is not None else
                   pr.pr_body_written(text, task_id, issue_url))

CHANGES_HEADING = "## Changes since first review"


def _without_changes(text):
    """Separate the maintained history from the description's other sections."""
    match = re.search(r"^## Changes since first review\s*\n(.*?)(?=^## |\Z)",
                      text, re.MULTILINE | re.DOTALL)
    if match is None:
        return text, []
    lines = [line for line in match[1].splitlines() if line.startswith("- ")]
    return text[:match.start()] + text[match.end():], lines


def refresh_pr_text(project, conn, run_id, task_id, task, branch, ticket,
                    beat_s, wt, budget_min, pull, answered, *, sha=None):
    """One bounded writing turn after approval; refusal never overwrites prose.
    A fix round whose change touches `[merge] ui_paths` since the sha the
    Evidence names also replaces the Evidence (`pr_media.refresh()`)."""
    if sha and pr_activity.latest(conn, run_id, "pr_text_sha") == sha:
        return
    endpoint = f"repos/{pull.repo}/pulls/{pull.number}"
    with heartbeat_while(conn, run_id, beat_s):
        current = pr.rest(project, pull, "GET", endpoint)["body"] or ""
    own, _, evidence, _ = pr.split_pr_body(current)
    with heartbeat_while(conn, run_id, beat_s):
        section = pr_media.refresh(
            project, wt, task_id, evidence,
            evidence_states=ticket_template.parse(ticket).evidence_states,
            record_note=lambda text: ledger(conn, run_id, task_id, "note", text, None))
    text = _refreshed_prose(project, conn, run_id, task_id, task, branch, ticket,
                            beat_s, wt, budget_min, own, answered)
    if text is None and section is None:
        return
    with heartbeat_while(conn, run_id, beat_s):
        body = pr.rest(project, pull, "GET", endpoint)["body"] or ""
        if text is not None:
            body = pr.replace_pr_text(body, text)
        if section is not None:
            body = pr.replace_pr_evidence(body, section)
        pr.edit_pr_body(project, pull, body)
    if text is not None and sha and conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pr_text_sha", sha)


def _refreshed_prose(project, conn, run_id, task_id, task, branch, ticket,
                     beat_s, wt, budget_min, own, answered):
    """The rewritten description with its maintained history, or None when
    the writing turn is refused."""
    written = _written_pr_text(
        project, conn, run_id, task_id, task, branch, ticket, beat_s, wt,
        monotonic(), budget_min or PR_TEXT_BUDGET_MIN, None,
        refresh=(own, answered))
    if written is None:
        return None
    log_changes = merge_config(project).pr_changes_log
    description, changes = _without_changes(written[1])
    if (not description.strip() or (log_changes and
            (len(changes) != 1 or not changes[0][2:].strip()))):
        print(f"[holo2] written PR text refused for {task_id}: missing behaviour"
              " summary; leaving the pull request body unchanged")
        return None
    text = description.rstrip()
    if log_changes:
        _, history = _without_changes(own)
        history.append(f"- Round {len(history) + 1}: {changes[0][2:]}")
        text += "\n\n" + CHANGES_HEADING + "\n" + "\n".join(history)
    return text


def _prepare_pr(project, conn, run_id, task_id, task, branch, body, beat_s,
                wt, started, budget_min, issue_url=None, lead=None):
    """`[merge] mode = "pr"`, the half of opening the pull request that
    needs no lock: capture the evidence and write the PR text, falling back
    to a stub on failure. `lead`, when given, is the loop's own first line
    of the body, ahead of the written text (KO-658). Returns the
    `(title, text)` `_push_and_open()` opens the pull request with
    (KO-644)."""
    with heartbeat_while(conn, run_id, beat_s):
        evidence = pr_media.prepare(
            project, wt, task_id,
            evidence_states=ticket_template.parse(body).evidence_states,
            record_note=lambda text: ledger(conn, run_id, task_id, "note", text, None))
    title, text = _written_pr_text(project, conn, run_id, task_id, task,
                                   branch, body, beat_s, wt, started,
                                   budget_min, issue_url)
    if lead:
        text = f"{lead}\n\n{text}"
    return title, pr_media.append(text, evidence)


def _push_and_open(project, conn, run_id, branch, title, text, beat_s):
    """`[merge] mode = "pr"`: push the approved candidate and open its pull
    request with `title` and `text`; return the PR's URL. The caller holds
    the merge lock: the push runs in the target checkout, whose refs a
    claim's fetch moves under the same lock.

    `git push origin BRANCH`, then the PR, so a PR never names a branch the
    remote does not hold. Either refusing is `InfraFailure` out of
    `holophyte.pr`: the route gave out, not the ticket, so no strike is
    spent and the branch and worktree stay exactly as after a refused
    merge. Nothing touches main.

    Adopt an existing PR on this branch, then babysit as usual (KO-407).

    Remote calls run under `heartbeat_while()`: a slow push is not a dead
    loop to sweep before its URL is recorded (KO-259 review round 1).
    """
    # Still the `merge_gate` phase: the push and the create are the mode's
    # way out of the gate, named on the stream rather than as a phase move.
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"pushing {branch} to {pr.REMOTE} and opening its"
                           " pull request")
    adopted = False
    with heartbeat_while(conn, run_id, beat_s):
        pr.push_branch(project, branch)
        print(f"[holo2] pushed {branch} to {pr.REMOTE}")
        # A branch already open as a pull request is adopted, not opened
        # again: `gh pr create` refuses with one still open, which is how
        # a requeued run used to fail after doing everything right.
        url = pr.open_pull_request(project, branch)
        if url is None:
            url = pr.create_pull_request(project, branch, title, text)
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
            # The PR is the run's `prUrl` from this moment, not only
            # from a later park: a run that merges without parking
            # still names it on its row, whether opened or adopted.
            store.set_pull_request(conn, run_id, url)
    return url


def _park_human(project, conn, run_id, provider, task_id, branch, sha, pull,
                human, listed, reviewed):
    """Park the run on the threads the pass found `HUMAN`, each quoted in
    the ticket's question, with `listed` as the open threads."""
    quoted = "\n\n".join(babysitter.quoted(t) for _, t, _ in human)
    _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                "a thread needs a human's answer; nothing was posted on"
                f" it:\n{quoted}", listed, reviewed=reviewed, park_kind="thread")


def _merge_pr(project, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
              pull, reviewed=None, retry_conflicts=False):
    """Merge the pinned candidate, clean up, and return its merge sha.
    Park on refusal unless the babysitter opts into raising 405 conflicts;
    operator approval retains the default park on every refusal."""
    set_phase(conn, run_id, "merging", f"merging {pull.url} through the"
              " pull request API")
    try:
        with heartbeat_while(conn, run_id, beat_s):
            # A queue on main lands the PR itself, main and PR tested together.
            merge_sha = (merge_queue.land_through_queue(project, conn, run_id,
                                                        pull, sha)
                         if merge_queue.merge_queue_required(project, pull)
                         else pr.merge_pull_request(project, pull, sha))
    except merge_queue.QueueLeft as left:
        removed = merge_queue.red_group(project, conn, run_id, pull, left)
        if removed is not None:
            raise removed from None  # The babysit's check fix turn's.
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    str(left), (), reviewed=reviewed)
    except pr.MergeRefused as refused:
        if (retry_conflicts and "405" in str(refused)
                and "merge conflicts" in str(refused).lower()):
            raise
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    f"GitHub refused the merge: {refused}", (),
                    reviewed=reviewed)
    print(f"[holo2] merged {pull.url} as {merge_sha[:12]}")
    # Local main is not moved: the factory never pushes it, and pulling it
    # here would make the writer host's checkout the loop's business. The
    # worktree and the local branch hold nothing the PR does not.
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], project.path)
        sh(["git", "branch", "-D", branch], project.path)
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


def _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                why, threads, reviewed=None, park_kind="pull_request"):
    """Park the run on its pull request: the ticket asks `PR open: URL`
    with `why` and the open `threads` listed, `store.park()` writes
    `runs.prUrl`, `runs.candidateSha` and -- `reviewed`, the sha the last
    independent judgement covered, when there is one -- `runs.approvedSha`
    with the phase move, the ledger carries the same, and `MergeParked`
    unwinds the run with branch and worktree left standing. The operator's
    ways on are `--approve KO-n` (merge it) and `--babysit KO-n` (look
    again, which merges at `reviewed` alone and reviews anything else) --
    and the supervisor's content-based wake rule. The park reads the PR after
    this pass's writes and records its mark, checks and review decision.
    Delayed updatedAt bumps alone cannot trigger another pass (KO-563).
    A failed read records no mark; reconcile initializes it without waking.
    """
    from holophyte.babysit_steps import record_step
    record_step(conn, run_id, "parked")
    short = sha[:12] if sha else "an unrecorded sha"
    question = babysitter.open_threads_question(pull, why, threads)
    if conn is not None and run_id is not None:
        ticket_id = store.read.run_snapshot(conn, run_id).ticketId
        if not block_ticket(conn, ticket_id, provider, question, park_kind=park_kind):
            print(f"[holo2] {task_id} could not be moved to"
                  " blocked_on_operator; parking the run anyway")
        store.park(conn, run_id, "awaiting_merge_approval",
                   f"{babysitter.gist(why)}; {branch} at {short} is open as"
                   f" {pull.url} ([merge] mode = \"pr\")",
                   candidate_sha=sha, pr_url=pull.url, approved_sha=reviewed,
                   pr_seen=_pr_seen(project, pull, conn, run_id), park_kind=park_kind)
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
