"""The babysit pass over a pull request, and the texts it reads and writes.

Design note 7's second half. `_babysit()` fetches a pull request's
unresolved threads and drives the turns; the shapes in this module are
what it hands them and what it reads back, with nothing in them that
talks to GitHub, the store or an agent, so every one is testable on its
own and the pass reads as the sequence Ko runs by hand: threads in,
verdicts out, fixes, replies. What the pass calls back into the loop
for -- the merge gate, the timed turn, the drift check, the pull-request
stage's parks and merge -- is imported inside the functions that use it,
the same in-function import `holophyte.pullrequest` uses for the loop,
so this module's import edge stays one-way. The moved bodies still name
the texts as `babysitter.<name>`; the module's self-import below keeps
those lines verbatim.

Three verdicts, one per thread, from the adjudicator role:

* `ADDRESS` -- a concrete defect; the fix round takes it, the reply names
  the sha, the thread is resolved.
* `DECLINE` -- asks for nothing specific, or for what the ticket puts out
  of scope; the reply says why and the thread is left open for the
  reviewer to close. A thread naming an existing function, helper or
  constant the diff re-implements is a concrete change request, not a
  preference: the fix is reuse, and the repository's `AGENTS.md` or
  `CLAUDE.md` conventions are the reviewer's standard.
* `HUMAN` -- a genuine question, a reject, or anything the adjudicator
  will not answer for the operator: no reply is posted, the run parks and
  the ticket's question quotes the thread. A thread the reply gives no
  verdict for is `HUMAN` too: silence is not a licence to answer.

A thread a person opened is judged only under `[merge] human_threads =
"act"`; then its verdicts are `ADDRESS` or `HUMAN`, never `DECLINE` --
the factory does what a person asked and says so, or hands the thread to
the operator -- and an addressed thread is left unresolved for its
author to close.

Every reply the pass posts opens with `---- Comment by MODEL ----`, so a
reader of the PR can tell the factory's comments from a person's.
"""
import re
import subprocess
from time import time

import review_runner
import store
import store.read
from holophyte import babysitter, pr, pr_status
from holophyte.agents import agent_route
from holophyte.board import ledger
from holophyte.config_tables import merge_config
from holophyte.gates import InfraFailure, RunFailure, run_verify
from holophyte.pr import NO_AUTHOR
from holophyte.review import criteria_brief, criteria_findings
from holophyte.runs import heartbeat_while, record_round

# The repository's conventions files, in the order the brief quotes them,
# and the most of each the brief carries: the rule they back is one
# sentence, the excerpt is there so "the repository asks for DRY" is read
# from the file, not guessed.
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
    """`path:line` for a thread, or the path alone, or `(no file)`."""
    if not thread.path:
        return "(no file)"
    return f"{thread.path}:{thread.line}" if thread.line else thread.path


def conversation(thread):
    """A thread's text as the adjudicator and implementer read it: the
    opening comment, then each follow-up under a line naming who wrote it
    -- a later rejection or question is judged, not the opener alone."""
    parts = [thread.body.strip()]
    parts.extend(f"@{c.author} replied:\n{c.body.strip()}"
                 for c in thread.replies)
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


