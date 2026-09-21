"""Structured PR findings and read-only compatibility for old round rows."""
import re

from holophyte.redact import outbound
from holophyte.thread_mentions import bot_author

RAW_LIMIT = 20_000
CUT = "\n[original comment truncated]"
LEGACY = re.compile(r" -- (?:MENTIONED: )?(ADDRESS|DECLINE|HUMAN|FOLLOW_UP): ")
AUTHOR = re.compile(r"^- (.*?) @([^:]+):")


def bounded_raw(text):
    """Redact before cutting so a partial secret cannot escape redaction."""
    text = outbound(text)
    return text if len(text) <= RAW_LIMIT else text[:RAW_LIMIT - len(CUT)] + CUT


def thread_finding(thread, verdict, legacy, bot_logins):
    """Keep the original parser's fingerprint keys, never its comment blob."""
    action, summary = verdict
    author = thread.comments[-1] if thread.classification == "MENTIONED" else thread
    is_bot = bot_author(author.author, bot_logins, author.author_kind)
    finding = dict(kind="thread", author=author.author,
                   author_kind="bot" if is_bot else author.author_kind,
                   verdict=action, summary=outbound(summary),
                   message=outbound(summary), path=thread.path or "(no file)",
                   line=thread.line, url=thread.url, severity=legacy["severity"],
                   raw=bounded_raw(author.body))
    keys = {key: legacy.get(key) for key in ("path", "line", "severity")}
    if any(finding[key] != value for key, value in keys.items()):
        finding["fingerprint"] = keys
    return finding


def normalize_thread(finding, bot_logins):
    """Decode the final factory marker; leave ordinary reviewer prose alone."""
    if finding.get("summary") is not None or finding.get("kind") == "instruction":
        return finding
    message = finding.get("message", "")
    if finding.get("kind") == "finding" and finding.get("author"):
        summary = finding.get("request", message)
        return dict(finding, kind="thread", verdict="ADDRESS", summary=summary,
                    message=summary, raw=bounded_raw(message),
                    author_kind="bot" if bot_author(finding["author"], bot_logins)
                    else "unknown", url=finding.get("url", ""))
    verdicts = list(LEGACY.finditer(message))
    author = AUTHOR.match(message)
    if not verdicts or not author:
        return finding
    verdict = verdicts[-1]
    path, login = author.groups()
    location = re.fullmatch(r"(.*):(\d+)", path)
    if location:
        path, line = location[1], int(location[2])
    else:
        line = finding.get("line")
    summary = message[verdict.end():].split("\nVERDICT:", 1)[0].strip()
    return dict(finding, kind="thread", author=login,
                author_kind="bot" if bot_author(login, bot_logins) else "unknown",
                verdict=verdict[1], summary=summary, message=summary,
                path=path, line=line, url=finding.get("url", ""),
                raw=bounded_raw(message[:verdict.start()]))
