"""PR babysitting: adjudicate threads, verify fixes, and wait for a safe merge."""
import json
import re
import subprocess
import tempfile
from pathlib import Path
from time import monotonic, time

import review_runner
import store
import store.read
from holophyte import babysitter, maintainer_notes, pr, pr_status, thread_mentions
from holophyte.agents import agent_route, review_refs
from holophyte.board import ledger
from holophyte.bot_threads import route_bot_threads
from holophyte.config_tables import merge_config
from holophyte.conversation_comments import quote_request
from holophyte.gates import (
    InfraFailure,
    RunFailure,
    VerificationOutput,
    record_unreviewed_verification,
    run_verify,
    with_baseline,
)
from holophyte.pr import NO_AUTHOR
from holophyte.pr_head import _just_pushed_state, _pr_terminal
from holophyte.review import (
    criteria_brief,
    criteria_findings,
    evidence_brief,
    parse_findings,
)
from holophyte.runs import heartbeat_while, record_round
from store.instructions import record_instruction_reply

# Quote conventions in brief order, capped per file, so rules aren't guessed.
CONVENTIONS_FILES = ("AGENTS.md", "CLAUDE.md")
CONVENTIONS_CAP = 4000

VERDICTS = ("ADDRESS", "DECLINE", "HUMAN")
# `THREAD 2: ADDRESS -- the null check is missing`, one per thread; the
# separator after the verdict is whatever the model reached for.
VERDICT_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?THREAD\s+(\d+)\s*[:.)-]\s*(ADDRESS|DECLINE|HUMAN)\b"
    r"\s*(?:[-–—:,]+\s*)?(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
# `THREAD 2: <what changed>` in the fix round's output, one per addressed
# thread; the reply on the thread carries it beside the sha.
SUMMARY_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?THREAD\s+(\d+)\s*[:.)-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)
COMMENT_HEADER = "---- Comment by {model} ----"
# How much of a thread's body the round row, the ledger and the parked
# question carry: enough to recognise it, not the whole thread.
GIST_CHARS = 200


def gist(text, limit=GIST_CHARS):
    """`text` as one line of at most `limit` characters."""
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def where(thread):
    """A review location or pull request conversation label."""
    if thread.kind == "conversation":
        return "conversation on the pull request"
    if not thread.path:
        return "(no file)"
    return f"{thread.path}:{thread.line}" if thread.line else thread.path


def conversation(thread, *, label_all=False):
    """A thread's text as the adjudicator and implementer read it: the
    opening comment, then each follow-up under a line naming who wrote it
    -- a later rejection or question is judged, not the opener alone."""
    if label_all:
        return "\n\n".join(f"@{c.author}: {c.body.strip()}" for c in thread.comments)
    parts = [thread.body.strip()]
    parts.extend(f"@{c.author} replied:\n{c.body.strip()}" for c in thread.replies)
    return "\n\n".join(parts)


def thread_line(number, thread):
    """One thread as one line: number, where, who, and the gist of it."""
    return f"{number}. {where(thread)} (@{thread.author}): {gist(thread.body)}"


def conventions(wt):
    """The repository's conventions files at the worktree root, `(name,
    text)` per file present in `CONVENTIONS_FILES` order; the same lookup
    feeds the pull request text and the adjudication brief."""
    return tuple((n, (wt / n).read_text(errors="replace").strip())
                 for n in CONVENTIONS_FILES if (wt / n).is_file())


def conventions_paragraph(files):
    """The brief's conventions excerpt, capped per file; empty with none."""
    if not files:
        return ""
    parts = []
    for name, text in files:
        if len(text) > CONVENTIONS_CAP:
            text = (text[:CONVENTIONS_CAP]
                    + f"\n\n[{name} truncated here]")
        parts.append(f"The repository's {name}:\n\n{text}")
    return "\n\n".join(parts) + "\n\n"


def adjudication_brief(pull, threads, ticket, sha, conventions=(), run_id=None):
    """The adjudicator's goal: the numbered threads, the verdicts to give
    each, and the repository's conventions when it has any."""
    listing = "\n\n".join(
        f"THREAD {n} -- {where(t)} by @{t.author}"
        + (" (outdated: the lines it was left on have changed)"
           if t.outdated else "")
        + (f" ({len(t.replies)} follow-up(s))" if t.replies else "")
        + f"\n{conversation(t, label_all=True)}"
        for n, t in enumerate(threads, 1))
    return (
        f"You are a READ-ONLY adjudicator of the review threads on pull "
        f"request {pull.url}. Judge commit {sha} using {review_refs(run_id)[0]} as "
        f"the frozen base and {review_refs(run_id)[1]} as the candidate in this "
        "repo, against the ticket below. The ticket is the contract: a "
        "thread asking for work outside it is out of scope.\n\n"
        f"{ticket}\n\n"
        f"Unresolved review threads ({len(threads)}):\n\n{listing}\n\n"
        + conventions_paragraph(conventions)
        + people_paragraph(threads)
        + "For EACH thread give exactly one verdict line, in this form and "
        "nothing else on the line:\n"
        "THREAD n: ADDRESS -- one sentence naming the defect to fix\n"
        "THREAD n: DECLINE -- one sentence saying why it is not a defect or "
        "not in scope\n"
        "THREAD n: HUMAN -- one sentence saying why a person must answer\n"
        "ADDRESS is for a concrete defect in the candidate. A thread that "
        "names an existing function, helper or constant already in the "
        "repository which the diff duplicates is a concrete change request, "
        "not a preference: ADDRESS, the fix being reuse. The repository's "
        "own conventions (its AGENTS.md or CLAUDE.md, quoted above when it "
        "has one) are the reviewer's standard: a thread asking for what "
        "they ask for is concrete. DECLINE is for a thread that asks for "
        "nothing specific, or asks for what the ticket puts out of scope. "
        "HUMAN is for a genuine question, a rejection of the approach, or "
        "anything you would not answer on the operator's behalf. Judge each "
        "thread by its whole conversation: a concrete change stated by a later reply "
        "is the thread's request. A follow-up can withdraw, "
        "sharpen, or turn a finding into a question. Do not modify "
        "anything.")


