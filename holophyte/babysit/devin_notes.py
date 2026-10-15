import re

MARKER_RE = re.compile(r"<!--\s*devin-review-comment\b(.*?)-->", re.DOTALL)
ANALYSIS_RE = re.compile(r'"kind"\s*:\s*"analysis"')
FACTORY = "---- Comment by "
NOTE_LINE = "Read as a note, not a finding"
FINDING_LINE = "Triaged as a finding"


def triage(thread):
    for reply in thread.replies:
        if reply.body.startswith(FACTORY):
            line = reply.body.partition("\n")[2].strip()
            if line.startswith(NOTE_LINE):
                return "note"
            if line.startswith(FINDING_LINE):
                return "finding"
    return None


def is_analysis(thread):
    marker = MARKER_RE.search(thread.body)
    answered = any(not reply.body.startswith(FACTORY) for reply in thread.replies)
    devin = thread.author_kind == "bot" and thread.author.startswith("devin")
    return (bool(devin and marker and ANALYSIS_RE.search(marker[1]))
            and not answered and triage(thread) != "finding")


def split(threads):
    notes = tuple(t for t in threads if is_analysis(t))
    return tuple(t for t in threads if t not in notes), notes
