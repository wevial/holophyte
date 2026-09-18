"""Run, shipped and ledger HTTP reads, including project startup outages."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from time import time
from urllib.parse import parse_qs

import store.read
from holophyte.config import budget_scale
from holophyte.files import GIT_TIMEOUT, RangeError, git, touched_files
from holophyte.pool_handoff import workers_on_previous_build  # noqa: F401
from holophyte.report import ended_rows, host_label
from holophyte.runs import MAX_ROUNDS
from holophyte.target import worktree_path
from store.working import effective_work

# Accepted origins: https://HOST/OWNER/REPO and git@HOST:OWNER/REPO(.git).
# Other origins carry no link. Each segment must be one
# plain path segment: a `?`, `#`, `@`, `:` or whitespace in it would ride
# into the link as a query, fragment or credential, so it disqualifies the
# remote rather than being copied through.
SEGMENT = r"[^/?#@:\s]+"
REMOTE_SHAPES = (
    re.compile(rf"^https://(?P<host>{SEGMENT})/(?P<owner>{SEGMENT})/"
               rf"(?P<repo>{SEGMENT}?)(?:\.git)?/?$"),
    re.compile(rf"^git@(?P<host>{SEGMENT}):(?P<owner>{SEGMENT})/"
               rf"(?P<repo>{SEGMENT}?)(?:\.git)?/?$"),
)
# `/runs/N` and `/runs/N/files`: one run by id. The id is captured as typed
# so a non-integer is 400 rather than the static-file 404; both routes parse
# it through `parse_run_id()`.
RUN_PATH = re.compile(r"^/runs/([^/]+)$")
RUN_FILES_PATH = re.compile(r"^/runs/([^/]+)/files$")
RUN_LEDGER_PATH = re.compile(r"^/runs/([^/]+)/ledger$")
# The captured id is an integer when it is an optionally signed run of
# digits; anything else is 400. Integers no run can have (negative, or past
# SQLite's INTEGER range) are 404 like any other absent id.
RUN_ID = re.compile(r"^([+-]?)0*(\d+)$")
SQLITE_MAX_INT = 2**63 - 1
# Any integer of more significant digits than this is past SQLite's range,
# so it is judged on its length, before `int()` -- which refuses strings
# past Python's digit limit -- ever sees it.
SQLITE_MAX_DIGITS = len(str(SQLITE_MAX_INT))


def parse_run_id(text):
    """The `/runs/N` id as an int, or `None` when it names no possible run.

    Leading zeros are normalized away so `/runs/007` is run 7. A negative
    id, or one with more significant digits than SQLite's INTEGER holds,
    is `None`: the length check comes first so a path of thousands of
    digits is a 404, not a `ValueError` from `int()` past Python's limit.
    Raises ValueError when `text` is not an integer at all.
    """
    match = RUN_ID.match(text)
    if match is None:
        raise ValueError(f"run id must be an integer, got {text!r}")
    sign, digits = match.groups()
    if len(digits) > SQLITE_MAX_DIGITS:
        return None
    run_id = int(sign + digits)
    return run_id if 0 <= run_id <= SQLITE_MAX_INT else None


def no_store(target):
    """The 503 body for a target whose store does not exist yet."""
    return {"error": "no store",
            "detail": f"{target.path} has no store yet; nothing has run"
                      " against it on this host",
            "target": str(target.path)}


# An optional sign and digits: what `int()` accepts minus its leniencies
# (whitespace, underscores), so a cursor is exactly what the client typed.
INTEGER = re.compile(r"-?[0-9]+")


def parse_limit(query, default=None, cap=None):
    """`?limit=N` as a positive int, `default` when absent; ValueError
    otherwise. A limit past `cap` is answered as `cap`, not refused: a
    client asking for more than a page is a client asking for a page.

    The shape is the report's: a dashboard asks for the newest few rows,
    and `limit=0` or `limit=abc` is a client bug to be told about, not a
    request for nothing.
    """
    values = parse_qs(query, keep_blank_values=True).get("limit")
    if values is None:
        return default
    text = values[-1]
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"limit must be a positive integer, got {text!r}")
    limit = int(text)
    return limit if cap is None else min(limit, cap)


def parse_since(query):
    """`?since=MS` as an int; ValueError when absent or not an integer.

    `since` is required: a window over the whole ledger with no start is
    the whole table, which is not a page. Any integer parses; a `since`
    in the future is an empty window, not a 400.
    """
    values = parse_qs(query, keep_blank_values=True).get("since")
    if values is None:
        raise ValueError("since is required (epoch milliseconds)")
    text = values[-1]
    if not INTEGER.fullmatch(text):
        raise ValueError(f"since must be an integer of epoch milliseconds,"
                         f" got {text!r}")
    return int(text)


def parse_filter(query, name, allowed=None):
    """`?name=VALUE` as its text, None when absent; ValueError when
    `allowed` is given and the value is not one of them."""
    values = parse_qs(query, keep_blank_values=True).get(name)
    if values is None:
        return None
    text = values[-1]
    if allowed is not None and text not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(allowed)},"
                         f" got {text!r}")
    return text


def parse_before(query):
    """`?before=ID` as an int, None when absent; ValueError otherwise.

    Any integer parses, sign and size included: whether a run has that id
    is the view's question, and an id no run has is an empty page, not a
    400. Only a non-integer is a client bug to be told about.
    """
    values = parse_qs(query, keep_blank_values=True).get("before")
    if values is None:
        return None
    text = values[-1]
    if not INTEGER.fullmatch(text):
        raise ValueError(f"before must be an integer run id, got {text!r}")
    return int(text)


def json_host(target, host):
    """`host_label()` for JSON: null, not the table's `?`, for a row older
    than the host column, label or not."""
    return None if host is None else host_label(target, host)


def origin_web_url(target):
    """`https://HOST/OWNER/REPO` for the target's `origin`, or None.

    Read once per request from `git remote get-url origin` in the target's
    checkout and normalized from either of `REMOTE_SHAPES`; no `origin`, a
    remote of another shape, or any git failure is None, so the rows it
    feeds carry no link rather than a bad one.
    """
    try:
        code, out = git(target.path, "remote", "get-url", "origin")
    except (subprocess.TimeoutExpired, OSError):
        return None
    if code != 0:
        return None
    for shape in REMOTE_SHAPES:
        found = shape.match(out.strip())
        if found:
            return "https://{host}/{owner}/{repo}".format(**found.groupdict())
    return None


def commit_url(target, sha, origin):
    """`ORIGIN/commit/SHA` when `sha` is an ancestor of `origin/main` in the
    target's checkout, else None.

    A local merge never pushed, one rewritten on the way up, or a sha the
    checkout does not hold would link to a page that does not exist, so the
    ancestry check gates the link; `origin/main` absent (a fresh clone) or
    git failing for any reason is the same None, never an error.
    """
    if not sha or not origin:
        return None
    try:
        code, _ = git(target.path, "merge-base", "--is-ancestor", sha,
                      "origin/main")
    except (subprocess.TimeoutExpired, OSError):
        return None
    return f"{origin}/commit/{sha}" if code == 0 else None


def runs(target, query=""):
    """The `/runs` answer: `--report`'s rows as JSON, first `limit` of them.

    Same rows, same order as `report_rows()` -- oldest first -- with the
    tuple's positions named, plus `ended_ms`: the run's `endedAt` in epoch
    milliseconds, which the table never prints and a drawer's "last merge
    KO-n · 2h ago" is read from against `/status`'s `now`, and `merge_sha`:
    the full merge commit a merged run landed on main as, null for any
    other outcome or a row older than the column. `host` is None for a row
    older than the column, label or not, as on `/status`.
    """
    try:
        limit = parse_limit(query)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        rows = ended_rows(conn)
    finally:
        conn.close()
    if limit is not None:
        rows = rows[:limit]
    return 200, {
        "rows": [{"ticket": ticket, "actual_min": actual,
                  "estimate_min": estimate, "ratio": ratio,
                  "rounds": rounds, "outcome": outcome,
                  "host": json_host(target, host), "ended_ms": ended_at,
                  "merge_sha": merge_sha, "wall_min": wall_min}
                 for ticket, actual, estimate, ratio, rounds, outcome, host,
                 ended_at, merge_sha, wall_min in rows],
        "limit": limit,
    }


SHIPPED_LIMIT = 50
SHIPPED_CAP = 200


def shipped(target, query=""):
    """The `/shipped` answer: finished runs newest end first, one page.

    The console's Shipped view is the merge ledger scrolling back over
    older days, and the Board's "shipped today" is its first page; `/runs`
    is the terminal's table, oldest first, and stays that. Each row is the
    run's `id`, `ticket`, `title`, `rounds`, `findings` (the count over its
    review rounds), `started_ms`, `ended_ms`, `actual_min`, `estimate_min`,
    `merge_sha`, `commit_url` (the merge commit's page on `origin` when the
    sha has reached `origin/main`, `commit_url()`), `pr_url` (the pull
    request the run merged through, `runs.prUrl`, null when none) and
    `host`, `outcome` and `outcome_reason` (at most 400 characters).
    `outcome=merged` is the default; `outcome=all` includes every ended run.
    `limit`
    defaults to `SHIPPED_LIMIT` and is capped at `SHIPPED_CAP`;
    `before=RUN_ID` answers the rows that ended before that run (ties by
    id), and `next_before` is the id to pass back for the next page, null
    on the last. A bad `limit`, `before` or `outcome` is 400
    naming it; a `before` no run has is an empty page.
    """
    try:
        limit = parse_limit(query, default=SHIPPED_LIMIT, cap=SHIPPED_CAP)
        before = parse_before(query)
        outcome = parse_filter(query, "outcome", ("merged", "all"))
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        # One past the page tells whether there is a next one.
        runs = store.read.finished_runs(
            conn, limit + 1, before,
            outcomes=None if outcome == "all" else ("merged",))
    finally:
        conn.close()
    more = len(runs) > limit
    runs = runs[:limit]
    origin = origin_web_url(target)
    return 200, {
        "rows": [{"id": run.id, "ticket": run.linearIdentifier,
                  "title": run.title, "rounds": run.reviewRoundCount,
                  "findings": run.findingCount,
                  "started_ms": run.startedAt, "ended_ms": run.endedAt,
                  "actual_min": (effective_work(run, run.endedAt) / 60000
                                 if run.workingMs is not None else None),
                  "working_ms": effective_work(run, run.endedAt),
                  "wall_min": (run.endedAt - run.startedAt) / 60000,
                  "estimate_min": (run.timeBoxMs / 60000
                                   if run.timeBoxMs else None),
                  "merge_sha": run.mergeSha,
                  "commit_url": commit_url(target, run.mergeSha, origin),
                  "outcome": run.outcome,
                  "outcome_reason": (run.outcomeReason[:400]
                                     if run.outcomeReason is not None else None),
                  "pr_url": run.prUrl,
                  "host": json_host(target, run.host)}
                 for run in runs],
        "next_before": runs[-1].id if more else None,
        "limit": limit,
    }


def locate_run(target, text):
    """The run `/runs/N`-style path segment `text` names, for the routes
    under it: `(None, RunDetail)` when there is one, else `(status, body)`
    -- the 400, 503 and 404 the routes share, so each states them once.

    `text` that is not an integer is 400. An integer no run can have
    (negative, or past SQLite's 64-bit INTEGER) is 404 without asking the
    store, which would raise OverflowError binding it; `run` then echoes
    the path as typed, since the id may be too long to be a JSON number.
    An integer with no run is 404 carrying `run` as a number.
    """
    try:
        run_id = parse_run_id(text)
    except ValueError as error:
        return (400, {"error": str(error)}), None
    if not target.store_path.exists():
        return (503, no_store(target)), None
    if run_id is None:
        return (404, {"error": "no such run", "run": text}), None
    conn = store.read.open_readonly(target.store_path)
    try:
        run = store.read.run_detail(conn, run_id)
    finally:
        conn.close()
    if run is None:
        return (404, {"error": "no such run", "run": run_id}), None
    return None, run


def run_detail(target, run_id, now=None):
    """Return `/runs/N`: run clocks, review rounds and narrative events.

    Effective working_ms includes active work through `now`; elapsed_ms is wall
    time, frozen at endedAt. The scaled time box matches `/status`. Live runs
    carry heartbeat age (null after completion) and the recorded review cap;
    old rows use MAX_ROUNDS. locate_run supplies invalid/missing 400/404/503s.
    Rounds and events are oldest first; include implementer_output summaries
    for refusals and no-commit crashes, keeping full payloads in the store."""
    now = int(time() * 1000) if now is None else now
    failed, run = locate_run(target, run_id)
    if failed is not None:
        return failed
    conn = store.read.open_readonly(target.store_path)
    try:
        rounds = store.read.rounds_of(conn, run.id)
        events = store.read.narrative_events(
            conn, run.id, detail_kinds=("implementer_output",))
    finally:
        conn.close()
    live = run.endedAt is None
    # `time_box_ms` is the box the run was counted against -- the estimate
    # scaled by `[agents] budget_scale` -- matching the box `/status` serves.
    scale = budget_scale(target)
    return 200, {
        "run": {"id": run.id, "ticket": run.linearIdentifier,
                "title": run.title, "phase": run.phase,
                "attempt": run.attempt, "started_ms": run.startedAt,
                "ended_ms": run.endedAt, "outcome": run.outcome,
                "elapsed_ms": (run.endedAt if not live else now) - run.startedAt,
                "working_ms": effective_work(run, run.endedAt if not live else now),
                "time_box_ms": (int(run.timeBoxMs * scale)
                                if run.timeBoxMs else run.timeBoxMs),
                "branch": run.branch, "host": json_host(target, run.host),
                "heartbeat_age_ms": now - run.lastHeartbeat if live else None,
                "merge_sha": run.mergeSha,
                "commit_url": commit_url(target, run.mergeSha,
                                         origin_web_url(target)),
                "pr_url": run.prUrl, "work_started_ms": run.workStartedAt,
                # The cap the loop gave this run; a run recorded before the
                # store carried one answers the constant.
                "max_rounds": run.reviewRoundCap or MAX_ROUNDS},
        "rounds": [{"round": r.round, "started_ms": r.startedAt,
                    "ended_ms": r.endedAt, "verdict": r.verdict,
                    "reviewer_model": r.reviewerModel,
                    "findings": json.loads(r.findings)}
                   for r in rounds],
        "findings": [{"tone": "advisory", "message": e.summary}
                     for e in events if e.kind == "bot_finding"],
        "events": [{"at": e.at, "kind": e.kind, "summary": e.summary}
                   for e in events],
    }


def run_ledger(target, run_id):
    """The `/runs/N/ledger` answer: `(http status, JSON-able body)`.

    The run's narrative as the store holds it (design note 9): `entries`
    oldest first, each its `at` in epoch milliseconds, `kind` (one of
    `store.LEDGER_KINDS`), `text` and `source` (`loop` or `operator`), with
    `run_id` and the run's `ticket`. An `intervention` entry also carries
    `cleared` and `waited_ms` (`ledger_entry()`). A merged run with no rows
    answers an empty list. `run_id` parses as on `/runs/N`
    (`locate_run()`): a non-integer is 400, an integer with no run is 404
    carrying `run`.
    """
    failed, run = locate_run(target, run_id)
    if failed is not None:
        return failed
    conn = store.read.open_readonly(target.store_path)
    try:
        entries = store.read.ledger(conn, run.id)
    finally:
        conn.close()
    return 200, {
        "run_id": run.id, "ticket": run.linearIdentifier,
        "entries": [ledger_entry(e, {}) for e in entries],
    }


def ledger_entry(entry, head):
    """One ledger entry as both ledger endpoints spell it: `head`'s
    fields first, then `at`, `kind`, `source` and `text`, and on an
    `intervention` entry `cleared` and `waited_ms` (KO-308) -- what the
    operator's step cleared (`question` or `failed`) and how long that had
    waited, both null when nothing was waiting. The store's rule
    (`store.read._cleared_by()`) decides; the wire only names the fields.
    """
    body = {**head, "at": entry.at, "kind": entry.kind,
            "source": entry.source, "text": entry.text}
    if entry.kind == "intervention":
        body["cleared"] = entry.cleared
        body["waited_ms"] = entry.waitedMs
    return body


LEDGER_LIMIT = 200
LEDGER_CAP = 1000


def ledger(target, query):
    """The `/ledger` answer: `(http status, JSON-able body)`.

    Read entries since the required epoch-ms cursor, narrowed by kind/ticket
    and capped by limit. The Now window also carries ongoing project outages,
    even across midnight, and suppresses their repeated launch interventions.
    Invalid query parameters return 400; absent stores return 503.
    """
    try:
        since = parse_since(query)
        kind = parse_filter(query, "kind", allowed=store.LEDGER_KINDS)
        ticket = parse_filter(query, "ticket")
        limit = parse_limit(query, default=LEDGER_LIMIT, cap=LEDGER_CAP)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        entries = store.read.ledger_since(conn, since, kind=kind,
                                          ticket=ticket, limit=limit,
                                          hide_launch_backoff=True)
        route_rows = (route_down_rows(conn)
                      if ticket is None and kind in (None, "intervention") else [])
    finally:
        conn.close()
    return 200, {
        "entries": [ledger_entry(e, {"run": e.runId, "ticket": e.ticket})
                    for e in entries],
        "active_outages": route_rows, "since": since, "limit": limit,
    }



def route_down_rows(conn):
    """One ongoing outage per project, including one begun before midnight."""
    from store import launch_backoff

    rows = []
    for (project,) in conn.execute(
            "SELECT id FROM projects WHERE launchBackoffReason IS NOT NULL"):
        state = launch_backoff.current(conn, project)
        started = datetime.fromtimestamp(
            state["since"] / 1000, timezone.utc).strftime("%H:%M")
        rows.append({
            "project": project, "run": None, "ticket": None,
            "kind": "route_down", "source": "loop", "at": state["since"],
            "reason": state["reason"],
            "text": f"implementer route down since {started} UTC: {state['reason']}",
        })
    return rows


def run_files(target, run_id):
    """Return paths changed by a run, with status and line counts.

    Live runs use their worktree against main's merge base, including
    uncommitted and untracked files. Ended runs use the merge commit or
    surviving branch. Paths are sorted and capped at files.MAX_FILES.
    Invalid/missing runs return 400/404; missing stores return 503, absent
    ranges 409, and a Git timeout 504."""
    failed, run = locate_run(target, run_id)
    if failed is not None:
        return failed
    worktree = worktree_path(target, run.branch) if run.branch else None
    try:
        touched = touched_files(target.path, run.branch, run.mergeSha,
                                worktree=worktree)
    except RangeError as error:
        return 409, {"error": str(error), "run": run.id}
    except subprocess.TimeoutExpired:
        return 504, {"error": f"git did not answer within {GIT_TIMEOUT}s",
                     "run": run.id}
    return 200, {
        "run": run.id, "base": touched.base, "head": touched.head,
        "files": [{"path": f.path, "status": f.status,
                   "added": f.added, "deleted": f.deleted}
                  for f in touched.files],
        "total_added": touched.total_added,
        "total_deleted": touched.total_deleted,
        "truncated": touched.truncated,
    }


def active_routes(target):
    """Current commands per seat; primary seats carry no fallback marker."""
    from holophyte.agent_routes import active_fallbacks, safe_command
    from holophyte.config import AGENT_CONFIG_KEYS

    fallback = active_fallbacks(target)
    table = {key: safe_command(target, value)
             for key, value in (target.config().get("agents") or {}).items()
             if key in AGENT_CONFIG_KEYS.values() and isinstance(value, str)}
    return {seat: {"command": fallback.get(seat, table.get(seat)),
                   **({"fallback": fallback[seat]} if seat in fallback else {})}
            for seat in AGENT_CONFIG_KEYS.values()}
