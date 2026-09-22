"""Explicit pull request instructions addressed to the factory."""
import re
from dataclasses import replace


def classify(thread, handle):
    """Only the latest comment can address the factory."""
    pattern = re.compile(r"(?<![\w@-])@" + re.escape(handle) + r"(?![\w-])", re.I)
    latest = thread.comments[-1]
    if handle and pattern.search(latest.body):
        request = pattern.sub("", latest.body).strip()
        marker = re.match(r"^(\?|ask\s*:|fix\s*:)\s*", request, re.I)
        intent = "unmarked"
        if marker:
            intent = "fix" if marker[1].lower().startswith("fix") else "ask"
            request = request[marker.end():].strip()
        return replace(thread, classification="MENTIONED",
                       request=request, intent=intent)
    return thread


def instruction(thread):
    return (f"Instruction from @{thread.comments[-1].author} on the pull request:\n"
            f"{thread.request}")


def bot_author(author, bot_logins, author_kind=""):
    """Share bot identity rules between stored requests and legacy reads."""
    return (author_kind == "bot" or author.lower().endswith("[bot]")
            or author.lower() in {name.lower() for name in bot_logins})
