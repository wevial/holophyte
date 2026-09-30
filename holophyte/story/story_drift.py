"""A story child at the local merge gate after siblings merged since its claim."""
import json
from time import time

import store
from holophyte.agents.review_workspace import review_refs
from holophyte.board.projection import ledger
from holophyte.loop.gates import InfraFailure, RunFailure, sh
from holophyte.loop.runs import heartbeat_while, record_round
from holophyte.loop.stop import stop_if_requested
from holophyte.redact import safe_print as print
from holophyte.review.briefs import (
    _changed_files,
    criteria_brief,
    main_merge_base,
    scope_brief,
    scope_files,
    tests_brief,
)
from holophyte.review.reply_parsing import _review_reply, criteria_findings
from store.stories import advance_story, story

STORY_DRIFT_QUESTION = "story siblings merged since the claim and changed: "


def shared_files(conn, run_id, wt, sha):
    """The files main and `sha` both changed since their merge-base, recorded
    when the run's story moved past its claimed generation; else None."""
    if conn is None or run_id is None:
        return None
    ticket_id, claimed = conn.execute(
        "SELECT ticketId, storyGeneration FROM runs WHERE id = ?",
        (run_id,)).fetchone()
    current = story(conn, ticket_id)
    if claimed is None or current is None or current.generation == claimed:
        return None
    base = sh(["git", "merge-base", "main", sha], cwd=wt)
    shared = sorted(_changed_files(wt, base, "main")
                    & _changed_files(wt, base, sha))
    found = f"shared files: {', '.join(shared)}" if shared else "no shared files"
    note = (f"story generation {claimed} at the claim, {current.generation}"
            f" at the merge gate; {found}")
    store.record_event(conn, run_id, "story_drift", note)
    print(f"[holo2] {note}")
    return shared


def advance_generation(conn, run_id):
    """Count a landed merge in its story before the merge lock is let go."""
    if conn is not None and run_id is not None:
        advance_story(conn, run_id)


def refresh_scope(wt, reviewed, sha, files):
    """The covering review's scope: the sibling merge into the candidate."""
    tests = sorted(path for path in _changed_files(wt, reviewed, sha)
                   if path.startswith("tests/"))
    return ("Sibling tickets of the same story merged into main after this "
            f"candidate was approved at {reviewed}, changing files it changes"
            f" too ({files}); main was merged into it at {sha}. Review the "
            f"range {reviewed}..{sha}: whether the candidate's changes and the"
            " siblings' still fit together in those files and every criterion"
            " still holds. For a criterion this range leaves alone you may "
            f"cite `approval at {reviewed}; tests/file.py::TestClass::"
            "test_name`, unless its test file is among those changed in this"
            f" range: {json.dumps(tests)}.\n\n")


def review_refresh(project, conn, run_id, provider, task_id, branch, wt,
                   reviewed, sha, beat_s, ticket, verify_cmd, out, shared):
    """One covering review of `reviewed` refreshed to `sha`; else a park."""
    from holophyte.babysit.babysitter import _next_round
    from holophyte.loop.merge_gate import _park_at_gate
    from holophyte.loop.review_round import _verify_brief, agent

    snapshot = json.loads(store.run_contract(conn, run_id) or "{}")
    criteria = snapshot.get("acceptanceCriteria") or []
    files = ", ".join(shared)
    base_sha = main_merge_base(wt, sha)
    rnd = _next_round(conn, run_id)
    round_started = int(time() * 1000)
    scope = scope_files(wt, ticket, reviewed, sha, candidate_only=True)
    base_ref, candidate_ref = review_refs(run_id)
    with heartbeat_while(conn, run_id, beat_s):
        verdict, decision, first_reply = _review_reply(project,
            f"You are a READ-ONLY code reviewer. Review commit {sha} using "
            f"{base_ref} as the frozen base and {candidate_ref} as the "
            "candidate in this repo against the ticket below. "
            + refresh_scope(wt, reviewed, sha, files)
            + "The ticket is the contract, acceptance criteria included: a "
            "candidate that leaves a criterion unmet or unwitnessed is not "
            f"approvable.\n\n{ticket}\n\n"
            + _verify_brief(verify_cmd, True, out)
            + criteria_brief(criteria)
            + tests_brief(wt)
            + scope_brief(wt, ticket, reviewed, sha, candidate_only=True)
            + "Do not modify anything. End your reply with exactly one "
            "line:\n"
            "VERDICT: APPROVE  or  VERDICT: REQUEST_CHANGES\n"
            "If REQUEST_CHANGES, list only concrete blockers.", wt,
            base_sha, sha, conn, run_id, run_agent=agent, review_round=rnd)
    record_round(project, conn, run_id, rnd, "review", verdict, verify_cmd,
                 True, out, started_at=round_started, criteria=criteria,
                 root=wt, prior_reply=first_reply,
                 approved_range=(reviewed, sha), scope=scope)
    if decision == "MALFORMED":
        reason = "reviewer returned no verdict line twice"
        print(f"[holo2] round {rnd}: {reason}")
        raise InfraFailure(f"{reason}; candidate preserved at {sha}",
                           "review_route")
    stop_if_requested(conn, run_id, "merge_gate")
    unwitnessed = criteria_findings(verdict, criteria, wt,
                                    approved_range=(reviewed, sha), scope=scope)
    if decision == "APPROVE" and not unwitnessed:
        ledger(conn, run_id, task_id, "round",
               f"Round {rnd}: APPROVE of {branch} refreshed from {reviewed}"
               f" to {sha} over story siblings' changes to {files}\n"
               f"Reviewer verdict:\n{verdict}", provider)
        print(f"[holo2] the refreshed candidate at {sha[:12]} is approved")
        return
    verdict += "".join(f"\n\n{f['message']}" for f in unwitnessed)
    why = (f"the review of {branch} refreshed over story siblings' changes"
           f" to {files} asked for changes; branch preserved at {sha[:12]}")
    print(f"[holo2] {why}")
    _park_at_gate(conn, run_id, provider, task_id, branch, sha,
                  f"{STORY_DRIFT_QUESTION}{files}; the review of the"
                  f" refreshed candidate asked for changes:\n{verdict}",
                  f"MERGE REFUSED: round {rnd} of {branch} refreshed from"
                  f" {reviewed} to {sha} asked for changes.\n\n{verdict}\n")
    raise RunFailure(why)