def people_paragraph(threads):
    """The brief's paragraph on the threads a person opened, numbered as
    the listing has them; empty when every thread is a bot's."""
    people = [str(n) for n, t in enumerate(threads, 1)
              if t.author_kind != "bot"]
    if not people:
        return ""
    return (
        f"THREAD {', '.join(people)} " + ("was" if len(people) == 1 else
                                          "were")
        + " opened by a person, not a bot. For a person's thread give "
        "ADDRESS only when it asks for a concrete change the diff can make "
        "(\"change X to Y\", \"this should also handle Z\", \"rename "
        "this\"). A question, a request for reasoning, a design objection, "
        "a request outside the ticket, or anything you are not sure is a "
        "change request is HUMAN -- do not guess in the person's favour. "
        "Never DECLINE a person's thread: the factory does not argue with a "
        "person; a DECLINE on it is read as HUMAN.\n\n")


def parse_verdicts(reply, count):
    """`{number: (verdict, reason)}` for threads 1..`count` off the
    adjudicator's reply; no verdict line is `HUMAN`; last line wins."""
    found = {}
    for m in VERDICT_LINE_RE.finditer(reply or ""):
        number = int(m.group(1))
        if 1 <= number <= count:
            found[number] = (m.group(2).upper(), m.group(3).strip())
    return {n: found.get(n, ("HUMAN", "the adjudicator gave no verdict for"
                                      " this thread"))
            for n in range(1, count + 1)}


def fix_brief(pull, addressed, ticket):
    """The implementer's goal for the fix round: the addressed threads,
    numbered as the adjudicator saw them, and the summary line for each."""
    listing = "\n\n".join(
        f"THREAD {n} -- {where(t)} by @{t.author}\n{conversation(t)}\n"
        + (maintainer_notes.instruction(t) if maintainer_notes.is_note(t)
         else thread_mentions.instruction(t) if t.classification == "MENTIONED"
         else f"Adjudicator: {reason}")
        for n, t, reason in addressed)
    return (
        f"Review threads on pull request {pull.url} were accepted as "
        "defects. The ticket you are held to, acceptance criteria "
        f"included:\n\n{ticket}\n\nThreads to address:\n\n{listing}\n\n"
        "Fix each one on this branch and commit; keep the ticket's verify "
        "commands passing. Then end your reply with one line per thread, "
        "in this form:\nTHREAD n: one sentence saying what changed")


def parse_summaries(output):
    """`{number: summary}` off the fix round's output; last line wins."""
    return {int(m.group(1)): m.group(2).strip()
            for m in SUMMARY_LINE_RE.finditer(output or "")}


def addressed_reply(model, summary, sha):
    """The reply on an addressed thread: the header, what changed, the sha."""
    return (f"{COMMENT_HEADER.format(model=model)}\n\n"
            f"Addressed in {sha}: {summary}")


def declined_reply(model, reason):
    """The reply on a declined thread: the header and the reason."""
    return (f"{COMMENT_HEADER.format(model=model)}\n\n"
            f"Declined: {reason}")


def round_reply(pull, pass_no, threads, verdicts, checks, sha):
    """The text a pass is recorded as, in `record_round()`'s shape: one
    bullet per thread citing its file and verdict, and a closing
    `VERDICT:` -- `APPROVE` when no thread was found."""
    lines = [f"Babysit pass {pass_no} over {pull.url} at {sha[:12]}:"
             f" {len(threads)} unresolved thread(s), checks {checks}."]
    lines += [f"- {where(t)} @{t.author}: {gist(t.body)}"
              f" -- {t.classification + ': ' if t.classification else ''}"
              f"{verdicts[n][0]}: {' '.join(verdicts[n][1].split())}"
              for n, t in enumerate(threads, 1)]
    lines.append("VERDICT: " + ("APPROVE" if not threads
                                else "REQUEST_CHANGES"))
    return "\n".join(lines)


def route_of(threads):
    """`github:LOGIN` for the pass: the threads' authors sorted and joined
    with `+`; `github:ci` for a pass that judged the checks alone."""
    authors = sorted({t.author for t in threads})
    return "github:" + ("+".join(authors) if authors else NO_AUTHOR)


