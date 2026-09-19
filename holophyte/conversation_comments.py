"""Human instructions in the pull request's paged issue comments."""
import re

from holophyte.config_tables import merge_config
from holophyte.pr import Thread
from holophyte.thread_mentions import classify

REPLY_RE = re.compile(
    r"(> \[Request by @[^\n]+\]\([^\n]+\)\n>\n> .*?)"
    r"\n\n---- Comment by [^\n]+ ----\n\nAddressed in [0-9a-f]{40}: .+",
    re.DOTALL)


def _reply_quote(comment):
    body = comment.get("body") if isinstance(comment, dict) else None
    match = REPLY_RE.fullmatch(body) if isinstance(body, str) else None
    return match[1] if match else None


def conversation_threads(target, pull, node, read_page):
    """Yield only human mentions; conversation summaries are not findings."""
    comments = []
    while True:
        page = node.get("comments") or {}
        comments.extend(page.get("nodes") or ())
        info = page.get("pageInfo") or {}
        if not (info.get("hasNextPage") and info.get("endCursor")):
            break
        node = read_page(target, pull, None, info["endCursor"])
    if not comments:
        return
    merge = merge_config(target)
    replies = {_reply_quote(c) for c in comments}
    for comment in comments:
        thread = _instruction(comment, pull, merge)
        if thread and quote_request(thread) not in replies:
            yield thread


def _instruction(comment, pull, merge):
    if not isinstance(comment, dict) or _reply_quote(comment) is not None:
        return None
    author = comment.get("author") or {}
    login = author.get("login") or "unknown"
    if (author.get("__typename") != "User" or login.endswith("[bot]")
            or login in merge.bot_authors or login in merge.bot_logins):
        return None
    thread = classify(Thread(
        id=comment.get("id") or "", path="", line=None, author=login,
        body=comment.get("body") or "", url=comment.get("url") or pull.url,
        author_kind="user", kind="conversation"), merge.mention_handle)
    return thread if thread.classification == "MENTIONED" else None


def quote_request(thread):
    quote = "\n".join("> " + line for line in thread.body.splitlines())
    return f"> [Request by @{thread.author}]({thread.url})\n>\n{quote}"
