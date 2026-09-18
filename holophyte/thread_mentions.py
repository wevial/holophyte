"""Explicit pull request instructions addressed to the factory."""
import re
from dataclasses import replace


def classify(thread, handle):
    """Only the latest comment can address the factory."""
    pattern = re.compile(r"(?<![\w@-])@" + re.escape(handle) + r"(?![\w-])", re.I)
    latest = thread.comments[-1]
    if handle and pattern.search(latest.body):
        return replace(thread, classification="MENTIONED",
                       request=pattern.sub("", latest.body).strip())
    return thread


def instruction(thread):
    return (f"Instruction from @{thread.comments[-1].author} on the pull request:\n"
            f"{thread.request}")