def open_threads_question(pull, why, threads):
    """The ticket's question for a run parked on its PR: the URL, why the
    pass stopped, and the open threads listed."""
    return "\n".join([f"PR open: {pull.url}", why]
                     + [thread_line(n, t) for n, t in enumerate(threads, 1)])


def quoted(thread):
    """A thread quoted whole, follow-ups included, for the parked question."""
    body = "\n".join(f"> {line}" for line in conversation(thread)
                     .splitlines()) or "> (empty)"
    return f"{where(thread)} by @{thread.author} ({thread.url}):\n{body}"


def _merge_origin_main(target, conn, run_id, provider, task_id, branch, wt,
                       sha, beat_s, pull, budget_min, reviewed=None, refusal=None,
                       previous=None, refresh=None, verify_cmd=None, contracts=(),
                       ticket=""):
    """Merge and push main; unresolved conflicts get one turn, then park."""
    from holophyte.claim import merge_conflicts
    from holophyte.loop import _timed, sh
    from holophyte.merge_gate import _is_ancestor, _merge_ref, merge_conflict_goal
    from holophyte.pullrequest import _park_on_pr
    with heartbeat_while(conn, run_id, beat_s):
        fetched = subprocess.run(["git", "fetch", pr.REMOTE], cwd=wt,
                                 capture_output=True, text=True)
    ref = f"{pr.REMOTE}/{pr.BASE}"
    if fetched.returncode != 0 or subprocess.run(
            ["git", "rev-parse", "--verify", "-q", ref], cwd=wt,
            capture_output=True).returncode != 0:
        raise InfraFailure(f"git fetch {pr.REMOTE} did not deliver {ref}"
                           f" for the conflicting {pull.url}:"
                           f" {(fetched.stderr or fetched.stdout).strip()}"
                           f"; branch {branch} preserved at {sha[:12]}")
    before = _diff_identity(wt, ref)
    status, detail = _merge_ref(wt, ref)
    if status == "conflicted":
        _timed(target, conn, run_id, beat_s, wt, budget_min,
               merge_conflict_goal(branch, pull, detail))
        still = merge_conflicts(wt)
        if still or not _is_ancestor(wt, ref, "HEAD"):
            if still:
                subprocess.run(["git", "merge", "--abort"], cwd=wt,
                               capture_output=True, text=True)
            _park_on_pr(
                target, conn, run_id, provider, task_id, branch, sha, pull,
                (f"GitHub refused the merge: {refusal}; " if refusal else "")
                + f"GitHub reported the pull request conflicting; merging"
                f" {pr.BASE} into {branch} stopped on"
                f" {', '.join(still or detail)} and the implementer turn"
                " left it unresolved", (), reviewed=reviewed)
        merged = sh(["git", "rev-parse", "HEAD"], wt)
    elif status == "ancestor":
        # `origin/main` is already in the branch: GitHub's answer was
        # stale, or an earlier pass merged it. Push anyway so the remote
        # head stands at the merged sha and GitHub recomputes.
        merged = sha
    else:
        merged = detail
    with heartbeat_while(conn, run_id, beat_s):
        pr.push_branch(target, branch)
    print(f"[holo2] pushed {branch} to {pr.REMOTE} at {merged[:12]}"
          " after the conflict merge")
    if merged != sha:
        note = (f"Merged main into {branch} at {merged} (GitHub reported"
                " a conflict)")
        if conn is not None and run_id is not None:
            store.record_ledger(conn, run_id, "note", note)
    merged = _verify_main_refresh(
        target, conn, run_id, provider, task_id, branch, wt, merged, beat_s,
        pull, budget_min, verify_cmd, contracts, ticket, ref)
    state = _wait_for_pushed_head(
        target, conn, run_id, provider, task_id, branch, merged, beat_s, pull, reviewed)
    if merged != sha and before == _diff_identity(wt, ref):
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "pull_request",
                               f"main refreshed at {merged}; diff to main unchanged,"
                               " review and quiet carried forward")
        if reviewed == sha:
            reviewed = merged
        if previous is not None and refresh is not None:
            quiet_at = refresh.get((sha, previous.updated_at), previous.updated_at)
            refresh.clear()
            refresh[(merged, state.updated_at)] = quiet_at
    return merged, state, reviewed


def _refresh_verify(target, conn, run_id, beat_s, wt, sha, command, contracts):
    """Record the checked tree beside its mechanical result, including red main."""
    started = int(time() * 1000)
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(command, wt, contracts, conn=conn, run_id=run_id)
        ok, out = with_baseline(target, wt, command, ok, out,
                               conn, run_id)
    out.results = [dict(row, output=f"Tree {sha}\n{row['output']}")
                   for row in out.results]
    record_round(target, conn, run_id, _next_round(conn, run_id), "review",
                 "VERDICT: " + ("APPROVE" if ok else "REQUEST_CHANGES"),
                 command, ok, VerificationOutput(f"Tree {sha}\n{out}", out.results),
                 started_at=started,
                 route="mechanical:main-refresh")
    return ok, out


