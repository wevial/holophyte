import re
import subprocess
from dataclasses import replace
from time import monotonic

import store
import store.read
import ticket_template
from holophyte import leak_guard
from holophyte.babysit import babysitter
from holophyte.board.projection import block_ticket, ledger
from holophyte.config.config_tables import board_mode, merge_config, sweep_config
from holophyte.host.reconcile import _pr_seen
from holophyte.loop import run as run_state
from holophyte.loop.gates import InfraFailure, MergeParked, RunFailure, sh
from holophyte.loop.runs import heartbeat_while, set_phase
from holophyte.loop.stop import resume_babysit_fix, stop_if_requested
from holophyte.pr import github, merge_queue, pr_activity, pr_media, pr_status
from holophyte.redact import safe_print as print
from holophyte.review import consolidate


def _resume_on_pr(run, carried, verify_cmd, contracts, body, criteria=()):
    """Only the sha an independent judgement covered merges without review."""
    project, conn, run_id, provider = run.project, run.conn, run.run_id, run.provider
    task_id, task, branch, wt = run.task_id, run.task, run.branch, run.wt
    from holophyte.loop.branch_sync import _sync_branch_from_origin

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
        verify_cmd, contracts, run.budget_min, carried, criteria)
    run = replace(run, sha=sha, pr_url=url)
    run = babysitter._babysit(
        run, beat_s, f"{task}\n\n{body}" if body else task,
        verify_cmd, contracts, criteria, approved=carried.approved,
        reviewed=reviewed, verified=None, just_pushed=pushed,
        fix_note=(None if carried.approved else
                  store.read.babysit_note(conn, carried.run_id)))
    return run_state.land(run, True)


PR_TEXT_DIFF_CAP = 60_000
PR_TEXT_BUDGET_MIN = 5
# The factory opens through the API, so the turn is handed the template.
PR_TEMPLATE_FILES = (".github/pull_request_template.md",
                     ".github/PULL_REQUEST_TEMPLATE.md",
                     "PULL_REQUEST_TEMPLATE.md")