def adjudication_brief(pull, threads, ticket, sha, conventions=()):
    """The adjudicator's goal: the numbered threads, the verdicts to give
    each, and the repository's conventions when it has any."""
    listing = "\n\n".join(
        f"THREAD {n} -- {where(t)} by @{t.author}"
        + (" (outdated: the lines it was left on have changed)"
           if t.outdated else "")
        + (f" ({len(t.replies)} follow-up(s))" if t.replies else "")
        + f"\n{conversation(t)}"
        for n, t in enumerate(threads, 1))
    return (
        f"You are a READ-ONLY adjudicator of the review threads on pull "
        f"request {pull.url}. Judge commit {sha} using refs/review/base as "
        "the frozen base and refs/review/candidate as the candidate in this "
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
        "thread by its whole conversation: a follow-up can withdraw, "
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
        f"Adjudicator: {reason}"
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
    """The reply on a declined thread: the header and the reason; it stays
    open for its author to close."""
    return (f"{COMMENT_HEADER.format(model=model)}\n\n"
            f"Declined: {reason}\n\nLeaving this thread open.")


def round_reply(pull, pass_no, threads, verdicts, checks, sha):
    """The text a pass is recorded as, in `record_round()`'s shape: one
    bullet per thread citing its file and verdict, and a closing
    `VERDICT:` -- `APPROVE` when no thread was found."""
    lines = [f"Babysit pass {pass_no} over {pull.url} at {sha[:12]}:"
             f" {len(threads)} unresolved thread(s), checks {checks}."]
    lines += [f"- {where(t)} @{t.author}: {gist(t.body)}"
              f" -- {verdicts[n][0]}: {verdicts[n][1]}"
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
                       sha, beat_s, pull, budget_min, reviewed=None, refusal=None):
    """On CONFLICTING or a conflict refusal, fetch and merge `origin/main`
    into the worktree's branch -- the remote's `main`, never the possibly
    stale local one -- push, and hand back the branch's sha so the pass
    waits on the restarted checks. A merge commit, never a rebase or a
    force-push: review threads keep their lines.

    A merge that stops on unmerged paths goes to one implementer turn,
    which resolves and commits it (`merge_conflict_goal()`); a turn that
    leaves it unresolved -- or dropped it without merging -- aborts the
    merge and parks with the paths and optional `refusal` text in the
    question, the branch left at `sha`. A fetch without `origin/main`
    is the route's failure, not the ticket's.
    """
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
    return merged


def _babysit(target, conn, run_id, provider, task_id, issue_id, task, branch,
              wt, sha, beat_s, url, ticket, verify_cmd, contracts, budget_min,
              criteria=(), approved=False, reviewed=None, verified=None):
    """Watch the PR until it merges or parks, bounded by `[merge] pr_rounds`.

    Each pass records a review round; `_answer_threads()` handles threads.
    Green, quiet PRs merge under auto approval or `--approve`, else park.

    `reviewed` is the sha independently approved, from the fresh review or
    the resumed park's `approvedSha`. A changed candidate needs `_review_fix()`
    before auto-merge; human approval covers only the released candidate.
    `verified` is the sha verified here, None on resume; changes pass verify
    and drift gates. `criteria` holds acceptance criteria for the fix review.

    CONFLICTING polls and HTTP 405 merge-conflict refusals both merge
    `origin/main` into the task branch with `_merge_origin_main()` and push.
    Auto retries restart the round; unresolved implementer conflicts park.

    `_park_on_pr()` records the question, PR URL, candidate and reviewed sha,
    then raises `MergeParked`, preserving work; exhausted rounds park too.
    Externally merged PRs return their sha; closed ones fail. Main is untouched."""
    from holophyte.pullrequest import _park_on_pr
    merge = merge_config(target)
    pull = pr_status.parse_pr_url(url)
    if pull is None:
        raise RunFailure(f"cannot read a pull request off {url!r};"
                         f" branch {branch} preserved at {sha[:12]}")
    model = agent_route(target, "adjudicate")
    # `reviewed`: the sha an independent judgement covers -- the reviewer's
    # approval or the operator's release. A fix round moves `sha` past it.
    for pass_no in range(1, merge.pr_rounds + 1):
        state = _settled_state(target, conn, run_id, beat_s, pull)
        done = _pr_terminal(target, conn, run_id, provider, task_id, branch,
                            sha, pull, state, reviewed)
        if done is not None:
            return done
        if state.mergeable == "CONFLICTING":
            # GitHub found the pull request unmergeable: bring
            # `origin/main` into the branch (fetch, merge, push) and let
            # the next pass wait on the restarted checks, before any
            # thread is judged. UNKNOWN is not a conflict -- GitHub
            # computes `mergeable` lazily and a later pass sees it.
            sha = _merge_origin_main(target, conn, run_id, provider,
                                     task_id, branch, wt, sha, beat_s, pull,
                                     budget_min, reviewed=reviewed)
            continue
        rnd = len(store.read.rounds_of(conn, run_id)) + 1 if conn else pass_no
        if state.threads:
            sha = _answer_threads(target, conn, run_id, provider, task_id,
                                  branch, wt, sha, beat_s, pull, state, rnd,
                                  pass_no, model, ticket, verify_cmd,
                                  contracts, budget_min, reviewed=reviewed)
            continue
        if state.checks == "success" and _quiet_left(
                state, merge.pr_quiet_sec * 1000):
            continue  # still not quiet; the next pass waits again
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
            if merge.approve != "auto":
                _park_on_pr(target, conn, run_id, provider, task_id, branch, sha,
                            pull, f"{_moved(sha, reviewed)}, and a human"
                            " says merge on the candidate as it stands"
                            " ([merge] approve = \"human\")", (),
                            reviewed=reviewed)
            _review_fix(target, conn, run_id, provider, task_id, branch, wt,
                        sha, reviewed, beat_s, pull, ticket, verify_cmd,
                        contracts, criteria)
            # The review vouches for the fix, not the gate: `verified`
            # stays behind, so the fixed candidate goes through
            # `_merge_gate()` below -- drift check and verify both --
            # before the merge API is called.
            reviewed = sha
        if merge.approve == "auto" or approved:
            try:
                return _verified_merge(target, conn, run_id, provider, task_id,
                                       issue_id, branch, wt, sha, beat_s, pull,
                                       reviewed, verified, verify_cmd, contracts,
                                       ticket, budget_min, merge.approve == "auto")
            except pr.MergeRefused as refused:
                verified = sha
                sha = _merge_origin_main(target, conn, run_id, provider,
                                         task_id, branch, wt, sha, beat_s, pull,
                                         budget_min, reviewed=reviewed,
                                         refusal=refused)
                continue
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    "ready to merge; waiting for a human to say merge"
                    " ([merge] approve = \"human\")", (), reviewed=reviewed)
    state = _settled_state(target, conn, run_id, beat_s, pull)
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


