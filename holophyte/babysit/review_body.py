import hashlib
import re
from dataclasses import dataclass

from holophyte.babysit.thread_mentions import bot_author
from holophyte.config.config_tables import merge_config
from holophyte.pr.github import Thread

SECTION_RE = re.compile(r"Outside diff range comments\s*\((\d+)\)")
TOP_SECTION_RE = re.compile(
    r"(?:Outside diff range|Nitpick|Duplicate|Additional) comments\s*\(\d+\)"
    r"|Review details")
DETAILS_RE = re.compile(
    r"^[ \t]*(?:(<details>)[ \t]*(?=<summary>|$)"
    r"|(?:</(?:blockquote|details)>[ \t]*)+$)", re.MULTILINE)
FENCE_RE = re.compile(r"^```.*?^```[^\n]*$", re.MULTILINE | re.DOTALL)
SUMMARY_RE = re.compile(r"\s*<summary>(.*?)</summary>(?:<blockquote>)?", re.DOTALL)
FILE_SUMMARY_RE = re.compile(r"^(.+?)\s*\(\d+\)$")
LOCATION_RE = re.compile(r"^`(\d+)(?:-(\d+))?`:(.*)$", re.MULTILINE)
SEVERITY_RE = re.compile(r"_\W*(Critical|Major|Minor|Trivial)_")
TITLE_RE = re.compile(r"^\*\*(.+?)\*\*\s*$", re.MULTILINE)
CLOSERS_RE = re.compile(r"(?:\s*</?(?:blockquote|details)>)+\s*$")
MARKER = "<!-- holophyte:review-finding {} -->"
MARKER_RE = re.compile(r"<!-- holophyte:review-finding ([0-9a-f]{12}) -->")
UNSPLIT_TITLE = "Outside diff range comments that could not be split"


@dataclass(frozen=True)
class Finding:
    path: str
    start: int | None
    end: int | None
    severity: str
    title: str
    text: str

    @property
    def lines(self):
        if self.start is None:
            return ""
        return f"{self.start}-{self.end}" if self.end != self.start else str(self.start)

    @property
    def header(self):
        where = f"{self.path}:{self.lines}" if self.path else "(no file)"
        severity = f" ({self.severity})" if self.severity else ""
        return f"{where}{severity}: {self.title}"


def _unquoted(body):
    return "\n".join(line[2:] if line.startswith("> ") else
                     "" if line == ">" else line
                     for line in body.splitlines())


def _unfenced(pattern, text):
    fences = [fence.span() for fence in FENCE_RE.finditer(text)]
    return [m for m in pattern.finditer(text)
            if not any(a <= m.start() < b for a, b in fences)]


def _openings(text):
    for tag in _unfenced(DETAILS_RE, text):
        summary = SUMMARY_RE.match(text, tag.end()) if tag[1] else None
        if summary:
            yield tag.start(), summary[1].strip()


def _unclosed_section(text):
    starts = [(start, summary) for start, summary in _openings(text)
              if TOP_SECTION_RE.search(summary)]
    for n, (start, summary) in enumerate(starts):
        if SECTION_RE.search(summary):
            end = starts[n + 1][0] if n + 1 < len(starts) else len(text)
            return [_unsplit(text[start:end])]
    return []


def _details(text):
    blocks, depth, start = [], 0, 0
    for tag in _unfenced(DETAILS_RE, text):
        if tag[1]:
            depth += 1
            if depth == 1:
                start = tag.end()
            continue
        for _ in range(tag.group().count("</details>")):
            if depth == 0:
                break
            depth -= 1
            if depth == 0:
                inner = text[start:tag.start()]
                summary = SUMMARY_RE.match(inner)
                if summary:
                    blocks.append((summary[1].strip(), inner[summary.end():]))
    return blocks


def _file_findings(path, text):
    locations = [location for location in _unfenced(LOCATION_RE, text)
                 if SEVERITY_RE.search(location[3])]
    for n, location in enumerate(locations):
        end = locations[n + 1].start() if n + 1 < len(locations) else len(text)
        chunk = CLOSERS_RE.sub("", text[location.start():end]).strip()
        severity = SEVERITY_RE.search(location[3])
        title = TITLE_RE.search(chunk)
        if severity and title:
            start = int(location[1])
            yield Finding(path, start, int(location[2] or start), severity[1],
                          title[1].strip(), chunk)


def _unsplit(text):
    return Finding("", None, None, "", UNSPLIT_TITLE,
                   CLOSERS_RE.sub("", text).strip())


def findings(body):
    text = _unquoted(body or "")
    for summary, inner in _details(text):
        section = SECTION_RE.search(summary)
        if section is None:
            continue
        found = [finding for name, block in _details(inner)
                 for finding in _file_findings(
                     FILE_SUMMARY_RE.sub(r"\1", name), block)]
        if len(found) < int(section[1]):
            found.append(_unsplit(inner))
        return found
    return _unclosed_section(text)


def fingerprint(reviewer, finding):
    title = finding.title if finding.path else finding.text
    key = "\n".join((reviewer, finding.path, finding.lines,
                     " ".join(title.split())))
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def answered(comments):
    return {mark for c in comments
            if isinstance(c, dict) and c.get("viewerDidAuthor") is True
            for mark in MARKER_RE.findall(c.get("body") or "")}


def review_threads(target, pull, node, comments):
    reviews = (node.get("reviews") or {}).get("nodes") or ()
    if not reviews:
        return
    merge = merge_config(target)
    bots = (*merge.bot_authors, *merge.bot_logins)
    done = answered(comments)
    for review in reviews:
        if not isinstance(review, dict):
            continue
        author = review.get("author") or {}
        login = author.get("login") or "unknown"
        if not bot_author(login, bots,
                          "bot" if author.get("__typename") == "Bot" else ""):
            continue
        for finding in findings(review.get("body")):
            mark = fingerprint(login, finding)
            if mark not in done:
                done.add(mark)
                yield Thread(
                    id=mark, path=finding.path, line=finding.start, author=login,
                    body=f"{finding.header}\n\n{finding.text}",
                    url=review.get("url") or pull.url, author_kind="bot",
                    kind="review_body", node_id=review.get("id") or "")


def answer(thread, body):
    header = thread.body.splitlines()[0]
    return (f"> [Review by @{thread.author}]({thread.url}): {header}\n\n"
            f"{body}\n\n{MARKER.format(thread.id)}")
