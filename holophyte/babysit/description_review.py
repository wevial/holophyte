import re

from holophyte.board.projection import ledger
from holophyte.loop.gates import sh
from holophyte.review.briefs import main_merge_base
from holophyte.review.reply_parsing import (
    CRITERION_LINE_RE,
    SCOPE_LINE_RE,
    finding_blocks,
    without_refuted,
)

DESCRIPTION_SUBJECT_RE = re.compile(
    r'^\s*(?:(?:[-*+]|\d+[.)])\s+)?(?:\*\*)?(?:(?:p[0-3]|nit|blocker|major|minor)\b'
    r'[^\w(\n]*)?[(\[]?\s*path:\s*"?(?:the\s+)?PR description\b', re.I)

FINDING_OPEN_RE = re.compile(
    r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?(?:\*\*)?[(\[]?\s*(?:(?:p[0-3]|nit|blocker|major|minor)\b"
    r"|[`*_\[(]*(?:path:\s*\"?)?[`\[]*[\w./-]+\.\w+(?::\d|#L\d|`|\]\())", re.I)

VERDICT_LINE_RE = re.compile(r"^\s*VERDICT:.*$", re.M)

HEADING_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+[\w ]{1,40}:?|\*\*[\w ]{1,40}:?\*\*:?"
    r"|\w[\w ]{0,39}:)[ \t]*$", re.M)

TAG_BRIEF = ("List each blocker as its own item, and tag one about the pull"
             " request description rather than the code with"
             ' `path: "PR description"`.\n')


def description_findings(verdict, decision, unwitnessed):
    """The blockers, when every one of them is about the PR description."""
    if decision != "REQUEST_CHANGES" or unwitnessed:
        return []
    reply = without_refuted(verdict)
    for pattern in (CRITERION_LINE_RE, SCOPE_LINE_RE, VERDICT_LINE_RE, HEADING_RE):
        reply = pattern.sub("", reply)
    blocks = [block.strip() for block in finding_blocks(reply) if block.strip()]
    if blocks and all(_one_description_finding(block) for block in blocks):
        return blocks
    return []


def _one_description_finding(block):
    first, *rest = block.splitlines()
    return bool(DESCRIPTION_SUBJECT_RE.match(first)) and all(
        line[:1].isspace() and (not FINDING_OPEN_RE.match(line)
                                or DESCRIPTION_SUBJECT_RE.match(line))
        for line in rest)


def rewrite_description(project, conn, run_id, provider, task_id, branch, wt,
                        sha, reviewed, beat_s, pull, ticket, budget_min, answered,
                        findings):
    from holophyte.pr.pullrequest import refresh_pr_text

    if not answered:
        base = reviewed or main_merge_base(wt, sha)
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
