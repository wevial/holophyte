import hashlib
import re
from dataclasses import dataclass

from holophyte.babysit.thread_mentions import bot_author
from holophyte.config.config_tables import merge_config
from holophyte.pr.github import Thread

SECTION_RE = re.compile(r"Outside diff range comments\s*\((\d+)\)")
DETAILS_RE = re.compile(r"<details>|</details>")
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


def _details(text):
    blocks, depth, start = [], 0, 0
    for tag in DETAILS_RE.finditer(text):
        if tag.group() == "<details>":
            depth += 1
            if depth == 1:
                start = tag.end()
        elif depth:
            depth -= 1
            if depth == 0:
                inner = text[start:tag.start()]
                summary = SUMMARY_RE.match(inner)
                if summary:
                    blocks.append((summary[1].strip(), inner[summary.end():]))
    return blocks


def _file_findings(path, text):
    locations = list(LOCATION_RE.finditer(text))
    for n, location in enumerate(locations):
        end = locations[n + 1].start() if n + 1 < len(locations) else len(text)
        chunk = CLOSERS_RE.sub("", text[location.start():end]).strip()
        severity = SEVERITY_RE.search(location[3])
        title = TITLE_RE.search(chunk)
        if severity and title:
            start = int(location[1])
            yield Finding(path, start, int(location[2] or start), severity[1],
                          title[1].strip(), chunk)


def findings(body):
    for summary, inner in _details(_unquoted(body or "")):
        section = SECTION_RE.search(summary)
        if section is None:
            continue
        found = [finding for name, text in _details(inner)
                 for finding in _file_findings(
                     FILE_SUMMARY_RE.sub(r"\1", name), text)]
        if len(found) < int(section[1]):
            found.append(Finding("", None, None, "", UNSPLIT_TITLE,
                                 CLOSERS_RE.sub("", inner).strip()))
        return found
    return []


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