def _verify_detached_main(target, conn, run_id, beat_s, wt, ref, command, contracts):
    """Check the fetched main once in an isolated sibling, then remove it."""
    from holophyte.loop import sh
    sha = sh(["git", "rev-parse", ref], wt)
    with tempfile.TemporaryDirectory(prefix="main-verify-", dir=wt.parent) as tmp:
        detached = Path(tmp) / "tree"
        sh(["git", "worktree", "add", "--detach", str(detached), sha], wt)
        try:
            ok, out = _refresh_verify(target, conn, run_id, beat_s, detached,
                                      sha, command, contracts)
        finally:
            sh(["git", "worktree", "remove", "--force", str(detached)], wt)
    return sha, ok, out


def _verify_main_refresh(target, conn, run_id, provider, task_id, branch, wt,
                         sha, beat_s, pull, budget_min, command, contracts, ticket,
                         ref):
    """Verify before carrying review forward; diagnose red once before a fix."""
    from holophyte.pullrequest import _park_on_pr
    ok, out = _refresh_verify(target, conn, run_id, beat_s, wt, sha, command, contracts)
    if ok:
        return sha
    main_sha, main_ok, main_out = _verify_detached_main(
        target, conn, run_id, beat_s, wt, ref, command, contracts)
    if not main_ok:
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    f"main is red at {main_sha}; verify command: {command}\n"
                    f"Merged tree:\n{out}\nMain:\n{main_out}\n"
                    "Fix main and send this run back through babysit.", ())
    goal = (f"The merge of main introduced a verification failure on {pull.url}; "
            f"main at {main_sha} passes. Failing verify command: {command}\n{out}\n"
            f"The ticket is the contract:\n{ticket}\n"
            "Fix this failure on this branch and commit; keep the ticket's verify "
            "commands passing. This is one implementer fix turn.")
    return _fix_threads(target, conn, run_id, provider, task_id, branch, wt, sha,
                        beat_s, pull, (), None, ticket, command, contracts,
                        budget_min, _next_round(conn, run_id),
                        review_follows=merge_config(target).approve == "auto",
                        goal=goal)


def _diff_identity(wt, ref):
    """Compare the candidate against the fetched main, never a stale local main."""
    diff = subprocess.run(["git", "diff", f"{ref}...HEAD"], cwd=wt,
                          capture_output=True, check=True).stdout
    return subprocess.run(["git", "patch-id", "--stable"], cwd=wt, input=diff,
                          capture_output=True, check=True).stdout


def _wait_for_pushed_head(target, conn, run_id, provider, task_id, branch,
                          sha, beat_s, pull, reviewed):
    """Bound propagation of our known push, then hand its state to settling."""
    from holophyte.pullrequest import _park_on_pr
    waited = 0
    wait_s = merge_config(target).pr_poll_sec
    with heartbeat_while(conn, run_id, beat_s):
        state = pr_status.pr_state(target, pull)
        while state.head_sha != sha and waited < wait_s:
            nap = min(pr.CHECK_POLL_S, wait_s - waited)
            pr.SLEEP(nap)
            waited += nap
            state = pr_status.pr_state(target, pull)
    if state.head_sha != sha:
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    f"the pull request's head is {(state.head_sha or '?')[:12]}"
                    f" after {waited}s; the babysitter pushed {sha[:12]}", (),
                    reviewed=reviewed)
    return state