def _pr_terminal(target, conn, run_id, provider, task_id, branch, sha,
                 pull, state, reviewed):
    """Handle a terminal PR or park a head that differs from the candidate."""
    from holophyte.pullrequest import _park_on_pr
    if state.merged:
        print(f"[holo2] {pull.url} is already merged as"
              f" {(state.merge_sha or '?')[:12]}")
        return state.merge_sha
    if state.closed:
        from holophyte.board import release_lease_label
        from holophyte.gates import MergeParked
        from holophyte.reconcile import _reject_pr
        if conn is not None and run_id is not None:
            _reject_pr(conn, run_id, pull, state.closed_by, branch, sha)
            ticket_id = store.read.run_snapshot(conn, run_id).ticketId
            release_lease_label(target, conn, ticket_id, provider, run_id)
        raise MergeParked(f"rejected: {pull.url} closed by"
                          f" {state.closed_by or 'unknown'}")
    if state.head_sha and state.head_sha != sha:
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha,
                    pull,
                    f"the pull request's head is {state.head_sha[:12]},"
                    f" not the candidate {sha[:12]} this run pushed;"
                    " someone else pushed to the branch, and the"
                    " babysitter does not judge or merge their commit",
                    state.threads, reviewed=reviewed)
    return None


def _moved(sha, reviewed):
    """Why the candidate at `sha` needs an independent look: it sits past
    the sha the last judgement covered, or nothing on record covers it."""
    if reviewed is None:
        return (f"no approval on record covers the candidate at {sha[:12]}"
                " (the last review asked for changes, or the park recorded"
                " none)")
    return (f"the fix rounds moved the candidate from {reviewed[:12]} to"
            f" {sha[:12]}; the release covered {reviewed[:12]}")


def _review_fix(target, conn, run_id, provider, task_id, branch, wt, sha,
                reviewed, beat_s, pull, ticket, verify_cmd, contracts,
                criteria=()):
    """The independent review of a candidate the babysitter's fix rounds
    moved from `reviewed` to `sha` (None: nothing on record covers it),
    before the merge API is called.

    The fix commits are the implementer's answer to the PR's threads; the
    reviewer's approval and the adjudicator's verdicts both came before
    them, so nothing independent has judged the candidate as it stands.
    The same reviewer route and brief as a review round: verify first,
    then a read-only review of the candidate at `sha` over the frozen
    `refs/review/*` pair, recorded as a `reviewRounds` row and held to
    the ticket's `criteria`: an approval that leaves a criterion unmet,
    unwitnessed, or witnessed by a test the worktree does not hold is a
    `REQUEST_CHANGES` whatever its verdict line says. Anything but an
    approval parks the run on the PR with the findings in the ticket's
    question -- there is no further fix round here; the operator reads
    the findings and answers with `--babysit` or by hand."""
    from holophyte.loop import _verify_brief, agent, set_phase, sh
    from holophyte.pullrequest import _park_on_pr
    set_phase(conn, run_id, "verifying", f"verify the fix at {sha[:12]}"
              " before its review")
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
    if not ok:
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
            "refs/review/base as the frozen base and refs/review/candidate "
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
    if not unwitnessed and review_runner.terminal_verdict(verdict) == "APPROVE":
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: APPROVE of the fix at {sha} on {pull.url}\n"
               f"Reviewer verdict:\n{verdict}", provider)
        print(f"[holo2] the fix at {sha[:12]} is approved")
        return
    ledger(conn, run_id, task_id, "round",
           f"Round {rnd}: REQUEST_CHANGES on the fix at {sha} on"
           f" {pull.url}; not merged\nReviewer findings:\n{verdict}",
           provider)
    # No `reviewed`: the judgement on record is this rejection, so the
    # resume that follows reviews the candidate again before any merge.
    _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                f"the review of the fix at {sha[:12]} asked for changes;"
                f" not merged. Reviewer findings:\n{verdict}", ())


