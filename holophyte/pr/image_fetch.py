"""GitHub's image proxy caches its first fetch: only a new URL clears a cut image."""
import http.client
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from holophyte.config.config_tables import merge_config
from holophyte.loop.gates import InfraFailure
from holophyte.pr import github

PROXY_HOST = "camo.githubusercontent.com"
RELINKS = 2
FETCH_TIMEOUT = 60
IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"


class _Images(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pairs = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        found = dict(attrs)
        src, canonical = found.get("src") or "", found.get("data-canonical-src")
        if canonical and urllib.parse.urlsplit(src).netloc == PROXY_HOST:
            self.pairs.append((src, canonical))


def proxied_pairs(body_html):
    parser = _Images()
    parser.feed(body_html)
    parser.close()
    return parser.pairs


def _fetch(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "holophyte"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return response.read(limit + 1)


def short_by(proxied, canonical, limit):
    """(served, full) byte counts when the proxy serves less than the image."""
    full = _fetch(canonical, limit)
    if len(full) > limit:
        return None
    served = _fetch(proxied, limit)
    png = urllib.parse.urlsplit(canonical).path.lower().endswith(".png")
    if len(served) < len(full) or (png and not served.endswith(IEND)):
        return len(served), len(full)
    return None


def relinked(url):
    parts = urllib.parse.urlsplit(url)
    version = re.search(r"(?:^|&)v=(\d+)(?=&|$)", parts.query)
    if version:
        query = (parts.query[:version.start(1)] + str(int(version[1]) + 1)
                 + parts.query[version.end(1):])
    else:
        query = f"{parts.query}&v=2" if parts.query else "v=2"
    return urllib.parse.urlunsplit(parts._replace(query=query))


def _linked(body, url, new):
    return re.sub(re.escape(url) + r"(?=[)\s\"'>]|\Z)", lambda _: new, body)


def check(target, pull):
    lines = []
    try:
        _relink_short(target, pull, lines)
    except (InfraFailure, OSError, http.client.HTTPException, ValueError) as e:
        lines.append(f"image fetch check skipped: {e}")
    return lines


def _relink_short(target, pull, lines):
    limit = merge_config(target).media_max_file_mb * 1024 * 1024
    watched = None
    for round_ in range(RELINKS + 1):
        short = {}
        for proxied, canonical in proxied_pairs(
                github.pull_body_html(target, pull)):
            if watched is None or canonical in watched:
                sizes = short.get(canonical) or short_by(proxied, canonical, limit)
                if sizes:
                    short[canonical] = sizes
        if not short:
            return
        if round_ == RELINKS:
            lines.extend(f"GitHub's image proxy served {served} of {full} bytes"
                         f" for {url}; still short after {RELINKS} re-links"
                         for url, (served, full) in short.items())
            return
        endpoint = f"repos/{pull.repo}/pulls/{pull.number}"
        written = github.rest(target, pull, "GET", endpoint)["body"] or ""
        watched = set()
        for url, (served, full) in short.items():
            new, before = relinked(url), written
            written = _linked(written, url, new)
            if written != before:
                watched.add(new)
                lines.append(f"GitHub's image proxy served {served} of {full}"
                             f" bytes for {url}; re-linked as {new}")
        if not watched:
            return
        github.edit_pr_body(target, pull, written)