def _babysit(target, conn, run_id, provider, task_id, issue_id, task, branch,
              wt, sha, beat_s, url, ticket, verify_cmd, contracts, budget_min,
              criteria=(), approved=False, reviewed=None, verified=None, fix_note=None,
              just_pushed=False):
    """Watch a PR until merge or park, bounded by rounds and a no-work deadline.
    Changed candidates need verification and independent review; human approval
    covers only the released SHA. Conflict recovery pushes origin/main's merge."""
    from holophyte.pullrequest import _park_on_pr
    merge = merge_config(target)
    pull = pr_status.parse_pr_url(url)
    if pull is None:
        raise RunFailure(f"cannot read a pull request off {url!r};"
                         f" branch {branch} preserved at {sha[:12]}")
    ticket = maintainer_notes.amended_ticket(conn, run_id, ticket, url)
    model = agent_route(target, "adjudicate")
    # A fix moves sha past the candidate covered by reviewed.
    pushed_state = (_just_pushed_state(
        target, conn, run_id, provider, task_id, branch, sha, beat_s, pull,
        reviewed) if just_pushed else None)
    refresh = {}  # Only the known main-refresh update inherits the quiet clock.
    for pass_no in range(1, merge.pr_rounds + 1):
        state = _settled_or_park(
            target, conn, run_id, beat_s, pull, pushed_state, provider,
            task_id, branch, sha, reviewed, refresh)
        pushed_state = None
        done = _pr_terminal(target, conn, run_id, provider, task_id, branch,
                            sha, pull, state, reviewed)
        if done is not None:
            return done
        if state.mergeable == "CONFLICTING":
            # Push origin/main's merge and settle again; UNKNOWN is not conflict.
            sha, pushed_state, reviewed = _merge_origin_main(
                target, conn, run_id, provider, task_id, branch, wt, sha, beat_s,
                pull, budget_min, reviewed=reviewed, previous=state, refresh=refresh,
                verify_cmd=verify_cmd, contracts=contracts, ticket=ticket)
            continue
        rnd = _next_round(conn, run_id)
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
            fixed = _review_fix(target, conn, run_id, provider, task_id, branch, wt,
                                sha, reviewed, beat_s, pull, ticket, verify_cmd,
                                contracts, criteria, fix_note, budget_min)
            if fixed != sha:
                fix_note = None  # One fix allowance per babysit, past the cap too.
                sha = reviewed = fixed
                pushed_state = _wait_for_pushed_head(
                    target, conn, run_id, provider, task_id, branch, sha,
                    beat_s, pull, reviewed)
                continue  # Settle the pushed fix's checks and threads first.
            # Keep verified behind: the reviewed fix still needs the merge gate.
            reviewed = sha
        if merge.approve == "auto" or approved:
            try:
                return _verified_merge(target, conn, run_id, provider, task_id,
                                       issue_id, branch, wt, sha, beat_s, pull,
                                       reviewed, verified, verify_cmd, contracts,
                                       ticket, budget_min, merge.approve == "auto")
            except pr.MergeRefused as refused:
                verified = sha
                sha, pushed_state, reviewed = _merge_origin_main(
                    target, conn, run_id, provider, task_id, branch, wt, sha,
                    beat_s, pull, budget_min, reviewed=reviewed,
                    refusal=refused, previous=state, refresh=refresh,
                    verify_cmd=verify_cmd, contracts=contracts, ticket=ticket)
                continue
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    "ready to merge; waiting for a human to say merge"
                    " ([merge] approve = \"human\")", (), reviewed=reviewed)
    state = _settled_or_park(
        target, conn, run_id, beat_s, pull, pushed_state, provider,
        task_id, branch, sha, reviewed, refresh)
    _pr_terminal(target, conn, run_id, provider, task_id, branch, sha,
                 pull, state, reviewed)
    _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                f"[merge] pr_rounds = {merge.pr_rounds} passes made; the"
                " babysitter stops here", state.threads, reviewed=reviewed)


def _verified_merge(target, conn, run_id, provider, task_id, issue_id, branch,
                    wt, sha, beat_s, pull, reviewed, verified, verify_cmd,
                    contracts, ticket, budget_min, retry_conflicts):
    """Gate a changed candidate before attempting the PR merge."""
    from holophyte.merge_gate import _merge_gate
    from holophyte.pullrequest import _merge_pr
    if sha != verified:
        _merge_gate(target, conn, run_id, provider, task_id, issue_id, branch,
                    wt, beat_s, sha, verify_cmd, contracts, ticket, budget_min,
                    sync_main=False)
    return _merge_pr(target, conn, run_id, provider, task_id, branch, wt, sha,
                     beat_s, pull, reviewed=reviewed, retry_conflicts=retry_conflicts)


def _moved(sha, reviewed):
    """Why the candidate at `sha` needs an independent look: it sits past
    the sha the last judgement covered, or nothing on record covers it."""
    if reviewed is None:
        return (f"no approval on record covers the candidate at {sha[:12]}"
                " (the last review asked for changes, or the park recorded"
                " none)")
    return (f"the fix rounds moved the candidate from {reviewed[:12]} to"
            f" {sha[:12]}; the release covered {reviewed[:12]}")


def _fix_answers(conn, run_id, rnd, fix_note):
    """Recover addressed adjudications since the preceding independent pass."""
    lines, rounds = [], store.read.rounds_of(conn, run_id) if conn is not None else []
    for recorded in reversed(rounds):
        if recorded.round >= rnd:
            continue
        if not recorded.reviewerModel.startswith("github:"):
            break
        lines[:0] = [line for finding in json.loads(recorded.findings)
                     for line in (f"ADDRESS: {' '.join(finding['request'].split())}"
                                  if finding.get("kind") == "instruction"
                                  else finding["message"]).splitlines()
                     if "ADDRESS:" in line]
    if fix_note:
        lines.append(f"Operator babysit note: {fix_note}")
    return "\n".join(lines)


