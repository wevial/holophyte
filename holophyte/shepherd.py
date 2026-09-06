"""The shepherd pass's texts: what the adjudicator is asked, how its answer
is read, what the replies and the round say.

Design note 7's second half, the half that is prose rather than calls. The
loop (`holophyte.loop._shepherd`) fetches a pull request's unresolved
threads and drives the turns; this module is what it hands them and what it
reads back, with nothing in it that talks to GitHub, the store or an agent,
so every shape here is testable on its own and the pass in the loop reads as
the sequence Ko runs by hand: threads in, verdicts out, fixes, replies.

Three verdicts, one per thread, from the adjudicator role:

* `ADDRESS` -- a concrete defect; the fix round takes it, the reply names
  the sha, the thread is resolved.
* `DECLINE` -- not a defect, or out of the ticket's scope; the reply says
  why and the thread is left open for the reviewer to close.
* `HUMAN` -- a genuine question, a reject, or anything the adjudicator will
  not answer for the operator: no reply is posted, the run parks and the
  ticket's question quotes the thread. A thread the reply gives no verdict
  for is `HUMAN` too: silence is not a licence to answer.

Every reply the loop posts opens with `---- Comment by MODEL ----`, the
model being the adjudicator's route, so a reader of the PR can tell the
factory's comments from a person's at a glance.
"""
import re

from holophyte.pr import NO_AUTHOR

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
    """A thread's text as the adjudicator and the implementer read it: the
    opening comment, then each follow-up under a line naming who wrote
    it, so a later rejection or question is judged, not the opener alone."""
    parts = [thread.body.strip()]
    parts.extend(f"@{c.author} replied:\n{c.body.strip()}"
                 for c in thread.replies)
    return "\n\n".join(parts)


def thread_line(number, thread):
    """One thread as one line: its number, where it is, who opened it, and
    the gist of what it says."""
    return f"{number}. {where(thread)} (@{thread.author}): {gist(thread.body)}"


def adjudication_brief(pull, threads, ticket, sha):
    """The adjudicator's goal: the threads, numbered, and the three verdicts
    to give each one."""
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
        "For EACH thread give exactly one verdict line, in this form and "
        "nothing else on the line:\n"
        "THREAD n: ADDRESS -- one sentence naming the defect to fix\n"
        "THREAD n: DECLINE -- one sentence saying why it is not a defect or "
        "not in scope\n"
        "THREAD n: HUMAN -- one sentence saying why a person must answer\n"
        "ADDRESS is for a concrete defect in the candidate. DECLINE is for a "
        "style preference, a duplicate, or a request beyond the ticket. "
        "HUMAN is for a genuine question, a rejection of the approach, or "
        "anything you would not answer on the operator's behalf. Judge each "
        "thread by its whole conversation: a follow-up can withdraw, "
        "sharpen, or turn a finding into a question. Do not modify "
        "anything.")


def parse_verdicts(reply, count):
    """`{number: (verdict, reason)}` for threads 1..`count` off the
    adjudicator's reply; a thread with no verdict line is `HUMAN`, reason
    given. The last line for a number wins."""
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
    numbered as the adjudicator saw them, and the summary line to end with
    for each."""
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
    """`{number: summary}` off the fix round's output; the last line for a
    number wins."""
    return {int(m.group(1)): m.group(2).strip()
            for m in SUMMARY_LINE_RE.finditer(output or "")}


def addressed_reply(model, summary, sha):
    """The reply on an addressed thread: the header, what changed, the sha
    it changed in."""
    return (f"{COMMENT_HEADER.format(model=model)}\n\n"
            f"Addressed in {sha}: {summary}")


def declined_reply(model, reason):
    """The reply on a declined thread: the header and the reason; the thread
    stays open for its author to close."""
    return (f"{COMMENT_HEADER.format(model=model)}\n\n"
            f"Declined: {reason}\n\nLeaving this thread open.")


def round_reply(pull, pass_no, threads, verdicts, checks, sha):
    """The text a shepherd pass is recorded as, in the shape
    `record_round()` reads: one bullet per thread citing its file, the
    verdict it got, and a closing `VERDICT:` line -- `APPROVE` for a pass
    that found no thread, `REQUEST_CHANGES` for one that did."""
    lines = [f"Shepherd pass {pass_no} over {pull.url} at {sha[:12]}:"
             f" {len(threads)} unresolved thread(s), checks {checks}."]
    for n, t in enumerate(threads, 1):
        verdict, reason = verdicts[n]
        lines.append(f"- {where(t)} @{t.author}: {gist(t.body)}"
                     f" -- {verdict}: {reason}")
    lines.append("VERDICT: " + ("APPROVE" if not threads
                                else "REQUEST_CHANGES"))
    return "\n".join(lines)


def route_of(threads):
    """`github:LOGIN` for the pass: the threads' authors, sorted and joined
    with `+` when there are several; `github:ci` for a pass that had no
    thread and judged the checks alone."""
    authors = sorted({t.author for t in threads})
    return "github:" + ("+".join(authors) if authors else NO_AUTHOR)


def open_threads_question(pull, why, threads):
    """The ticket's question for a run parked on its PR: the URL, why the
    pass stopped, and the open threads listed."""
    lines = [f"PR open: {pull.url}", why]
    lines.extend(thread_line(n, t) for n, t in enumerate(threads, 1))
    return "\n".join(lines)


def quoted(thread):
    """A thread quoted whole -- follow-ups included -- for the parked
    question and the ledger."""
    body = "\n".join(f"> {line}" for line in conversation(thread)
                     .splitlines()) or "> (empty)"
    return f"{where(thread)} by @{thread.author} ({thread.url}):\n{body}"
