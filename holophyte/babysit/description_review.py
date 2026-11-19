import re

import store
from holophyte.board.projection import ledger
from holophyte.loop.gates import sh
from holophyte.review.briefs import main_merge_base
from holophyte.review.reply_parsing import (
    FINDING_CATEGORIES,
    SCOPE_LINE_RE,
    without_criteria,
    without_refuted,
)

LEAD = (r"^\s*(?:(?:[-*+]|\d+[.)])\s+)?(?:\*\*)?(?:[\[(]?\s*"
        r"(?:p[0-3]|nit|blocker|major|minor)\s*[\])]?\s*"
        r"[:\u2014-]?\s*)?(?:\*\*)?\s*")

DESCRIPTION_SUBJECT_RE = re.compile(
    r"(?!\s*>)" + LEAD
    + rf"(?:\[\s*(?:{'|'.join(FINDING_CATEGORIES)})\s*\]\s*(?:\*\*)?\s*)?"
    r'(?:(?:p[0-3]|nit|blocker|major|minor)\b)?[^\w(>\n]*[(\[]?[`*\s]*'
    r'path:\s*"PR description"', re.I)

PR_TEXT_REVIEW_KIND = "pr_text_review"

VERDICT_LINE_RE = re.compile(r"^\s*(?:[`*_]+\s*)?VERDICT:.*$", re.M)

HEADING_WORDS = (r"(?:(?:review|open|remaining|description)\s+)?"
                 r"(?:findings|blockers|issues|comments|notes|problems)"
                 r"|summary|required\s+changes|changes\s+requested")

HEADING_RE = re.compile(
    rf"^[ \t]*(?:#{{1,6}}[ \t]+)?(?:\*\*)?(?:{HEADING_WORDS})[ \t]*:?"
    r"(?:\*\*)?:?[ \t]*$", re.M | re.I)

TAG_BRIEF = ("List each blocker as its own item, and tag one about the pull"
             " request description rather than the code with"
             ' `path: "PR description"`.\n')


def description_findings(verdict, decision, unwitnessed):
    """The blockers, when every one of them is about the PR description."""
    if decision != "REQUEST_CHANGES" or unwitnessed:
        return []
    findings = _blocker_items(verdict)
    if findings and all(DESCRIPTION_SUBJECT_RE.match(item) for item in findings):
        return findings
    return []


def without_description(verdict):
    described = [line for line in _blocker_items(verdict)
                 if DESCRIPTION_SUBJECT_RE.match(line)]
    kept = [line for line in verdict.splitlines() if line.strip() not in described]
    return "\n".join(kept), described


def without_description_items(message):
    kept, described = without_description(message)
    if not described:
        return message
    return kept if _blocker_items(kept) else ""


def hold_for_pr_text(conn, run_id, described):
    if described and conn is not None:
        store.record_event(conn, run_id, PR_TEXT_REVIEW_KIND,
                           "\n".join(described), level="detail")


def pending_text_review(conn, run_id):
    if conn is None:
        return ""
    pending = []
    for kind, summary in conn.execute(
            "SELECT kind, summary FROM runEvents WHERE runId = ?"
            " AND kind IN (?, 'pr_text_sha') ORDER BY id",
            (run_id, PR_TEXT_REVIEW_KIND)):
        pending = [*pending, summary] if kind == PR_TEXT_REVIEW_KIND else []
    return "\n".join(pending)


def text_review(conn, run_id, findings):
    return "\n".join(filter(None, (pending_text_review(conn, run_id), *findings)))


def _blocker_items(verdict):
    reply = without_criteria(without_refuted(verdict))
    for pattern in (SCOPE_LINE_RE, VERDICT_LINE_RE, HEADING_RE):
        reply = pattern.sub("", reply)
    return [line.strip() for line in reply.splitlines() if line.strip()]


def rewrite_description(project, conn, run_id, provider, task_id, branch, wt,
                        sha, reviewed, beat_s, pull, ticket, budget_min, answered,
                        findings):
    from holophyte.pr.pullrequest import refresh_pr_text

    answered = fix_summary(answered, wt, reviewed, sha)
    print(f"[holo2] the review of the fix at {sha[:12]} faults only the pull"
          " request description; rewriting it")
    rewrote = refresh_pr_text(
        project, conn, run_id, task_id, ticket.splitlines()[0], branch, ticket,
        beat_s, wt, budget_min, pull, answered, sha=sha,
        review=text_review(conn, run_id, findings))
    if rewrote:
        ledger(conn, run_id, task_id, "round",
               f"Rewrote the description of {pull.url} for the review of the"
               f" fix at {sha}; reviewing the same commit once more", provider)
    return rewrote


def refreshed_or_held(project, conn, run_id, provider, task_id, branch, wt, sha,
                      beat_s, pull, ticket, budget_min, answered):
    from holophyte.pr.pullrequest import _park_on_pr, refresh_pr_text

    review = pending_text_review(conn, run_id)
    if refresh_pr_text(project, conn, run_id, task_id, ticket.splitlines()[0],
                       branch, ticket, beat_s, wt, budget_min, pull, answered,
                       sha=sha, review=review) or not review:
        return
    _park_on_pr(project, conn, run_id, provider, task_id, branch, sha, pull,
                "the fix is approved, but the PR-text writer refused to rewrite"
                " the description, so these review findings about it are"
                f" undelivered:\n{review}", ())


def fix_summary(answered, wt, reviewed, sha):
    answered = without_description(answered)[0].strip()
    if answered:
        return answered
    base = reviewed or main_merge_base(wt, sha)
    return sh(["git", "log", "--format=%s", f"{base}..{sha}"], cwd=wt)


def rereviewed(covering_review, review, outcome, fix_note, budget_min, answered):
    described = description_findings(*outcome[:3])
    if not described or not rewrite_description(
            *review[:12], budget_min, answered, described):
        return outcome, fix_note
    outcome = covering_review(*review)
    return outcome, None if description_findings(*outcome[:3]) else fix_note
