import re

from holophyte.agents.review_workspace import review_refs
from holophyte.babysit import maintainer_notes, thread_mentions
from holophyte.babysit.plain_text import readable
from holophyte.pr.github import NO_AUTHOR

CONVENTIONS_FILES = ("AGENTS.md", "CLAUDE.md")
CONVENTIONS_CAP = 4000

VERDICTS = ("ADDRESS", "DECLINE", "HUMAN")
VERDICT_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?THREAD\s+(\d+)\s*[:.)-]\s*(ADDRESS|DECLINE|HUMAN)\b"
    r"\s*(?:[-–—:,]+\s*)?(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
SUMMARY_LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?THREAD\s+(\d+)\s*[:.)-]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE)
COMMENT_HEADER = "---- Comment by {model} ----"
GIST_CHARS = 200


def gist(text, limit=GIST_CHARS):
    line = " ".join((text or "").split())
    return line if len(line) <= limit else line[:limit - 1].rstrip() + "…"


def where(thread):
    if thread.kind == "conversation":
        return "conversation on the pull request"
    if not thread.path:
        return "(no file)"
    return f"{thread.path}:{thread.line}" if thread.line else thread.path


def conversation(thread, *, label_all=False):
    if label_all:
        return "\n\n".join(f"@{c.author}: {c.body.strip()}" for c in thread.comments)
    parts = [thread.body.strip()]
    parts.extend(f"@{c.author} replied:\n{c.body.strip()}" for c in thread.replies)
    return "\n\n".join(parts)


def thread_line(number, thread):
    return f"{number}. {where(thread)} (@{thread.author}): {gist(thread.body)}"


def conventions(wt):
    return tuple((n, (wt / n).read_text(errors="replace").strip())
                 for n in CONVENTIONS_FILES if (wt / n).is_file())


def conventions_paragraph(files):
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
    found = {}
    for m in VERDICT_LINE_RE.finditer(reply or ""):
        number = int(m.group(1))
        if 1 <= number <= count:
            found[number] = (m.group(2).upper(), m.group(3).strip())
    return {n: found.get(n, ("HUMAN", "the adjudicator gave no verdict for"
                                      " this thread"))
            for n in range(1, count + 1)}


def fix_brief(pull, addressed, ticket):
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
    return {int(m.group(1)): m.group(2).strip()
            for m in SUMMARY_LINE_RE.finditer(output or "")}


def addressed_reply(model, summary, sha):
    return (f"{COMMENT_HEADER.format(model=model)}\n\n"
            f"Addressed in {sha}: {summary}")


def declined_reply(model, reason):
    return (f"{COMMENT_HEADER.format(model=model)}\n\n"
            f"Declined: {reason}")


def round_reply(pull, pass_no, threads, verdicts, checks, sha):
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
    authors = sorted({t.author for t in threads})
    return "github:" + ("+".join(authors) if authors else NO_AUTHOR)


def open_threads_question(pull, why, threads):
    return "\n".join([f"PR open: {pull.url}", why]
                     + [thread_line(n, t) for n, t in enumerate(threads, 1)])


def quoted(thread):
    body = "\n\n".join([readable(thread.body)] + [
        f"@{c.author} replied:\n{readable(c.body)}" for c in thread.replies])
    return f"{where(thread)} by @{thread.author} ({thread.url}):\n{body or '(empty)'}"
