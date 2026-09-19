"""Human instructions in the pull request's paged issue comments."""
from holophyte.config_tables import merge_config
from holophyte.pr import Thread
from holophyte.thread_mentions import classify


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
    replies = {c.get("body", "").split("\n\n---- Comment by ")[0]
               for c in comments if isinstance(c, dict)
               and "\n\n---- Comment by " in c.get("body", "")}
    for comment in comments:
        thread = _instruction(comment, pull, merge)
        if thread and quote_request(thread) not in replies:
            yield thread


def _instruction(comment, pull, merge):
    if (not isinstance(comment, dict)
            or "\n\n---- Comment by " in comment.get("body", "")):
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
