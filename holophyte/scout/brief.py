"""Short texts served for scouts whose lead wrote none: titles and reasons."""
import re

SCOUT_TITLE_CHARS, TASK_TITLE_CHARS, TASK_TITLE_WORDS = 70, 60, 8
REASON_CHARS = 100
SENTENCE_END = re.compile(r"[.?!](?=\s|$)|[\r\n]")
CLAUSE_END = re.compile(r": |; |, | \(|\. |\? ")
LAUNCH = re.compile(r"could not launch: (.*)")
CREDENTIAL = re.compile(r"implementer credential variable \S+ is not set")


def collapsed(text):
    return " ".join(text.split())


def trimmed(text, limit):
    text = collapsed(text)
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if text[limit - 1] != " ":
        cut = cut.rpartition(" ")[0] or cut
    return cut.rstrip() + "…"


def scout_title(ask):
    text = ask.strip()
    end = SENTENCE_END.search(text)
    if end is not None:
        kept = end.group() if end.group() in ("?", "!") else ""
        text = text[:end.start()] + kept
    return trimmed(text if text.strip() else ask, SCOUT_TITLE_CHARS)


def task_title(question):
    text = collapsed(question)
    end = CLAUSE_END.search(text)
    if end is not None:
        text = text[:end.start()] + ("?" if end.group() == "? " else "")
    words = text.split()
    if len(words) > TASK_TITLE_WORDS:
        text = " ".join(words[:TASK_TITLE_WORDS]) + "…"
    return trimmed(text, TASK_TITLE_CHARS)


def failed_reason(excerpt):
    lines = [line for line in (excerpt or "").splitlines() if line.strip()]
    if not lines:
        return "no output"
    launch = next((found for line in lines
                   if (found := LAUNCH.search(line)) is not None), None)
    if launch is not None:
        why = CREDENTIAL.sub("implementer credential not set", launch.group(1))
        return trimmed(f"could not start: {why}", REASON_CHARS)
    return trimmed(lines[-1], REASON_CHARS)
