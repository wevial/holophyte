from holophyte.agents.fallback import failure_line
from holophyte.babysit.thread_text import VERDICT_LINE_RE
from holophyte.loop.runs import heartbeat_while
from holophyte.loop.stop import stop_if_requested


def outage_line(reply, count):
    failed = failure_line(reply)
    if failed:
        return failed
    if any(1 <= int(m.group(1)) <= count
           for m in VERDICT_LINE_RE.finditer(reply)):
        return None
    return "no verdict for any thread"


def park_outage(project, conn, run_id, provider, task_id, branch, sha, pull,
                line, threads, reviewed):
    from holophyte.pr.pullrequest import _park_on_pr
    _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                f"the adjudicator turn failed: {line}; no thread was judged"
                " and nothing was posted", threads, reviewed=reviewed,
                park_kind="route")


def adjudicate_or_park(project, conn, run_id, provider, task_id, branch, wt,
                       sha, beat_s, pull, judged, ticket, base_sha, reviewed):
    from holophyte.babysit import babysitter
    from holophyte.loop.review_round import agent
    reply = "(no bot opened a thread; the adjudicator was not asked)"
    if judged:
        with heartbeat_while(conn, run_id, beat_s):
            reply = agent(project, "adjudicate",
                          babysitter.adjudication_brief(
                              pull, judged, ticket, sha,
                              babysitter.conventions(wt), run_id=run_id), wt, conn=conn,
                          base_sha=base_sha, candidate_sha=sha, run_id=run_id)
    stop_if_requested(conn, run_id, "merge_gate")
    outage = judged and outage_line(reply, len(judged))
    if outage:
        park_outage(project, conn, run_id, provider, task_id, branch, sha, pull,
                    outage, judged, reviewed)
    return reply