def _review_fix(target, conn, run_id, provider, task_id, branch, wt, sha,
                reviewed, beat_s, pull, ticket, verify_cmd, contracts,
                criteria=(), fix_note=None, budget_min=None, *, fix_context=""):
    """Verify and review; allow one fix past the cap, then park on rejection."""
    from holophyte.loop import _verify_brief, agent, set_phase, sh
    from holophyte.pullrequest import _park_on_pr, refresh_pr_text
    if merge_config(target).approve != "auto":
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha,
                    pull, f"{_moved(sha, reviewed)}, and a human"
                    " says merge on the candidate as it stands"
                    " ([merge] approve = \"human\")", (),
                    reviewed=reviewed)
    set_phase(conn, run_id, "verifying", f"verify the fix at {sha[:12]}"
              " before its review")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts, conn=conn, run_id=run_id)
        ok, out = with_baseline(target, wt, verify_cmd, ok, out,
                               conn, run_id)
    if not ok:
        record_unreviewed_verification(conn, run_id, out)
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
            f"{review_refs(run_id)[0]} as the frozen base and {review_refs(run_id)[1]} "
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
            + evidence_brief(target, wt, task_id)
            + "Do not modify anything. End your reply with exactly one "
            "line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha=base_sha, candidate_sha=sha, conn=conn, run_id=run_id)
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
    recovered = _fix_answers(conn, run_id, rnd, fix_note)
    answered = "\n".join(part for part in (fix_context, recovered) if part)
    if not unwitnessed and review_runner.terminal_verdict(verdict) == "APPROVE":
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: APPROVE of the fix at {sha} on {pull.url}\n"
               f"Reviewer verdict:\n{verdict}", provider)
        print(f"[holo2] the fix at {sha[:12]} is approved")
        if not answered:
            answered = sh(["git", "log", "--format=%s",
                           f"{reviewed or base_sha}..{sha}"], cwd=wt)
        refresh_pr_text(target, conn, run_id, task_id, ticket.splitlines()[0],
                        branch, ticket, beat_s, wt, budget_min, pull, answered)
        return sha
    ledger(conn, run_id, task_id, "round",
           f"Round {rnd}: REQUEST_CHANGES on the fix at {sha} on"
           f" {pull.url}; not merged\nReviewer findings:\n{verdict}",
           provider)
    if fix_note is not None and (unwitnessed or
            review_runner.terminal_verdict(verdict) == "REQUEST_CHANGES"):
        goal = (f"Fix the pre-merge review findings on {pull.url}. The ticket"
                f" is the contract:\n\n{ticket}\n\nReviewer findings:\n{verdict}"
                f"\n\nOperator babysit note:\n{fix_note}\n\n"
                "Fix the blockers on this branch and commit; keep the ticket's"
                " verify commands passing.")
        ledger(conn, run_id, task_id, "round",
               f"Babysit review fix allowance after round {rnd}: one fix and"
               " recorded re-review, even if reviewRoundCap is spent.", provider)
        fixed = _fix_threads(target, conn, run_id, provider, task_id, branch,
                             wt, sha, beat_s, pull, (), None, ticket, verify_cmd,
                             contracts, budget_min, rnd, review_follows=True, goal=goal)
        return _review_fix(target, conn, run_id, provider, task_id, branch, wt,
                           fixed, None, beat_s, pull, ticket, verify_cmd,
                           contracts, criteria, budget_min=budget_min,
                           fix_context=f"{answered}\n{verdict}")
    # No `reviewed`: the judgement on record is this rejection, so the
    # resume that follows reviews the candidate again before any merge.
    _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                f"the review of the fix at {sha[:12]} asked for changes;"
                f" not merged. Reviewer findings:\n{verdict}", ())


def _next_round(conn, run_id):
    """The number the run's next `reviewRounds` row takes; 1 with no store."""
    return len(store.read.rounds_of(conn, run_id)) + 1 if conn else 1


def _quiet_left(state, quiet_ms, refresh=None):
    """Quiet milliseconds remaining since the last PR update."""
    key = (state.head_sha, state.updated_at)
    if refresh and key not in refresh:
        refresh.clear()  # A later update or another head is real activity.
    updated_at = (refresh or {}).get(key, state.updated_at)
    if updated_at is None:
        return quiet_ms
    return max(0, quiet_ms - (int(time() * 1000) - updated_at))


def _settled_or_park(target, conn, run_id, beat_s, pull, state, provider,
                     task_id, branch, sha, reviewed, refresh=None):
    from holophyte.pullrequest import _park_on_pr
    try:
        state = state or pr_status.pr_state(target, pull)
        state = maintainer_notes.pending_state(conn, run_id, state, pull.url)
        return _settled_state(target, conn, run_id, beat_s, pull, state, refresh)
    except WaitExpired as expired:
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha,
                    pull, str(expired), (), reviewed=reviewed)


class WaitExpired(Exception):
    """A continuous PR wait reached its independent liveness deadline."""


def _settled_state(target, conn, run_id, beat_s, pull, state=None, refresh=None):
    """Bound pending/quiet waiting with one deadline; return threads promptly."""
    merge = merge_config(target)
    quiet_ms = merge.pr_quiet_sec * 1000
    deadline = monotonic() + merge.check_wait_sec
    with heartbeat_while(conn, run_id, beat_s):
        state = state or pr_status.pr_state(target, pull)
        state = route_bot_threads(target, conn, run_id, beat_s, pull, state, merge)
        while (not state.threads and not state.merged and not state.closed
               and state.mergeable != "CONFLICTING"):
            if state.checks == "pending":
                reason = "pending checks"
                if state.pending_contexts:
                    reason += f" ({', '.join(state.pending_contexts)})"
                nap = pr.CHECK_POLL_S
                print(f"[holo2] checks pending on {pull.url}; waiting"
                      f" {nap}s")
            elif state.checks == "success" \
                    and (left := _quiet_left(state, quiet_ms, refresh)):
                reason = "quiet wait"
                nap = min(merge.pr_poll_sec, left / 1000)
                print(f"[holo2] {pull.url} is green and quiet for"
                      f" {(quiet_ms - left) // 1000}s of the"
                      f" {quiet_ms // 1000}s required; waiting {nap}s")
            else:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise WaitExpired(
                    f"{reason} exceeded {merge.check_wait_sec}s on the pull request")
            pr.SLEEP(min(nap, remaining))
            state = pr_status.pr_state(target, pull)
            state = route_bot_threads(target, conn, run_id, beat_s, pull, state, merge)
    return state


