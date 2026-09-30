"""A review comment as plain text for a parked question."""
import re
from html.parser import HTMLParser

QUOTE_CHARS = 600
QUOTE_MARKER_RE = re.compile(r"^[ \t]*(?:>[ \t]?)+", re.MULTILINE)
BLANK_RUN_RE = re.compile(r"\n[ \t]*(?:\n[ \t]*)+\n")
BLOCK_TAGS = frozenset((
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl",
    "dt", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5",
    "h6", "header", "hr", "li", "ol", "p", "pre", "section", "summary",
    "table", "tr", "ul"))


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.hidden += 1
        elif self.hidden:
            return
        elif tag == "img":
            self.parts.append(dict(attrs).get("alt") or "")
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "details" and self.hidden:
            self.hidden -= 1
        elif tag in BLOCK_TAGS and tag != "br" and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def readable(text, limit=QUOTE_CHARS):
    parser = _Text()
    parser.feed(text or "")
    parser.close()
    plain = QUOTE_MARKER_RE.sub("", "".join(parser.parts))
    plain = BLANK_RUN_RE.sub("\n\n", plain).strip()
    return plain if len(plain) <= limit else plain[:limit - 1].rstrip() + "…"
