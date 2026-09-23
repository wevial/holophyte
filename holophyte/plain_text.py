"""A review comment as plain text for a parked question (KO-717).

Review bots write for GitHub's renderer: a linked `img` badge, HTML around
the finding, and a `details` block repeating it as a fix prompt. A
maintainer reading the question in the console wants the finding alone.
"""
import re
from html.parser import HTMLParser

QUOTE_CHARS = 600
QUOTE_MARKER_RE = re.compile(r"^[ \t]*(?:>[ \t]?)+", re.MULTILINE)
BLANK_RUN_RE = re.compile(r"\n[ \t]*(?:\n[ \t]*)+\n")


class _Text(HTMLParser):
    """Collects text: an `img` as its `alt`, other tags dropped for their
    text, and each `details` block, however nested, dropped whole."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.hidden += 1
        elif tag == "img" and not self.hidden:
            self.parts.append(dict(attrs).get("alt") or "")

    def handle_endtag(self, tag):
        if tag == "details" and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def readable(text, limit=QUOTE_CHARS):
    """`text` without markup, `details` blocks or quote markers, blank runs
    collapsed, cut to `limit` characters ending in "…"."""
    parser = _Text()
    parser.feed(text or "")
    parser.close()
    plain = QUOTE_MARKER_RE.sub("", "".join(parser.parts))
    plain = BLANK_RUN_RE.sub("\n\n", plain).strip()
    return plain if len(plain) <= limit else plain[:limit - 1].rstrip() + "…"