def _answer_threads(target, conn, run_id, provider, task_id, branch, wt, sha,
                    beat_s, pull, state, rnd, pass_no, model, ticket,
                    verify_cmd, contracts, budget_min, reviewed=None):
    """Adjudicate threads, fix ADDRESSes, then reply to DECLINEs; return sha or park.
    Resolve declines only for configured bots or `[bot]` logins. Other declines
    park open. Human ADDRESSes under act are answered but stay open; other
    human verdicts park without a reply."""
    from holophyte.loop import agent, sh
    from holophyte.pullrequest import _park_human, _park_on_pr
    merge = merge_config(target)
    threads = tuple(thread_mentions.classify(t, merge.mention_handle)
                    for t in state.threads)
    base_sha = sh(["git", "merge-base", "main", sha], cwd=wt)
    round_started = int(time() * 1000)
    # Park unmentioned human threads unless human_threads = "act".
    # Mentions are instructions; bots alone enter adjudication under park.
    act = merge.human_threads == "act"
    judged = tuple(t for t in threads if not maintainer_notes.is_note(t)
                   and t.classification != "MENTIONED"
                   and (act or t.author_kind == "bot"))
    reply = "(no bot opened a thread; the adjudicator was not asked)"
    if judged:
        with heartbeat_while(conn, run_id, beat_s):
            reply = agent(target, "adjudicate",
                          babysitter.adjudication_brief(
                              pull, judged, ticket, sha,
                              babysitter.conventions(wt), run_id=run_id), wt, conn=conn,
                          base_sha=base_sha, candidate_sha=sha, run_id=run_id)
    verdicts = _verdicts_by_kind(
        threads, judged, babysitter.parse_verdicts(reply, len(judged)))
    record_round(target, conn, run_id, rnd, "review",
                 babysitter.round_reply(pull, pass_no, threads, verdicts,
                                      state.checks, sha),
                 None, True, "", started_at=round_started,
                 route=babysitter.route_of(threads),
                 structured_findings=_thread_findings(
                     pull, pass_no, threads, verdicts, state.checks, sha))
    people = sum(t.author_kind not in ("bot", "maintainer")
                 and t.classification != "MENTIONED" for t in threads)
    ledger(conn, run_id, task_id, "round",
           f"Babysit pass {pass_no} over {pull.url}: {len(threads)}"
           f" unresolved thread(s), checks {state.checks}\n"
           + (f"{people} opened by a person, HUMAN"
              " before the adjudicator was asked\n" if not act else
              f"{people} opened by a person, judged (human_threads = act):"
              " ADDRESS is fixed and"
              " answered, anything else is HUMAN\n")
           + f"Adjudicator verdicts:\n{reply}", provider)
    by_verdict = {v: [(n, t, verdicts[n][1]) for n, t in
                      enumerate(threads, 1) if verdicts[n][0] == v]
                  for v in babysitter.VERDICTS}
    # A HUMAN verdict on a bot's thread ends the pass before anything is
    # posted, under either setting -- bot handling does not move. Only a
    # person's HUMAN under `act` waits: the bots' threads and the
    # person's ADDRESSes are fixed and answered first; the pass then
    # parks with that thread quoted, unanswered, and any addressed one
    # listed as left open for them to close -- so the next pass does not
    # judge it again.
    if by_verdict["HUMAN"] and (not act or any(
            t.author_kind == "bot" for _, t, _ in by_verdict["HUMAN"])):
        _park_human(target, conn, run_id, provider, task_id, branch, sha, pull,
                    by_verdict["HUMAN"], threads, reviewed)
    if by_verdict["ADDRESS"]:
        sha = _fix_threads(target, conn, run_id, provider, task_id, branch,
                           wt, sha, beat_s, pull, by_verdict["ADDRESS"],
                           model, ticket, verify_cmd, contracts, budget_min,
                           pass_no,
                           review_follows=merge_config(target).approve == "auto")
    declined_open = _decline_threads(target, conn, run_id, beat_s, pull,
                                     by_verdict["DECLINE"], model)
    left_open = declined_open + tuple(
        t for _, t, _ in by_verdict["ADDRESS"]
        if t.author_kind != "bot" and not maintainer_notes.is_note(t)
        and t.classification != "MENTIONED")
    if by_verdict["HUMAN"]:
        _park_human(target, conn, run_id, provider, task_id, branch, sha, pull,
                    by_verdict["HUMAN"],
                    tuple(t for _, t, _ in by_verdict["HUMAN"]) + left_open,
                    reviewed)
    if left_open:
        declined = len(declined_open)
        answered = len(left_open) - declined
        why = [f"{declined} thread(s) declined and left open for their"
               " authors"] if declined else []
        why += [f"{answered} person's thread(s) addressed and left open for"
                " them to close"] if answered else []
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    "; ".join(why), left_open, reviewed=reviewed)
    return sha


