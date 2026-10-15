import re

import store
import store.read
import ticket_template
from holophyte.babysit.check_failures import LISTED_MAX, failure_lines
from holophyte.board.projection import ledger
from holophyte.config.config_tables import merge_config
from holophyte.loop.runs import heartbeat_while
from holophyte.pr import pr_media
from holophyte.pr.evidence_refresh import publish_evidence
from holophyte.redact import safe_print as print

LOG_TAIL_LINES = 80


def capture_fix_brief(project, pull, ticket, task_id, sha, failure):
    failing = failure_lines(failure.log)[:LISTED_MAX]
    tail = "\n".join(failure.log.splitlines()[-LOG_TAIL_LINES:])
    fence = "`" * max(3, 1 + max(map(len, re.findall("`+", tail)), default=0))
    return (
        f"The UI capture for pull request {pull.url} failed at its head. The"
        " ticket you are held to, acceptance criteria included:\n\n"
        f"{ticket}\n\n"
        f"The UI capture at {sha[:12]} failed: {failure.sentence}\n"
        + "".join(f"Not captured: {view}\n" for view in failure.missing)
        + ("\nFailing tests and errors, read from the whole capture log:\n\n"
           + "".join(f"- {line}\n" for line in failing) if failing else "")
        + (f"\nThe last {LOG_TAIL_LINES} lines of the capture log:\n\n"
           f"{fence}\n{tail}\n{fence}\n" if tail else "")
        + pr_media.implementer_brief(project, ticket, task_id)
        + "\n\nFix the capture spec, or the UI it drives, so the capture saves"
        " every view; run the capture in the foreground until it exits, open"
        " each NN-slug.png, and commit. Keep the ticket's verify commands"
        " passing.")


def _rounds_run(conn, run_id):
    if conn is None or run_id is None:
        return 0
    return sum(event.kind == "capture_fix_round"
               for event in store.read.narrative_events(conn, run_id))


def _record_round(conn, run_id, task_id, pull, sha, failure, rounds):
    views = ", ".join(view.split()[0] for view in failure.missing)
    sentence = " ".join(failure.sentence.split())
    if conn is not None and run_id is not None:
        store.record_event(conn, run_id, "capture_fix_round",
                           f"capture fix round {rounds[0]} of {rounds[1]} at"
                           f" {sha[:12]}: {sentence}"
                           + (f"; not captured: {views}" if views else ""))
    ledger(conn, run_id, task_id, "round",
           f"Capture fix round {rounds[0]} of {rounds[1]} on {pull.url}: the UI"
           f" capture at {sha[:12]} failed", None)


def fix_capture(project, conn, run_id, provider, task_id, branch, wt, sha,
                reviewed, beat_s, pull, ticket, verify_cmd, contracts, budget_min,
                notes):
    """The head the covering review reads: the fix's head after a capture
    fix round, else `sha`."""
    from holophyte.babysit.babysitter import _fix_threads, _next_round
    from holophyte.pr.pullrequest import _park_on_pr
    failure = publish_evidence(project, conn, run_id, task_id, wt, pull, ticket,
                               sha, beat_s)
    merge = merge_config(project)
    cap = merge.capture_fix_rounds
    number = _rounds_run(conn, run_id) + 1
    if (not merge.ui_capture_local or failure is None
            or failure.kind != "capture" or number > cap):
        return sha
    _record_round(conn, run_id, task_id, pull, sha, failure, (number, cap))
    print(f"[holo2] the UI capture at {sha[:12]} failed; capture fix round"
          f" {number} of {cap}")
    fixed = _fix_threads(project, conn, run_id, provider, task_id, branch, wt,
                         sha, beat_s, pull, (), None, ticket, verify_cmd,
                         contracts, budget_min, _next_round(conn, run_id),
                         review_follows=True, reviewed=reviewed,
                         goal=capture_fix_brief(project, pull, ticket, task_id,
                                                sha, failure),
                         reviewed_by_caller=True, notes=notes)
    states = ticket_template.parse(ticket).evidence_states
    with heartbeat_while(conn, run_id, beat_s):
        pr_media.prepare(project, wt, task_id, evidence_states=states)
    again = publish_evidence(project, conn, run_id, task_id, wt, pull, ticket,
                             fixed, beat_s)
    if again is not None and again.kind != "infra":
        _park_on_pr(project, conn, run_id, provider, task_id, branch, fixed, pull,
                    f"the UI capture at {fixed[:12]} still fails after capture"
                    f" fix round {number} of {cap}:"
                    f" {' '.join(again.sentence.split())}; not merged", (),
                    reviewed=reviewed)
    return fixed