def _pr_template(wt):
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
    """A refresh returns None on refusal, so its caller keeps the body."""
    from holophyte.loop.implement import _timed

    diff = sh(["git", "diff", "main...HEAD"], cwd=wt)
    if len(diff) > PR_TEXT_DIFF_CAP:
        diff = (diff[:PR_TEXT_DIFF_CAP]
                + "\n\n[diff truncated here: the change is larger than this"
                " prompt can carry; describe what is shown]")
    parts = [
        f"Write the pull request title and description for branch {branch},"
        f" the candidate for ticket {task_id}: {task}.",
        "Answer with exactly one line `TITLE: ...` (the title alone, under"
        f" {github.PR_TITLE_MAX} characters) followed by the description in"
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
    parsed = None if timed_out else github.parse_pr_text(reply)
    parsed = _with_own_key(parsed, task_id) if refresh is None else parsed
    leaks = parsed and leak_guard.text_leaks(project, "written pull request text",
                                             "\n".join(parsed))
    leak_guard.record(conn, run_id, leaks)
    native = board_mode(project).kind == "native"
    if parsed is None or not parsed[1] or leaks:
        why = ("the turn ran out of time" if timed_out
               else "the reply holds text the project does not publish"
               if leaks
               else "the reply has no `TITLE:` line, an empty title, or a"
               f" title over {github.PR_TITLE_MAX} characters, or an empty body")
        if refresh is not None:
            print(f"[holo2] written PR text refused for {task_id}: {why};"
                  " leaving the pull request body unchanged")
            return None
        print(f"[holo2] written PR text refused for {task_id}: {why};"
              " opening the pull request with the ticket's title and a stub")
        return github.pr_title(task_id, task), github.pr_body_stub(
            {"id": task_id, "body": body}, why, issue_url, native)
    title, text = parsed
    if refresh is not None:
        return title, text
    return title, github.pr_body_written(text, task_id, issue_url, native)


TRAILING_KEY = re.compile(r" \(([A-Z]+-\d+)\)\Z")


def _with_own_key(parsed, task_id):
    if parsed is None:
        return None
    title, text = parsed
    match = TRAILING_KEY.search(title)
    if match is None or match[1] == task_id:
        return parsed
    title = f"{title[:match.start()]} ({task_id})"
    return (title, text) if len(title) <= github.PR_TITLE_MAX else None

CHANGES_HEADING = "## Changes since first review"


def _without_changes(text):
    match = re.search(r"^## Changes since first review\s*\n(.*?)(?=^## |\Z)",
                      text, re.MULTILINE | re.DOTALL)
    if match is None:
        return text, []
    lines = [line for line in match[1].splitlines() if line.startswith("- ")]
    return text[:match.start()] + text[match.end():], lines


def refresh_pr_text(project, conn, run_id, task_id, task, branch, ticket,
                    beat_s, wt, budget_min, pull, answered, *, sha=None):
    """A refused turn never overwrites the existing prose."""
    written = bool(sha) and pr_activity.latest(conn, run_id, "pr_text_sha") == sha
    if written and not merge_config(project).ui_paths:
        return
    endpoint = f"repos/{pull.repo}/pulls/{pull.number}"
    with heartbeat_while(conn, run_id, beat_s):
        current = github.rest(project, pull, "GET", endpoint)["body"] or ""
    own, _, evidence, _ = github.split_pr_body(current)
    with heartbeat_while(conn, run_id, beat_s):
        section = pr_media.refresh(
            project, wt, task_id, evidence,
            evidence_states=ticket_template.parse(ticket).evidence_states,
            record_note=lambda text: ledger(conn, run_id, task_id, "note", text, None))
    text = None if written else _refreshed_prose(
        project, conn, run_id, task_id, task, branch, ticket, beat_s, wt,
        budget_min, own, answered)
    if text is None and section is None:
        return
    with heartbeat_while(conn, run_id, beat_s):
        body = github.rest(project, pull, "GET", endpoint)["body"] or ""
        if text is not None:
            body = github.replace_pr_text(body, text)
        if section is not None:
            body = github.replace_pr_evidence(body, section)
        github.edit_pr_body(project, pull, body)
    if text is not None and sha and conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pr_text_sha", sha)


def _refreshed_prose(project, conn, run_id, task_id, task, branch, ticket,
                     beat_s, wt, budget_min, own, answered):
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
    return title, _with_concerns(pr_media.append(text, evidence), conn, run_id)


def _with_concerns(text, conn, run_id):
    held = consolidate.held_concerns(conn, run_id)
    if not held:
        return text
    return (f"{text.rstrip()}\n\n## Other concerns ({len(held)})\n"
            + "\n".join(f"- {consolidate.bullet(item)}" for item in held))


def _push_and_open(project, conn, run_id, branch, title, text, beat_s):
    """The caller holds the merge lock a claim's fetch of these refs takes."""
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "pull_request",
                           f"pushing {branch} to {github.REMOTE} and opening its"
                           " pull request")
    adopted = False
    leak_guard.refuse_private_text(project, pull_request_title=title,
                                   pull_request_body=text)
    with heartbeat_while(conn, run_id, beat_s):
        github.push_branch(project, branch)
        print(f"[holo2] pushed {branch} to {github.REMOTE}")
        # `gh pr create` refuses a branch already open as a PR: adopt it.
        url = github.open_pull_request(project, branch)
        if url is None:
            url = github.create_pull_request(project, branch, title, text)
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
            # The PR is the run's `prUrl` from now, even for a run that never parks.
            store.set_pull_request(conn, run_id, url)
    if adopted:
        with heartbeat_while(conn, run_id, beat_s):
            _adopt_evidence(project, url, text)
    return url


def _adopt_evidence(project, url, text):
    section = github.split_pr_body(text)[2]
    if not section:
        return
    pull = pr_status.parse_pr_url(url)
    body = github.rest(project, pull, "GET",
                       f"repos/{pull.repo}/pulls/{pull.number}")["body"] or ""
    if github.split_pr_body(body)[2].rstrip() != section.rstrip():
        github.edit_pr_body(project, pull,
                            github.replace_pr_evidence(body, section))


