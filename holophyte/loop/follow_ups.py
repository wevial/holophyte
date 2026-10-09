"""FOLLOW_UP lines read from a fix turn's commits, filed or kept once the run merges."""
from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass

import provider
import store
import store.follow_ups
from holophyte.redact import safe_print as print

FALLBACK_KIND = "feature"
_LINE = re.compile(r"(?:- )?FOLLOW_UP\((feature|guardrail)\): (.+?)"
                   r"(?: @ ([^\s:]+)(?::(\d+))?)?")
_LEGACY = re.compile(r"(?:- )?FOLLOW_UP(.*)")
_QUOTES = str.maketrans("", "", "`'\"‘’“”")
TITLE_PREFIX = "Draft follow-up: "
TITLE_CHARS = 70
ESTIMATE_MIN = 30
BACKLOG = "Backlog"


@dataclass(frozen=True)
class Finding:
    kind: str
    kind_given: bool
    text: str
    path: str | None = None
    line: int | None = None


def parse(message):
    found = []
    for raw in message.splitlines():
        text = raw.strip()
        strict = _LINE.fullmatch(text)
        if strict:
            kind, body, path, line = strict.groups()
            found.append(Finding(kind, True, body.strip(), path,
                                 int(line) if line else None))
            continue
        legacy = _LEGACY.fullmatch(text)
        rest = legacy and legacy.group(1).strip().removeprefix(":").strip()
        if rest:
            found.append(Finding(FALLBACK_KIND, False, rest))
    return found


def fingerprint(text, path):
    normal = " ".join(text.lower().translate(_QUOTES).split()).rstrip(".")
    return hashlib.sha256(f"{normal}\n{path or ''}".encode()).hexdigest()


def _commits(wt, sha):
    log = subprocess.run(
        ["git", "log", "--reverse", "--format=%H%x00%B%x1e", f"{sha}..HEAD"],
        cwd=wt, capture_output=True, text=True, check=True).stdout
    for record in log.split("\x1e"):
        commit, _, message = record.strip("\n").partition("\x00")
        if commit:
            yield commit, message


def capture(conn, run_id, wt, sha):
    """Each FOLLOW_UP line of the commits after `sha`, stored pending."""
    try:
        for commit, message in _commits(wt, sha):
            for finding in parse(message):
                store.follow_ups.record_follow_up(
                    conn, run_id, commit, finding.kind, finding.kind_given,
                    finding.text, fingerprint(finding.text, finding.path),
                    path=finding.path, line=finding.line)
    except Exception as e:
        print(f"[holo2] follow-ups not captured: {e}")


def draft_title(text):
    head = text[:TITLE_CHARS]
    if len(text) > TITLE_CHARS and text[TITLE_CHARS] != " " and " " in head:
        head = head.rsplit(" ", 1)[0]
    return TITLE_PREFIX + head.rstrip()


@dataclass(frozen=True)
class Origin:
    key: str
    pr_url: str | None
    merge_sha: str | None


def draft_body(row, origin):
    where = f"`{row.path}:{row.line}`" if row.line is not None else (
        f"`{row.path}`" if row.path else None)
    landed = (f"Pull request: {origin.pr_url}" if origin.pr_url
              else f"Merge sha: `{origin.merge_sha}`")
    facts = [f"Found at: {where}" if where else None, landed,
             f"Originating ticket: {origin.key}",
             None if row.kindGiven else "Kind not given; filed as feature."]
    summary = "\n".join(f"- {fact}" for fact in facts if fact)
    return (
        f"# {draft_title(row.text)}\n\n"
        "## Summary\n\n"
        "DRAFT: the operator completes this before it leaves Backlog.\n\n"
        f"{row.text}\n\n{summary}\n\n"
        "## What / Why / How\n\n"
        "**What:** <What observable behavior or capability are we delivering?>"
        "\n\n**Why:** <What problem does this solve, for whom?>\n\n"
        "**How:** <Intended technical direction and constraints.>\n\n"
        "## In scope\n\n- <Behavior, surface, route, data, or integration.>\n\n"
        "## Out of scope\n\n- <Adjacent work that is explicitly excluded.>\n\n"
        "## Acceptance criteria\n\n"
        "- [ ] Given <starting condition>, when <action>, then <observable"
        " result>.\n\n"
        "## Verify command(s)\n\n```\n<Exact runnable command(s).>\n```\n\n"
        "## Implementation notes\n\n"
        f"- Follow-up fingerprint: {row.fingerprint}\n\n"
        "## Estimate & dependencies\n\n"
        f"Estimate: {ESTIMATE_MIN} min · Depends on: none\n\n"
        "## Open questions\n\n"
        "- Complete this draft: scope, criteria with witnesses, verify"
        " commands and estimate.\n")


def _origin(conn, run_id):
    return Origin(*conn.execute(
        "SELECT t.linearIdentifier, r.prUrl, r.mergeSha FROM runs r"
        " JOIN tickets t ON t.id = r.ticketId WHERE r.id = ?",
        (run_id,)).fetchone())


def _board(target):
    board = provider.board_for(target)
    if board is None:
        raise RuntimeError("the project has no board to file on")
    return board


def settle_row(conn, row, origin, board_of):
    if row.kind == "guardrail":
        store.follow_ups.settle_ledger(conn, row.id)
        return
    board = board_of()
    filed = store.follow_ups.filed_drafts(conn, row.id)
    closed = board.closed_identifiers(filed) if filed else {}
    still_open = [key for key in filed if key not in closed]
    if still_open:
        store.follow_ups.settle_duplicate(conn, row.id, still_open[0])
        return
    key = board.file(draft_title(row.text), draft_body(row, origin),
                     ESTIMATE_MIN, BACKLOG)
    store.follow_ups.settle_filed(conn, row.id, key)


@contextlib.contextmanager
def _filing_turn(target):
    # One settle per project at a time, its duplicate check through its filing:
    # a file lock, not the store's, because it is held across a board call.
    path = target.holo_dir / "follow-ups.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def settle(target, conn, run_id):
    """A merged run's pending rows; a failure is recorded, never raised."""
    try:
        with _filing_turn(target):
            _settle_rows(target, conn, run_id)
    except Exception as e:
        print(f"[holo2] follow-ups of run {run_id} not settled: {e}")
        with contextlib.suppress(Exception):
            store.record_event(conn, run_id, "follow_up_settle_failed",
                               f"follow-ups not settled: {e}", level="detail")


def _settle_rows(target, conn, run_id):
    origin = _origin(conn, run_id)
    board_of = functools.cache(lambda: _board(target))
    for row in store.follow_ups.pending_follow_ups(conn, run_id):
        try:
            settle_row(conn, row, origin, board_of)
        except Exception as e:
            print(f"[holo2] follow-up {row.id} not filed: {e}")
            store.follow_ups.settle_unfiled(conn, row.id,
                                            f"{type(e).__name__}: {e}")
