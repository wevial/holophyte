import re

from holophyte.commit_hygiene import rewrite_unpublished
from store import steer_notes
from store.read import run_snapshot
from ticket_criteria import amendment_edits

WITHHELD = "[maintainer note withheld]"
WINDOW = 8
SHORTEST = 4
FACTORY_BABYSIT = re.compile(r"(?:sent back to the babysitter"
                             r"|\S+ was marked ready for review)\Z")


def private_texts(conn, run_id):
    if conn is None or run_id is None:
        return ()
    ticket_id = run_snapshot(conn, run_id).ticketId
    texts = []
    for kind, note in steer_notes.console_notes(conn, ticket_id):
        if kind == "babysit" and FACTORY_BABYSIT.match(note.strip()):
            continue
        try:
            note = amendment_edits(note)[1]
        except ValueError:
            pass
        if note.strip() and note not in texts:
            texts.append(note)
    return tuple(texts)


def _pattern(words):
    body = r"\s+(?:>+\s*)*".join(re.escape(word) for word in words)
    head = r"(?<!\w)" if re.match(r"\w", words[0]) else ""
    tail = r"(?!\w)" if re.search(r"\w\Z", words[-1]) else ""
    return re.compile(head + body + tail, re.IGNORECASE)


def _patterns(note):
    words = note.split()
    if len(words) < SHORTEST:
        return []
    if len(words) < WINDOW:
        return [_pattern(words)]
    return [_pattern(words[i:i + WINDOW])
            for i in range(len(words) - WINDOW + 1)]


def withhold(text, notes):
    if not text or not notes:
        return text
    spans = sorted(m.span() for note in notes for pattern in _patterns(note)
                   for m in pattern.finditer(text))
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + WITHHELD + text[end:]
    return text


def withhold_history(wt, branch, notes):
    if not notes:
        return

    def transform(message):
        text = message.decode("utf-8", errors="surrogateescape")
        return withhold(text, notes).encode("utf-8", errors="surrogateescape")

    rewrite_unpublished(wt, branch, transform, "console note withholding")
