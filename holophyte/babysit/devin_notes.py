import re

MARKER_RE = re.compile(r"<!--\s*devin-review-comment\b(.*?)-->", re.DOTALL)
ANALYSIS_RE = re.compile(r'"kind"\s*:\s*"analysis"')


def is_analysis(thread):
    marker = MARKER_RE.search(thread.body)
    answered = any(reply.author_kind != "bot"
                   and not reply.body.startswith("---- Comment by ")
                   for reply in thread.replies)
    devin = thread.author_kind == "bot" and thread.author.startswith("devin")
    return bool(devin and marker and ANALYSIS_RE.search(marker[1])) and not answered


def split(threads):
    notes = tuple(t for t in threads if is_analysis(t))
    return tuple(t for t in threads if t not in notes), notes