def _park_human(project, conn, run_id, provider, task_id, branch, sha, pull,
                human, listed, reviewed):
    quoted = "\n\n".join(babysitter.quoted(t) for _, t, _ in human)
    _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                "a thread needs a human's answer; nothing was posted on"
                f" it:\n{quoted}", listed, reviewed=reviewed, park_kind="thread")


def _merge_pr(project, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
              pull, reviewed=None, retry_conflicts=False):
    set_phase(conn, run_id, "merging", f"merging {pull.url} through the"
              " pull request API")
    behind = None
    if merge_config(project).require_up_to_date:
        with heartbeat_while(conn, run_id, beat_s):
            behind = _behind_main(wt, sha)
    if behind and retry_conflicts:
        raise github.MergeRefused(behind)
    if behind:
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    behind, (), reviewed=reviewed)
    try:
        with heartbeat_while(conn, run_id, beat_s):
            # A queue on main lands the PR itself, main and PR tested together.
            merge_sha = (merge_queue.land_through_queue(project, conn, run_id,
                                                        pull, sha)
                         if merge_queue.merge_queue_required(project, pull)
                         else github.merge_pull_request(project, pull, sha))
    except merge_queue.QueueLeft as left:
        removed = merge_queue.red_group(project, conn, run_id, pull, left)
        if removed is not None:
            raise removed from None
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    str(left), (), reviewed=reviewed)
    except github.MergeRefused as refused:
        if (retry_conflicts and "405" in str(refused)
                and "merge conflicts" in str(refused).lower()):
            raise
        _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                    f"GitHub refused the merge: {refused}", (),
                    reviewed=reviewed)
    print(f"[holo2] merged {pull.url} as {merge_sha[:12]}")
    # Local main is not moved: the factory never pushes it.
    try:
        sh(["git", "worktree", "remove", "--force", str(wt)], project.path)
        sh(["git", "branch", "-D", branch], project.path)
    except RuntimeError as e:
        print(f"[holo2] post-merge cleanup left debris: {e}")
    return merge_sha


def _behind_main(wt, sha):
    ref = f"{github.REMOTE}/{github.BASE}"
    try:
        fetched = subprocess.run(
            ["git", "fetch", github.REMOTE,
             f"+refs/heads/{github.BASE}:refs/remotes/{ref}"],
            cwd=wt, capture_output=True, text=True, timeout=github.PR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise InfraFailure(f"git fetch {github.REMOTE} {github.BASE} did not answer in"
                           f" {github.PR_TIMEOUT}s; branch preserved") from None
    if fetched.returncode != 0:
        raise InfraFailure(f"git fetch {github.REMOTE} {github.BASE} failed before the"
                           f" merge: {(fetched.stderr or fetched.stdout).strip()}")
    tip = subprocess.run(["git", "rev-parse", "--verify", "-q", ref], cwd=wt,
                         capture_output=True, text=True).stdout.strip()
    if not tip:
        raise InfraFailure(f"{ref} did not resolve after its fetch; the merge"
                           f" of {sha[:12]} waits")
    ancestry = subprocess.run(["git", "merge-base", "--is-ancestor", tip, sha],
                              cwd=wt, capture_output=True, text=True)
    if ancestry.returncode == 0:
        return None
    if ancestry.returncode != 1:
        raise InfraFailure(f"git merge-base could not place {sha[:12]} against"
                           f" {ref}: {ancestry.stderr.strip()}")
    return (f"the candidate at {sha[:12]} is behind main: {ref} at"
            f" {tip[:12]} is not in it")


def _landed_pr(conn, run_id, provider, task_id, task, branch, url, merge_sha,
               started, budget_min, rnd):
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
    """Delayed updatedAt bumps alone cannot trigger another pass."""
    from holophyte.babysit.babysit_steps import record_step
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