def _next_round(conn, run_id):
    """The number the run's next `reviewRounds` row takes; 1 with no store."""
    return len(store.read.rounds_of(conn, run_id)) + 1 if conn else 1


def _quiet_left(state, quiet_ms):
    """Milliseconds of `quiet_ms` still ahead of `state` -- `updatedAt`
    moves on every comment, review, push and check; none waits in full."""
    if state.updated_at is None:
        return quiet_ms
    return max(0, quiet_ms - (int(time() * 1000) - state.updated_at))


def _settled_state(target, conn, run_id, beat_s, pull):
    """One read of the PR, re-read while its checks are pending and it has
    no thread to answer -- every `pr.CHECK_POLL_S`, for at most
    `pr.CHECK_WAIT_S` -- and, since KO-429, while it is green and
    thread-free but younger than `[merge] pr_quiet_sec` -- every
    `pr_poll_sec`, so a merge lands only once the pull request has been
    quiet that long -- under the heartbeat. Threads are answered without
    waiting: the fix they call for restarts the checks anyway."""
    merge = merge_config(target)
    quiet_ms = merge.pr_quiet_sec * 1000
    wait_s = max(pr.CHECK_WAIT_S, quiet_ms // 1000)
    waited = 0
    with heartbeat_while(conn, run_id, beat_s):
        state = pr_status.pr_state(target, pull)
        while not state.threads and not state.merged and waited < wait_s:
            if state.checks == "pending":
                nap = pr.CHECK_POLL_S
                print(f"[holo2] checks pending on {pull.url}; waiting"
                      f" {nap}s")
            elif state.checks == "success" \
                    and (left := _quiet_left(state, quiet_ms)):
                nap = merge.pr_poll_sec
                print(f"[holo2] {pull.url} is green and quiet for"
                      f" {(quiet_ms - left) // 1000}s of the"
                      f" {quiet_ms // 1000}s required; waiting {nap}s")
            else:
                break
            pr.SLEEP(nap)
            waited += nap
            state = pr_status.pr_state(target, pull)
    return state


def _answer_threads(target, conn, run_id, provider, task_id, branch, wt, sha,
                    beat_s, pull, state, rnd, pass_no, model, ticket,
                    verify_cmd, contracts, budget_min, reviewed=None):
    """One pass over the PR's unresolved threads; return the candidate's
    sha after the fix round, or park.

    The adjudicator judges every thread against the ticket and the
    candidate (the same frozen `refs/review/*` pair a review round gets)
    and answers `ADDRESS`, `DECLINE` or `HUMAN` per thread; the pass is
    recorded as a round before anything is posted, so an interrupted
    pass has its row. A `HUMAN` verdict ends the pass with nothing
    posted: the run parks and the ticket's question quotes the thread.
    Otherwise the addressed threads go to one fix round (`_timed()`, the
    implementer), the verify commands run over the fix, the branch is
    pushed, and each addressed thread gets a reply naming the change and
    the sha and is resolved; each declined thread gets a reply with the
    reason and stays open. Declines park the run with those threads
    listed -- they are the reviewer's to close.

    Under `[merge] human_threads = "act"` a person's thread is judged
    too: an `ADDRESS` on it is fixed and answered like a bot's but never
    resolved, and any other verdict folds to `HUMAN`. A person's `HUMAN`
    parks after the fix round then, so a bot's defect is not held up by
    a person's question, and a pass that answered a person parks with
    their thread listed as left open for them to close. A bot's `HUMAN`
    still ends the pass before anything is posted.
    """
    from holophyte.loop import agent, sh
    from holophyte.pullrequest import _park_human, _park_on_pr
    threads = state.threads
    base_sha = sh(["git", "merge-base", "main", sha], cwd=wt)
    round_started = int(time() * 1000)
    # Under `human_threads = "park"` a thread a person opened is the
    # operator's whatever it says: HUMAN before the adjudicator is asked,
    # which sees the bots' threads alone, renumbered so its reply and
    # `parse_verdicts()` agree. Under `"act"` a person's threads are
    # judged too, but only an ADDRESS stands: anything else folds to
    # HUMAN -- a person is never declined. A deleted account reads as a
    # person: silence is the safe side.
    act = merge_config(target).human_threads == "act"
    judged = tuple(t for t in threads if act or t.author_kind == "bot")
    reply = "(no bot opened a thread; the adjudicator was not asked)"
    if judged:
        with heartbeat_while(conn, run_id, beat_s):
            reply = agent(target, "adjudicate",
                          babysitter.adjudication_brief(
                              pull, judged, ticket, sha,
                              babysitter.conventions(wt)), wt, conn=conn,
                          base_sha=base_sha, candidate_sha=sha, run_id=run_id)
    verdicts = _verdicts_by_kind(
        threads, judged, babysitter.parse_verdicts(reply, len(judged)))
    record_round(target, conn, run_id, rnd, "review",
                 babysitter.round_reply(pull, pass_no, threads, verdicts,
                                      state.checks, sha),
                 None, True, "", started_at=round_started,
                 route=babysitter.route_of(threads))
    ledger(conn, run_id, task_id, "round",
           f"Babysit pass {pass_no} over {pull.url}: {len(threads)}"
           f" unresolved thread(s), checks {state.checks}\n"
           + (f"{len(threads) - len(judged)} opened by a person, HUMAN"
              " before the adjudicator was asked\n" if not act else
              f"{sum(t.author_kind != 'bot' for t in threads)} opened by a"
              " person, judged (human_threads = act): ADDRESS is fixed and"
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
                           pass_no)
    for _, thread, reason in by_verdict["DECLINE"]:
        _post(target, conn, run_id, beat_s, pull, thread,
              babysitter.declined_reply(model, reason), resolve=False)
    left_open = tuple(t for _, t, _ in by_verdict["DECLINE"]) + tuple(
        t for _, t, _ in by_verdict["ADDRESS"] if t.author_kind != "bot")
    if by_verdict["HUMAN"]:
        _park_human(target, conn, run_id, provider, task_id, branch, sha, pull,
                    by_verdict["HUMAN"],
                    tuple(t for _, t, _ in by_verdict["HUMAN"]) + left_open,
                    reviewed)
    if left_open:
        declined = len(by_verdict["DECLINE"])
        answered = len(left_open) - declined
        why = [f"{declined} thread(s) declined and left open for their"
               " authors"] if declined else []
        why += [f"{answered} person's thread(s) addressed and left open for"
                " them to close"] if answered else []
        _park_on_pr(target, conn, run_id, provider, task_id, branch, sha, pull,
                    "; ".join(why), left_open, reviewed=reviewed)
    return sha


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
                 contracts, budget_min, pass_no):
    """The fix round for the addressed threads, the push, then a reply on
    each and a resolve on each bot's; the fixed candidate's sha."""
    from holophyte.loop import (
        _candidate_drift,
        _record_implementer_output,
        _transport_timed,
        sh,
    )
    from holophyte.redact import known_secrets
    fixes, timed_out = _transport_timed(target, conn, run_id, beat_s, wt, budget_min,
        babysitter.fix_brief(pull, addressed, ticket))
    fixed = sh(["git", "rev-parse", "HEAD"], cwd=wt)
    if fixed == sha:
        _record_implementer_output(conn, run_id,
                                   f"fix round {pass_no}: {fixes}",
                                   known_secrets(target.config()))
    if timed_out or fixed == sha:
        raise RunFailure(f"fix round for {pull.url} timed out or made no"
                         f" progress; branch {branch} preserved at"
                         f" {sha[:12]}")
    # The verify runs over the working tree, so it vouches for the
    # commit only when the tree is that commit: a fix half committed and
    # half left in the tree would verify green and push a commit that
    # does not hold it -- and resolve the thread on it. The tree is left
    # for a human; nothing is committed, deleted or pushed.
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
    with heartbeat_while(conn, run_id, beat_s):
        ok, out = run_verify(verify_cmd, wt, contracts)
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
    # A person's thread is theirs to close: the reply names the fix and
    # the sha, and the thread is left unresolved for its author.
    for n, thread, reason in addressed:
        _post(target, conn, run_id, beat_s, pull, thread,
              babysitter.addressed_reply(model, summaries.get(n, reason),
                                       fixed),
              resolve=thread.author_kind == "bot")
    return fixed


def _post(target, conn, run_id, beat_s, pull, thread, body, resolve):
    """Reply `body` on `thread`, resolving it when `resolve`; each landed
    call is a `runEvents` row for an interrupted pass to read back."""
    with heartbeat_while(conn, run_id, beat_s):
        pr.reply_thread(target, pull, thread.id, body)
        if conn is not None and run_id is not None:
            store.record_event(conn, run_id, "pull_request",
                               f"replied on thread {thread.url}:"
                               f" {babysitter.gist(body.splitlines()[-1])}")
        if resolve:
            pr.resolve_thread(target, pull, thread.id)
            if conn is not None and run_id is not None:
                store.record_event(conn, run_id, "pull_request",
                                   f"resolved thread {thread.url}")
