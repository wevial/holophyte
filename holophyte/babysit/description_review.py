import re

from holophyte.board.projection import ledger
from holophyte.loop.gates import sh
from holophyte.review.reply_parsing import (
    CRITERION_LINE_RE,
    SCOPE_LINE_RE,
    finding_blocks,
    without_refuted,
)

DESCRIPTION_TAG = 'path: "PR description"'

DESCRIPTION_TAG_RE = re.compile(r'path:\s*"?(?:the\s+)?PR description\b', re.I)

VERDICT_LINE_RE = re.compile(r"^\s*VERDICT:.*$", re.M)

TAG_BRIEF = ("Tag a blocker about the pull request description rather than"
             f" the code with `{DESCRIPTION_TAG}`.\n")


def description_findings(verdict, decision, unwitnessed):
    """The blockers, when every one of them is about the PR description."""
    if decision != "REQUEST_CHANGES" or unwitnessed:
        return []
    reply = without_refuted(verdict)
    for pattern in (CRITERION_LINE_RE, SCOPE_LINE_RE, VERDICT_LINE_RE):
        reply = pattern.sub("", reply)
    blocks = [block.strip() for block in finding_blocks(reply) if block.strip()]
    if blocks and all(DESCRIPTION_TAG_RE.search(block) for block in blocks):
        return blocks
    return []


def rewrite_description(project, conn, run_id, provider, task_id, branch, wt,
                        sha, base, beat_s, pull, ticket, budget_min, answered,
                        findings):
    from holophyte.pr.pullrequest import refresh_pr_text

    if not answered:
        answered = sh(["git", "log", "--format=%s", f"{base}..{sha}"], cwd=wt)
    print(f"[holo2] the review of the fix at {sha[:12]} faults only the pull"
          " request description; rewriting it")
    rewrote = refresh_pr_text(
        project, conn, run_id, task_id, ticket.splitlines()[0], branch, ticket,
        beat_s, wt, budget_min, pull, answered, sha=sha,
        review="\n".join(findings))
    if rewrote:
        ledger(conn, run_id, task_id, "round",
               f"Rewrote the description of {pull.url} for the review of the"
               f" fix at {sha}; reviewing the same commit once more", provider)
    return rewrote
