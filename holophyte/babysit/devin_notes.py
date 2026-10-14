import re

MARKER_RE = re.compile(r"<!--\s*devin-review-comment\b(.*?)-->", re.DOTALL)
ANALYSIS_RE = re.compile(r'"kind"\s*:\s*"analysis"')


def is_analysis(thread):
    marker = MARKER_RE.search(thread.body)
    return bool(marker and ANALYSIS_RE.search(marker[1]))


def split(threads):
    notes = tuple(t for t in threads if is_analysis(t))
    return tuple(t for t in threads if t not in notes), notes