def _decline_threads(target, conn, run_id, beat_s, pull, declined, model):
    """Reply before resolving bot declines; return the threads left open."""
    bot_authors = merge_config(target).bot_authors
    left_open = []
    for _, thread, reason in declined:
        resolve = thread.author.endswith("[bot]") or thread.author in bot_authors
        _post(target, conn, run_id, beat_s, pull, thread,
              babysitter.declined_reply(model, reason), resolve=resolve)
        if not resolve:
            left_open.append(thread)
    if declined:
        print(f"[holo2] {len(declined)} thread(s) declined;"
              f" {len(declined) - len(left_open)} from bots resolved with the reason")
    return tuple(left_open)


def _thread_findings(pull, pass_no, threads, verdicts, checks, sha):
    """Keep explicit instructions structured; ordinary findings retain their prose."""
    findings = []
    for n, thread in enumerate(threads, 1):
        if thread.classification == "MENTIONED":
            findings.append(dict(kind="instruction", path=thread.path or "(no file)",
                                 line=thread.line, author=thread.comments[-1].author,
                                 request=thread.request, url=thread.url,
                                 severity="nit", message=thread.request))
        else:
            reply = babysitter.round_reply(
                pull, pass_no, (thread,), {1: verdicts[n]}, checks, sha)
            # The terminal verdict describes the round, not this finding.
            findings.extend(parse_findings(reply.rsplit("\n", 1)[0]))
    return findings


def _verdicts_by_kind(threads, judged, parsed):
    """`{number: (verdict, reason)}` over `threads`, numbered as the
    round row lists them: a thread not in `judged` -- a person's under
    `human_threads = "park"` -- is `HUMAN`, "opened by a person"; a
    judged thread takes the next verdict off `parsed` in order, and one
    on a person's thread that is not `ADDRESS` folds to `HUMAN` -- the
    factory never declines a person."""
    pending = iter(sorted(parsed))
    verdicts = {}
    for n, t in enumerate(threads, 1):
        if maintainer_notes.is_note(t):
            verdicts[n] = ("ADDRESS",
                           f"operator_note event {maintainer_notes.event_id(t)}")
            continue
        if t.classification == "MENTIONED":
            verdicts[n] = ("ADDRESS", t.request)
            continue
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
                 contracts, budget_min, pass_no, *, review_follows, goal=None):
    """Fix, verify, push and answer threads; return the fixed candidate's sha."""
    from holophyte.loop import (
        _candidate_drift,
        _record_implementer_output,
        _transport_timed,
        sh,
    )
    from holophyte.redact import known_secrets
    maintainer_notes.start_fix(conn, run_id, addressed)
    fixes, timed_out = _transport_timed(target, conn, run_id, beat_s, wt, budget_min,
        goal or babysitter.fix_brief(pull, addressed, ticket))
    fixed = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if fixed == sha:
        _record_implementer_output(conn, run_id,
                                   f"fix round {pass_no}: {fixes}",
                                   known_secrets(target.config()))
    if timed_out or fixed == sha:
        raise RunFailure(f"fix round for {pull.url} timed out or made no"
                         f" progress; branch {branch} preserved at"
                         f" {sha[:12]}")
    # Verify vouches for the commit only if the tree matches it. Preserve
    # uncommitted work for a human without pushing or resolving threads.
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
    fixed = maintainer_notes.cite_commits(wt, sha, fixed, addressed, sh)
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts, conn=conn, run_id=run_id)
        ok, out = with_baseline(target, wt, verify_cmd, ok, out,
                               conn, run_id)
    if not ok or not review_follows:
        record_unreviewed_verification(conn, run_id, out)
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
    # Human review threads stay open unless explicitly addressed to the factory.
    for n, thread, reason in addressed:
        if maintainer_notes.is_note(thread):
            continue
        reply = babysitter.addressed_reply(model, summaries.get(n, reason), fixed)
        _post(target, conn, run_id, beat_s, pull, thread,
              reply,
              resolve=(thread.author_kind == "bot"
                       or thread.classification == "MENTIONED"))
    return fixed


def _post(target, conn, run_id, beat_s, pull, thread, body, resolve):
    """Reply and optionally resolve a review thread; record each landed call."""
    with heartbeat_while(conn, run_id, beat_s):
        if thread.kind == "conversation":
            pr.comment_on_pull(target, pull, f"{quote_request(thread)}\n\n{body}")
        else:
            pr.reply_thread(target, pull, thread.id, body)
        if thread.classification == "MENTIONED":
            record_instruction_reply(conn, run_id, thread.url, "changed", body)
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "pull_request",
                               f"replied on thread {thread.url}:"
                               f" {babysitter.gist(body.splitlines()[-1])}")
        if resolve and thread.kind != "conversation":
            pr.resolve_thread(target, pull, thread.id)
            if conn is not None and run_id is not None:
                store.record_event(conn, run_id, "pull_request",
                                   f"resolved thread {thread.url}")
