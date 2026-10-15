from holophyte.agents.fallback import failure_line
from holophyte.babysit.thread_text import VERDICT_LINE_RE


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
