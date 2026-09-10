"""`--serve PORT|HOST:PORT`: a read-only HTTP daemon answering `/status`,
`/runs`, `/runs/N`, `/runs/N/files`, `/attention` and `/board` as JSON and
serving the console at `/`.

One `ThreadingHTTPServer` per target, bound to the one address the command
line names -- loopback when it names only a port -- so a drawer on this
machine, or on another host of the private network when a host is given,
can poll the factory without ssh. Every request opens the store through
`store.read.open_readonly()`, reads, and closes it: the daemon never holds a
connection between requests and, short of the opt-in `POST` actions at the
end of this note, never holds a write connection at all, which is why this
module imports `store.read` and, for those actions alone, the operator API
of `store` itself. The handler calls the typed read views and formats JSON; no SQL
lives here, so a later daemon can replace the module wholesale against the
same store. `/runs` is the `--report` table as JSON: the same rows
`report_rows()` prints, in the same order, so a dashboard and the terminal
never disagree about the history. `/runs/N` is one run in full: its row
joined to its ticket, its review rounds with their findings as objects,
and the narrative half of its event stream, so the console's run detail
reads the rounds from the store and never reconstructs them from the
ledger prose. `/runs/N/files` is the paths a run touched with their line
counts, read from git in the target's checkout by `holophyte.files` -- the
one route that asks anything but the store, since git is the truth about
what a branch changed and the daemon is the one process that knows both
the run and the repository. `/attention` is "what needs the
operator": one ordered list of items with a level, computed here where the
store is, so the drawer, a native app and a phone client all show the same
answer and the rule lives in one place rather than in each client.

Every host the body carries passes through `host_label()`, so a configured
`[report] host_label` is what the network sees rather than the machine name.

The console is the static bundle the renderer's build writes to the
repository's own `console/dist/` (`CONSOLE_DIR`, found from this package,
never the target's checkout); `/` and any path no JSON route claims are
answered from it, so opening the daemon's address in a browser is the
console with no second process. Without a built console, `/` is a 404
naming that, and the JSON routes answer as before.

Beyond loopback the bind address stops being a boundary, so `serve()`
resolves a bearer token there: `[serve] token_file` names a file, read
once at startup and held to an owner-only mode, whose contents every JSON
route but `/peers` demands as `Authorization: Bearer ...`, checked in
constant time before any store is opened; a non-loopback bind without the
key is a startup error naming it. `/`, the console's files and `/peers`
stay open so the page can load and learn where its peers are. A loopback
bind ignores the key for its reads. The token is never printed or logged.

`[serve] actions = true` (KO-348) is the one exception to read-only: it
opens three `POST /actions/...` routes behind the token, each a legal
rung of the operator ladder -- `restart-supervisor` and `launch-loop` run
`systemctl --user` against the deploy units named by `[serve] name`, and
`requeue` is `store.requeue()`, what `--requeue KO-n --note TEXT` does.
The actions demand the token on every bind, loopback included -- a bind
address guards reads, not a hand on the units -- so the opt-in needs
`[serve] token_file` and binding without one is a startup error. Each
records its `store.record_intervention()` row before it acts and answers
`{"action", "ok", "detail"}`; a `systemctl` that fails is `ok: false`
carrying its stderr, never a 500, and an action that cannot be recorded
does not run. Off, every `/actions/` path is 404 and this module still
opens no write connection.

`[serve] config_edit = true` (KO-356) opens the target's own `config.toml`
the same way: `GET /config` is the file's text with the value of every key
named `...token` or `...key` replaced by `[redacted]` (`token_file`, a
path, stays), and `PUT /config` takes `{"text": ...}`, puts the current
secret back under every `[redacted]` so a round trip through the page
never blanks one, parses it and runs `config.check_document()` -- the
checks startup runs -- over the parsed document; a refusal is 400 carrying
the loader's own sentence and nothing is written. An accepted document is
written beside a `config.toml.bak-STAMP` copy of the previous text, by
rename, after its `config_edit` interventions row. The routes demand the
token on every bind as the actions do, since a writable config is
`[worktree] setup` and `[agents]` -- commands the next loop start runs --
and the change applies at that start, not to a running loop. Off, both
are 404.

Run the tests: python3 -m unittest discover -s tests -p 'test_serve*' -v
"""
from __future__ import annotations

import dataclasses
import hmac
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from urllib.parse import parse_qs, unquote, urlsplit

import store.read
from holophyte.agents import probe_implementer
from holophyte.config import (
    check_document,
    console_config,
    serve_config,
    split_address,
    sweep_config,
)
from holophyte.files import GIT_TIMEOUT, RangeError, git, touched_files
from holophyte.redact import RedactionError, redact, restore
from holophyte.report import ended_rows, host_label
from holophyte.runs import MAX_ROUNDS, open_store
from holophyte.supervisor import SWEEPABLE_PHASES
from holophyte.target import worktree_path

ADDRESS_SHAPE = "PORT|HOST:PORT"
LOOPBACK = "127.0.0.1"
# The hosts a bind stays open on, as typed: the loopback names and
# addresses (`is_loopback()` adds the rest of 127/8). Anything else, a
# tailnet address or the wildcard included, needs `[serve] token_file`.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "127.1"})
TOKEN_KEY = "[serve] token_file"
# Bits the token file must not carry: anyone but its owner reading it.
TOKEN_FORBIDDEN_MODE = stat.S_IRWXG | stat.S_IRWXO
# The paths the token does not guard: the console page and what it needs
# to load and to learn where the token goes. `/peers` and the static files
# carry no store data.
OPEN_PATHS = frozenset({"/peers"})
# The token-gated `POST` routes `[serve] actions = true` opens (KO-348),
# each the operator-ladder step it maps to. The two unit actions name the
# deploy templates with the `[serve] name` instance appended at request
# time; `systemctl` gets `SYSTEMCTL_TIMEOUT` seconds to answer.
ACTIONS_PREFIX = "/actions/"
# Route name -> (systemctl verb, unit template, interventions action).
UNIT_ACTIONS = {
    "restart-supervisor": ("restart", "holophyte-supervise@",
                           "restart_supervisor"),
    "launch-loop": ("start", "holophyte-loop@", "launch_loop")}
REQUEUE_ACTION = "requeue"
ACTIONS = frozenset(UNIT_ACTIONS) | {REQUEUE_ACTION}
SYSTEMCTL_TIMEOUT = 20
# The note a requeue records when the request carries none: the store
# refuses an empty one, and the CLI's `--note` is the operator's reason.
DEFAULT_REQUEUE_NOTE = "requeued from the console"
# How much JSON a `POST` body may carry; a ticket and a note are far under.
MAX_BODY = 64 * 1024
# `GET /config` and `PUT /config` (KO-356), behind `[serve] config_edit =
# true` and the write token. `holophyte.redact` finds, hides and puts back
# the secret values -- any key whose name ends in `token` or `key`, so
# `token_file` (a path) is left alone. `CONFIG_LOCK` serialises the
# read-restore-validate-backup-replace of a `PUT`: the server is threaded,
# and two writers interleaved could back up the same previous text twice
# and lose one of the two edits without either being told.
CONFIG_PATH = "/config"
CONFIG_ACTION = "config_edit"
CONFIG_APPLIES = "next loop start"
CONFIG_LOCK = threading.Lock()
BACKUP_STAMP = "%Y%m%dT%H%M%SZ"
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# How long a failed run stays on `/attention`: a failure the operator has
# not requeued or merged past in a day is one they have not looked at.
FAILED_WINDOW_MS = 24 * 60 * 60 * 1000
# The console's built bundle: the repository's own, found from this package.
CONSOLE_DIR = Path(__file__).resolve().parent.parent / "console" / "dist"
# Content type by extension for the files under it; anything else is bytes.
CONTENT_TYPES = {".html": "text/html; charset=utf-8",
                 ".js": "text/javascript",
                 ".css": "text/css",
                 ".svg": "image/svg+xml",
                 ".woff2": "font/woff2",
                 ".png": "image/png",
                 ".json": "application/json",
                 ".webmanifest": "application/manifest+json",
                 ".map": "application/json"}
OCTET_STREAM = "application/octet-stream"
# The two `origin` shapes a merge commit can link into: `https://HOST/OWNER/
# REPO(.git)` and `git@HOST:OWNER/REPO(.git)`. Anything else is not a web
# page the daemon can name, so its rows carry no link. Each segment is one
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
# `/tickets/KO-n`: one mirrored ticket by its Linear identifier, body
# included (KO-328). Behind the token like the run routes; `SHAPED_ROUTES`
# below pairs each of these with its handler.
TICKET_PATH = re.compile(r"^/tickets/([^/]+)$")
# The fixed JSON paths that read the store; every one is behind the token.
JSON_PATHS = frozenset({"/status", "/runs", "/shipped", "/ledger",
                        "/attention", "/board"})
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


def parse_address(text):
    """`PORT` or `HOST:PORT` as a `(host, port)` pair; ValueError naming both.

    A bare port binds loopback: there the bind address is the daemon's
    only boundary, so the short form is the safe one, and reaching another
    machine takes typing a host -- and, then, `[serve] token_file`. The port is a
    non-negative integer -- 0 asks the kernel for an ephemeral one, which
    is how the tests bind. With a host, it is whatever precedes the last
    colon, so nothing here decides what a valid hostname is: the bind does.
    The `HOST:PORT` rule is `config.split_address()`'s, the one `[console]
    daemons` entries are held to.
    """
    text = str(text)
    if text.isdecimal():
        return LOOPBACK, int(text)
    try:
        return split_address(text)
    except ValueError:
        raise ValueError(f"--serve takes {ADDRESS_SHAPE} (a non-negative"
                         f" integer port, loopback when no host is given),"
                         f" got {text!r}") from None


def status(target, now=None, started_ms=None):
    """The `/status` answer for `target`: `(http status, JSON-able body)`.

    A target with no store answers 503 rather than creating one -- a
    read-only daemon that wrote an empty store into a home would shadow the
    adoption `open_store()` performs on first need. Ages are computed here
    against `now` (epoch milliseconds, the clock by default) so the client
    compares one number to `thresholds.heartbeat_stale_ms` and never has to
    agree with the writer host about the time. `started_ms` is when the
    serving daemon started, which `StatusServer` reads once at bind and
    passes on every request; it defaults to `now` so a caller with no
    daemon (the tests, a REPL) gets the same shape.

    Each run carries what the console's floor row draws: the ticket's
    `title`, `started_ms` (the run's `startedAt`, so a client need not
    guess it from `elapsed_ms`), `round` (the review rounds recorded so
    far) and `strikes` (the sweep's tally, 0 when the run is not under
    suspicion). `project` is the same string as `target`: the console's
    word for it; the wire carries both for one release. `actions` is
    `[serve] actions`, so the console knows before a click whether the
    `POST /actions/...` routes exist here or its buttons stay disabled.
    """
    now = int(time() * 1000) if now is None else now
    started_ms = now if started_ms is None else started_ms
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        strikes = {run.id: store.read.strike(conn, run.id) for run in runs}
        beat = store.read.supervisor_beat(conn)
    finally:
        conn.close()
    knobs = sweep_config(target)
    return 200, {
        "target": str(target.path),
        "project": str(target.path),
        "host": host_label(target, socket.gethostname()),
        "now": now,
        "daemon": {"started_ms": started_ms, "pid": os.getpid()},
        "supervisor": supervisor_view(target, beat, now, knobs),
        "thresholds": {"heartbeat_stale_ms": knobs.heartbeat_stale_ms,
                       "strikes": knobs.stale_strikes},
        "actions": serve_config(target).actions,
        "runs": [{"id": run.id, "ticket": run.linearIdentifier,
                  "title": run.title,
                  "phase": run.phase,
                  "started_ms": run.startedAt,
                  "heartbeat_age_ms": now - run.lastHeartbeat,
                  "elapsed_ms": now - run.startedAt,
                  "time_box_ms": run.timeBoxMs,
                  "round": run.reviewRoundCount,
                  "strikes": (strikes[run.id].strikes
                              if strikes[run.id] is not None else 0),
                  "host": json_host(target, run.host)}
                 for run in runs],
    }


def supervisor_view(target, beat, now, knobs):
    """`/status`'s `supervisor` object for `beat` (None when none was ever
    written): `live` under the stale threshold, `stale` at or past it."""
    if beat is None:
        return {"state": "none", "pid": None, "heartbeat_age_ms": None,
                "host": None}
    age = now - beat.lastBeat
    return {"state": "live" if age < knobs.heartbeat_stale_ms else "stale",
            "pid": beat.pid, "heartbeat_age_ms": age,
            "host": host_label(target, beat.host)}


PR_OPEN_PREFIX = "PR open:"


def parked_item(ticket):
    """One `blocked_on_operator` ticket as an `/attention` item. A ticket
    whose run has a `prUrl` and whose question opens with `PR open:` -- the
    line `_park_on_pr()` writes first -- is `pr_open`: the run waits on a
    review or a merge, not on an answer, so the item carries the URL and
    the `reason` (the question with that first line removed). Every other
    ticket is `blocked` with its `question`."""
    question = ticket.blockedQuestion or ""
    if ticket.prUrl and question.startswith(PR_OPEN_PREFIX):
        _, _, reason = question.partition("\n")
        return {"kind": "pr_open", "ticket": ticket.linearIdentifier,
                "run": ticket.runId, "pr_url": ticket.prUrl,
                "reason": reason, "asked_ms": ticket.askedMs,
                "level": "attention"}
    return {"kind": "blocked", "ticket": ticket.linearIdentifier,
            "question": ticket.blockedQuestion,
            "run": ticket.runId, "asked_ms": ticket.askedMs,
            "pr_url": ticket.prUrl, "level": "attention"}


def attention(target, now=None):
    """The `/attention` answer: `(http status, JSON-able body)`.

    `items` is what needs the operator, in the order they should read it:
    every ticket parked `blocked_on_operator` with its question, as
    `pr_open` when the park is a pull request waiting on a review or a
    merge (`parked_item()`); every live
    run whose heartbeat age exceeds `heartbeat_stale_ms`; every run that
    ended `failed` within `FAILED_WINDOW_MS` and whose ticket is still
    `in_flight` (a requeue walks it to `ready`, a later attempt merges it,
    and either drops the failure); then the supervisor when it is not
    live. Each item that names a run carries the run's `pr_url`
    (`runs.prUrl`, null when it opened none). Each item carries its
    `level`. `level` on the body is the worst
    over the items -- `attention` when there is any -- else `working` when
    a run is live, else `none`. `critical` is in the enum for a client to
    rank above `attention` (a daemon it cannot reach); nothing here is
    that bad, since the daemon answering is the proof.

    The stale-run and supervisor rules are `/status`'s numbers compared the
    way the drawer compared them: a run is stale strictly past the
    threshold, the supervisor at it.
    """
    now = int(time() * 1000) if now is None else now
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        blocked = store.read.blocked_tickets(conn)
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        failed = store.read.recent_failed_runs(conn, now - FAILED_WINDOW_MS)
        beat = store.read.supervisor_beat(conn)
    finally:
        conn.close()
    knobs = sweep_config(target)
    items = [parked_item(ticket) for ticket in blocked]
    for run in runs:
        age = now - run.lastHeartbeat
        if age > knobs.heartbeat_stale_ms:
            items.append({"kind": "stale_run", "run": run.id,
                          "ticket": run.linearIdentifier, "phase": run.phase,
                          "heartbeat_age_ms": age, "pr_url": run.prUrl,
                          "level": "attention"})
    items.extend({"kind": "failed", "run": run.id,
                  "ticket": run.linearIdentifier, "reason": run.outcomeReason,
                  "ended_ms": run.endedAt, "attempt": run.attempt,
                  "pr_url": run.prUrl, "level": "attention"}
                 for run in failed if run.ticketStatus == "in_flight")
    supervisor = supervisor_view(target, beat, now, knobs)
    if supervisor["state"] != "live":
        items.append({"kind": "supervisor", "state": supervisor["state"],
                      "heartbeat_age_ms": supervisor["heartbeat_age_ms"],
                      "level": "attention"})
    if items:
        level = "attention"
    else:
        level = "working" if runs else "none"
    return 200, {"level": level, "items": items, "now": now,
                 "target": str(target.path), "project": str(target.path)}


# The path to merge, left to right: the columns `/board` answers, in order,
# every one present even when empty. The two terminal statuses are absent.
BOARD_STATES = ("needs_spec", "blocked_on_deps", "ready",
                "blocked_on_operator", "in_flight")


def board(target, now=None):
    """The `/board` answer: `(http status, JSON-able body)`.

    `columns` is one entry per open state in `BOARD_STATES` order, each
    carrying the tickets the store mirrors in that state, ordered by
    identifier: the ticket's `title`, `time_box_ms`, `run` (the active
    run's id, null when none), `question` (the blocked question, null when
    none), `waits_on` (the identifiers of the open tickets its `dependsOn`
    names, empty when none) and `mirrored_ms`. `merged` and `abandoned`
    tickets are absent. The store's mirror is the whole answer: a ticket
    the loop never claimed is not on this board, and nothing here calls
    the provider.
    """
    now = int(time() * 1000) if now is None else now
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        tickets = store.read.open_tickets(conn)
    finally:
        conn.close()
    columns = {state: [] for state in BOARD_STATES}
    for ticket in tickets:
        columns[ticket.status].append({
            "ticket": ticket.linearIdentifier, "title": ticket.title,
            "time_box_ms": ticket.timeBoxMs, "run": ticket.activeRunId,
            "question": ticket.blockedQuestion,
            "waits_on": list(ticket.waitsOn),
            "mirrored_ms": ticket.mirroredAt})
    return 200, {"columns": [{"state": state, "tickets": columns[state]}
                             for state in BOARD_STATES],
                 "now": now}


def ticket_detail(target, identifier):
    """The `/tickets/KO-n` answer: `(http status, JSON-able body)`.

    One mirrored ticket by identifier: `ticket`, `title`, `status`, `body`
    (the Linear text the loop last read at claim, served as it is, not
    rendered), `acceptance_criteria`, `verification_commands`,
    `time_box_ms`, `run` (the active run's id, null when none) and
    `mirrored_ms`. An identifier the store has never mirrored is 404 with
    an empty object, like an absent run. The store's mirror is the whole
    answer; nothing here calls the provider.
    """
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        ticket = store.read.ticket_by_identifier(conn, identifier)
    finally:
        conn.close()
    if ticket is None:
        return 404, {}
    return 200, {"ticket": ticket.linearIdentifier, "title": ticket.title,
                 "status": ticket.status, "body": ticket.body,
                 "acceptance_criteria": list(ticket.acceptanceCriteria),
                 "verification_commands": list(ticket.verificationCommands),
                 "time_box_ms": ticket.timeBoxMs, "run": ticket.activeRunId,
                 "mirrored_ms": ticket.mirroredAt}


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
                  "merge_sha": merge_sha}
                 for ticket, actual, estimate, ratio, rounds, outcome, host,
                 ended_at, merge_sha in rows],
        "limit": limit,
    }


SHIPPED_LIMIT = 50
SHIPPED_CAP = 200


def shipped(target, query=""):
    """The `/shipped` answer: merged runs newest end first, one page.

    The console's Shipped view is the merge ledger scrolling back over
    older days, and the Board's "shipped today" is its first page; `/runs`
    is the terminal's table, oldest first, and stays that. Each row is the
    run's `id`, `ticket`, `title`, `rounds`, `findings` (the count over its
    review rounds), `started_ms`, `ended_ms`, `actual_min`, `estimate_min`,
    `merge_sha`, `commit_url` (the merge commit's page on `origin` when the
    sha has reached `origin/main`, `commit_url()`), `pr_url` (the pull
    request the run merged through, `runs.prUrl`, null when none) and
    `host`. `limit`
    defaults to `SHIPPED_LIMIT` and is capped at `SHIPPED_CAP`;
    `before=RUN_ID` answers the rows that ended before that run (ties by
    id), and `next_before` is the id to pass back for the next page, null
    on the last. A bad `limit` or `before` is 400
    naming it; a `before` no run has is an empty page.
    """
    try:
        limit = parse_limit(query, default=SHIPPED_LIMIT, cap=SHIPPED_CAP)
        before = parse_before(query)
    except ValueError as bad:
        return 400, {"error": str(bad)}
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        # One past the page tells whether there is a next one.
        runs = store.read.merged_runs(conn, limit + 1, before)
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
                  "actual_min": (run.endedAt - run.startedAt) / 60000,
                  "estimate_min": (run.timeBoxMs / 60000
                                   if run.timeBoxMs else None),
                  "merge_sha": run.mergeSha,
                  "commit_url": commit_url(target, run.mergeSha, origin),
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
    """The `/runs/N` answer: `(http status, JSON-able body)`.

    `run` is the row joined to its ticket, with `heartbeat_age_ms` computed
    here against `now` while the run is live and null once it has ended --
    an ended run's heartbeat is history, not a liveness signal -- and
    `max_rounds`, the loop's review-round cap, so a client can say "round 2
    of 3" without knowing the constant, and `commit_url` and `pr_url` as
    `/shipped` carries them. `rounds` is oldest first, each with
    its findings decoded once here into objects; `events` is the narrative
    level of the stream, oldest first, without the detail rows. `run_id`
    that is not an integer is 400; an integer with no run is 404 carrying
    `run` (`locate_run()`).
    """
    now = int(time() * 1000) if now is None else now
    failed, run = locate_run(target, run_id)
    if failed is not None:
        return failed
    conn = store.read.open_readonly(target.store_path)
    try:
        rounds = store.read.rounds_of(conn, run.id)
        events = store.read.narrative_events(conn, run.id)
    finally:
        conn.close()
    live = run.endedAt is None
    return 200, {
        "run": {"id": run.id, "ticket": run.linearIdentifier,
                "title": run.title, "phase": run.phase,
                "attempt": run.attempt, "started_ms": run.startedAt,
                "ended_ms": run.endedAt, "outcome": run.outcome,
                "time_box_ms": run.timeBoxMs, "branch": run.branch,
                "host": json_host(target, run.host),
                "heartbeat_age_ms": now - run.lastHeartbeat if live else None,
                "merge_sha": run.mergeSha,
                "commit_url": commit_url(target, run.mergeSha,
                                         origin_web_url(target)),
                "pr_url": run.prUrl,
                # The cap the loop gave this run; a run recorded before the
                # store carried one answers the constant.
                "max_rounds": run.reviewRoundCap or MAX_ROUNDS},
        "rounds": [{"round": r.round, "started_ms": r.startedAt,
                    "ended_ms": r.endedAt, "verdict": r.verdict,
                    "reviewer_model": r.reviewerModel,
                    "findings": json.loads(r.findings)}
                   for r in rounds],
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

    The ledger across runs, newest first, from `since` (epoch
    milliseconds, required) on: the console's "resolved today" fold is one
    window over the store's ledger table, and a blocked ticket's thread is
    the same window narrowed with `ticket=KO-n` to the entries since the
    question was asked -- the `intervention` rows carry the operator's
    answer. `kind` narrows to one of `store.LEDGER_KINDS`. `limit` defaults
    to `LEDGER_LIMIT` and is capped at `LEDGER_CAP`. Each entry is its
    `at`, `run`, `ticket`, `kind`, `source` and `text`, as `/runs/N/ledger`
    spells them, an `intervention` entry with `cleared` and `waited_ms`
    too (`ledger_entry()`). A missing or non-integer `since`, a bad `limit` or an
    unknown `kind` is 400 naming the parameter.
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
                                          ticket=ticket, limit=limit)
    finally:
        conn.close()
    return 200, {
        "entries": [ledger_entry(e, {"run": e.runId, "ticket": e.ticket})
                    for e in entries],
        "since": since, "limit": limit,
    }


def run_files(target, run_id):
    """The `/runs/N/files` answer: `(http status, JSON-able body)`.

    The paths the run touched with a status letter and line counts, from
    `holophyte.files.touched_files()`: a merged run's merge commit against
    its first parent in the target's checkout; a live run's worktree (found
    from its branch as the loop names it, `target.worktree_path()`) against
    the merge base with main, uncommitted edits and untracked files
    included, an empty list when nothing changed yet; a run whose branch
    survives without a worktree, that branch against its merge base.
    `files` is sorted by path and capped at `files.MAX_FILES` with
    `truncated` set past that; the totals are over the whole diff. 400, 404
    and 503 as `/runs/N`; 409 carrying `error` when the run has no range to
    diff (no branch and no merge sha, or a branch with neither a worktree
    nor a ref); 504 when git outlives its cap.
    """
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


def parse_action_body(raw):
    """The `POST /actions/...` body as a dict; ValueError when it is not
    JSON, not an object, or past `MAX_BODY`. An empty body is `{}`."""
    if len(raw) > MAX_BODY:
        raise ValueError(f"body must be under {MAX_BODY} bytes")
    if not raw.strip():
        return {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValueError("body must be JSON") from None
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    return body


def unit_action(target, action, unit_name):
    """Run the `systemctl --user` step `action` names against the unit
    instance `unit_name`: `(http status, JSON-able body)`.

    The interventions row lands first (`store.record_intervention()`, the
    operator ladder's record-before-acting call), on the store's newest run
    (`store.read.newest_run_id()`) since interventions are keyed by run. A
    target with no store, or a store with no run yet, has nothing to record
    against and the step does not run: 200 with `ok: false` saying so,
    since an unrecorded hand on the units is what the ladder forbids.
    `systemctl` exiting non-zero, being absent or outliving
    `SYSTEMCTL_TIMEOUT` is 200 with `ok: false` and the reason in
    `detail`: the operator asked for a thing and is told what happened,
    which is not a server error.
    """
    verb, template, intervention = UNIT_ACTIONS[action]
    unit = template + unit_name
    argv = ["systemctl", "--user", verb, unit]
    note = f"operator asked the daemon to {verb} {unit} (POST /actions/{action})"
    recorded = record_action_intervention(target, intervention, note)
    if recorded is None:
        detail = ("the store holds no run to record the intervention"
                  " against; nothing run")
        return 200, {"action": action, "ok": False, "detail": detail,
                     "unit": unit, "recorded": None}
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT)
    except FileNotFoundError:
        detail = "systemctl is not on this host"
        return 200, {"action": action, "ok": False, "detail": detail,
                     "unit": unit, "recorded": recorded}
    except subprocess.TimeoutExpired:
        detail = f"systemctl did not answer within {SYSTEMCTL_TIMEOUT}s"
        return 200, {"action": action, "ok": False, "detail": detail,
                     "unit": unit, "recorded": recorded}
    ok = done.returncode == 0
    detail = (f"{' '.join(argv)} exited 0" if ok
              else (done.stderr or done.stdout or "").strip()
              or f"{' '.join(argv)} exited {done.returncode}")
    return 200, {"action": action, "ok": ok, "detail": detail,
                 "unit": unit, "recorded": recorded}


def record_action_intervention(target, action, note):
    """Record the human `action` with `note` as an interventions row on the
    store's newest run, before the step it describes; the run's id, or None
    when the store does not exist or holds no run to record against."""
    if not target.store_path.exists():
        return None
    conn = open_store(target)
    try:
        run_id = store.read.newest_run_id(conn)
        if run_id is not None:
            store.record_intervention(conn, run_id, action, note,
                                      source="human", trigger="manual")
    finally:
        conn.close()
    return run_id


def tickets_named(conn, identifier):
    """How many mirrored tickets carry the Linear identifier `identifier`.

    `linearIdentifier` is not unique in the store -- `linearIssueId` is --
    so the same check `--requeue` makes before it writes: an identifier
    the store holds more than once names nobody, and nothing is written.
    """
    (count,) = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE linearIdentifier = ?",
        (identifier,)).fetchone()
    return count


def requeue_action(target, body):
    """`POST /actions/requeue`: `store.requeue()` on the ticket `body`
    names, with `note` or `DEFAULT_REQUEUE_NOTE`: `(http status, JSON-able
    body)`.

    The store's one transaction is the whole write -- the `requeue`
    interventions row carrying the note and the ticket walked to `ready`
    -- exactly what `--requeue KO-n --note TEXT` does. A missing or
    non-string `ticket` is 400; a store the target does not have is 503;
    a ticket the store never mirrored, one it holds more than once (the
    CLI refuses to pick one; so does the route), or one the store refuses
    to requeue (a live run, not `in_flight`, its last run not `failed`),
    is 200 with `ok: false` and the refusal in `detail`, nothing written.
    """
    action = REQUEUE_ACTION
    identifier = body.get("ticket")
    if not isinstance(identifier, str) or not identifier.strip():
        return 400, {"error": "ticket must name a mirrored ticket (KO-n)"}
    note = body.get("note", DEFAULT_REQUEUE_NOTE)
    if not isinstance(note, str) or not note.strip():
        note = DEFAULT_REQUEUE_NOTE
    identifier = identifier.strip()
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = open_store(target)
    try:
        ticket = store.read.ticket_by_identifier(conn, identifier)
        if ticket is None:
            return 200, {"action": action, "ok": False, "ticket": identifier,
                         "detail": f"{identifier}: no such ticket in the store"}
        named = tickets_named(conn, identifier)
        if named > 1:
            return 200, {"action": action, "ok": False, "ticket": identifier,
                         "detail": f"{identifier} names {named} tickets in the"
                                   " store; refusing to pick one"}
        try:
            run_id = store.requeue(conn, ticket.id, note)
        except (store.RequeueRefused, ValueError) as refused:
            return 200, {"action": action, "ok": False, "ticket": identifier,
                         "detail": str(refused)}
    finally:
        conn.close()
    return 200, {"action": action, "ok": True, "ticket": identifier,
                 "detail": f"{identifier} requeued after run {run_id}",
                 "run": run_id}


def config_text(target):
    """The target's config file as written, `""` when there is none yet."""
    try:
        return target.config_path.read_text()
    except FileNotFoundError:
        return ""


def read_config(target):
    """`GET /config`: the file's text, secrets redacted, its path, and when
    a change to it applies. A text `redact()` cannot vouch for is 500 with
    its sentence and no text: better no page than a secret on it."""
    try:
        text = redact(config_text(target))
    except RedactionError as bad:
        return 500, {"error": str(bad)}
    return 200, {"text": text, "path": str(target.config_path),
                 "applies": CONFIG_APPLIES}


def validate_config(target, text):
    """Hold `text` to what startup would accept for `target`: parsed as
    TOML and run through `config.check_document()` on a copy of the target
    carrying the parsed document instead of the file's. The refusal, when
    there is one, is the loader's own sentence -- naming the file, the
    table and the key -- or `tomllib`'s; None when the document passes."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as bad:
        return f"malformed TOML: {bad}"
    candidate = dataclasses.replace(target, _config=document)
    try:
        check_document(candidate)
    except SystemExit as refused:
        return str(refused)
    return None


def write_config(target, body, now=None):
    """`PUT /config`: replace the target's config with `body["text"]`
    after validating it: `(http status, JSON-able body)`.

    `text` must be a string, 400 otherwise. Every `[redacted]` value in it
    is the current file's (`restore()`) before anything is judged, so the
    text validated and written is the whole document. A document
    `validate_config()` refuses is 400 with its sentence as `error`,
    nothing written. The `config_edit` interventions row lands first, on
    the store's newest run as the actions record theirs; a target with no
    store or no run has nothing to record against and the file is left
    alone, 503 saying so. Then the previous text is copied to
    `config.toml.bak-STAMP` beside the file (`-2`, `-3` when the second
    has a sibling already; none when there was no file) and the new text
    lands by rename from a staging file of its own, so a reader sees the
    old file or the new one and never a torn one. One `PUT` at a time
    holds `CONFIG_LOCK` from the read to the rename. The reply names the
    backup.

    A write that changes `[agents] implementer` is followed by the probe
    the loop runs at startup (KO-357): `probe_implementer()` on the
    document as written, outside the lock, its result under `probe` in
    the reply -- `null` when the key did not change or now names no route.
    The probe reports; it does not gate: the file is already replaced and
    backed up when it runs, so a route that does not answer is a `200`
    whose `probe.ok` is false, the same text the next loop start refuses
    with, and the operator fixes the key or restores the backup.
    """
    text = body.get("text")
    if not isinstance(text, str):
        return 400, {"ok": False, "error": "text must be the file's new"
                                            " contents as a string"}
    with CONFIG_LOCK:
        current = config_text(target)
        code, reply, written = _write_config(target, text, now, current)
    if written is not None:
        reply["probe"] = probe_changed_implementer(target, current, written)
    return code, reply


def implementer_of(text):
    """`[agents] implementer` in `text`, None when unset or when the text
    does not parse: a file the loop would refuse names no route."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    agents = document.get("agents")
    return agents.get("implementer") if isinstance(agents, dict) else None


def probe_changed_implementer(target, before, after):
    """`probe_implementer().to_json()` for the `after` document when its
    `[agents] implementer` differs from `before`'s; None otherwise. The
    probed target carries `after` parsed, not the file: the `Target`
    the server was bound with read its config once, at bind."""
    if implementer_of(after) == implementer_of(before):
        return None
    candidate = dataclasses.replace(target, _config=tomllib.loads(after))
    result = probe_implementer(candidate)
    return None if result is None else result.to_json()


def _write_config(target, text, now, current):
    """`(status, reply, written)` under the lock: `written` is the text
    on disk after a `200`, None when nothing was."""
    try:
        text = restore(text, current)
    except ValueError as bad:
        return 400, {"ok": False, "error": str(bad)}, None
    refused = validate_config(target, text)
    if refused is not None:
        return 400, {"ok": False, "error": refused}, None
    path = target.config_path
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if path.exists():
        stamp = (now or datetime.now(timezone.utc)).strftime(BACKUP_STAMP)
        backup = next_backup(path, stamp)
    note = (f"operator replaced {path} from the console (PUT /config);"
            f" applies at the {CONFIG_APPLIES}; previous text in "
            + (str(backup) if backup else "no backup: there was no file"))
    recorded = record_action_intervention(target, CONFIG_ACTION, note)
    if recorded is None:
        return 503, {"ok": False, "error": "the store holds no run to record"
                                            " the intervention against;"
                                            " nothing written"}, None
    if backup is not None:
        # The backup holds the same secrets as the file: it is born with
        # the file's mode, never the umask's default for a new file.
        mode = stat.S_IMODE(path.stat().st_mode)
        handle = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(handle, "w") as out:
            os.fchmod(handle, mode)
            out.write(current)
    handle, staging = tempfile.mkstemp(prefix=f"{path.name}.new-",
                                       dir=path.parent)
    with os.fdopen(handle, "w") as out:
        out.write(text)
    if backup is not None:
        os.chmod(staging, stat.S_IMODE(path.stat().st_mode))
    os.replace(staging, path)
    return 200, {"ok": True, "path": str(path),
                 "backup": None if backup is None else str(backup),
                 "applies": CONFIG_APPLIES, "recorded": recorded}, text


def next_backup(path, stamp):
    """`path.bak-STAMP`, or the first of `-2`, `-3`, ... that does not
    exist yet: two writes in one second keep two backups."""
    first = path.with_name(f"{path.name}.bak-{stamp}")
    if not first.exists():
        return first
    n = 2
    while True:
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{n}")
        if not candidate.exists():
            return candidate
        n += 1


def is_loopback(host):
    """Whether a bind `host` reaches this machine only.

    A name is judged as typed, not resolved: `localhost` is loopback and a
    hostname that happens to resolve there is not, since what the operator
    wrote is what the daemon can be sure of. `127.0.0.0/8` as a whole is
    loopback too.
    """
    if host in LOOPBACK_HOSTS:
        return True
    try:
        packed = socket.inet_pton(socket.AF_INET, host)
    except OSError:
        return False
    return packed[0] == 127


def load_token(path):
    """The token file's contents, whitespace-stripped; SystemExit otherwise.

    The file must exist, be a regular file, carry something, and be
    readable by its owner alone: a group- or world-readable token is one
    the daemon refuses to serve behind, and the refusal names the mode so
    the operator can see the bit to drop. The token itself is never
    printed.
    """
    path = Path(path)
    try:
        info = path.stat()
    except OSError as error:
        raise SystemExit(f"[holo2] {TOKEN_KEY}: {path}: {error.strerror}")
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"[holo2] {TOKEN_KEY}: {path} is not a regular file")
    if info.st_mode & TOKEN_FORBIDDEN_MODE:
        raise SystemExit(
            f"[holo2] {TOKEN_KEY}: {path} is mode {stat.S_IMODE(info.st_mode):04o};"
            " it must not be group- or world-readable (chmod 600)")
    token = path.read_text().strip()
    if not token:
        raise SystemExit(f"[holo2] {TOKEN_KEY}: {path} is empty")
    return token


def resolve_token(target, host):
    """The token `--serve` on `host` needs, or None when the bind is loopback.

    A non-loopback bind with no `[serve] token_file` configured exits
    naming the key: the bind address stops being the boundary the moment
    it is not loopback, and a daemon that answered anyway would be open to
    the whole network by default. A loopback bind ignores the key even
    when it is set.
    """
    if is_loopback(host):
        return None
    token_file = serve_config(target).token_file
    if token_file is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: --serve {host} binds beyond"
            f" loopback, which needs {TOKEN_KEY} = \"PATH\" naming a"
            " file whose contents every request presents as"
            " `Authorization: Bearer ...`")
    return load_token(token_file)


def resolve_action_token(target, knobs, token):
    """The token `POST /actions/...` and the `/config` routes demand, or
    None when neither `[serve] actions` nor `[serve] config_edit` is on.

    `token` is what `resolve_token()` gave the bind: on a non-loopback bind
    it is already the file's contents and the write routes share it. A
    loopback bind has none, and the write routes do not inherit its
    openness -- the bind address guards reads, not a hand on the units or
    the config -- so the file is read for them alone, and either opt-in
    without `[serve] token_file` exits naming the keys rather than binding
    open.
    """
    on = [key for key, flag in (("actions", knobs.actions),
                                ("config_edit", knobs.config_edit)) if flag]
    if not on:
        return None
    if token is not None:
        return token
    if knobs.token_file is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: [serve] {' and '.join(on)} = true"
            f" needs {TOKEN_KEY} = \"PATH\" on every bind, loopback"
            " included: the routes it opens answer only to"
            " `Authorization: Bearer ...`")
    return load_token(knobs.token_file)


def authorized(header, token):
    """Whether `header` is exactly `Bearer TOKEN`, compared in constant time.

    Any other scheme, a missing header, a wrong or a truncated value are
    all one answer, so the check leaks nothing about how close the guess
    came.
    """
    scheme, _, value = (header or "").partition(" ")
    if scheme != "Bearer":
        return False
    return hmac.compare_digest(value.strip().encode(), token.encode())


def static_file(console_dir, path):
    """The console file for request `path`: `(bytes, content type)`, or
    `(404 status, JSON body)` when there is none to serve.

    `/` is `index.html`. The path is percent-decoded and resolved under
    `console_dir`, symlinks followed, and refused unless the result is a
    regular file inside the directory: `..`, an encoded `..`, an absolute
    path and a symlink pointing out are all the plain 404, indistinguishable
    from a missing file. With no `console_dir` at all the 404 says the
    console is not built, so a daemon on a host without the renderer's
    toolchain still answers its JSON and says why `/` does not.
    """
    if not console_dir.is_dir():
        return 404, {"error": "not found", "path": path,
                     "detail": "the console is not built: "
                               f"{console_dir} does not exist"}
    relative = unquote(path).lstrip("/") or "index.html"
    root = console_dir.resolve()
    try:
        file = (root / relative).resolve()
    except OSError:
        file = None
    if file is None or not file.is_relative_to(root) or not file.is_file():
        return 404, {"error": "not found", "path": path}
    return file.read_bytes(), CONTENT_TYPES.get(file.suffix, OCTET_STREAM)


# The JSON routes with a path segment to capture, each with the handler
# that takes `(target, segment)`. Every one is behind the token; `dispatch`
# tries them in this order after the fixed paths.
SHAPED_ROUTES = (
    (RUN_PATH, run_detail),
    (RUN_FILES_PATH, run_files),
    (RUN_LEDGER_PATH, run_ledger),
    (TICKET_PATH, ticket_detail),
)


def shaped_route(path):
    """`(handler, captured segment)` for the `SHAPED_ROUTES` entry `path`
    matches, or None when none does."""
    for shape, handler in SHAPED_ROUTES:
        match = shape.match(path)
        if match is not None:
            return handler, match.group(1)
    return None


class StatusHandler(BaseHTTPRequestHandler):
    """`GET /status`, `GET /runs`, `GET /runs/N`, `GET /runs/N/files`,
    `GET /attention`, `GET /board` and `GET /peers` as JSON; any other GET
    is a console file under the server's `console_dir` or 404 JSON; 405
    otherwise.

    "Otherwise" is every other method, HEAD and OPTIONS included: a client
    that speaks anything but GET gets a JSON refusal it can parse, never
    the library's HTML 501 page.

    Every answer is JSON with `Cache-Control: no-store`, the error ones
    included, so a client can parse whatever comes back, and carries
    `Access-Control-Allow-Origin: *`: the console page is served by one
    daemon and fetches the others from the browser, which refuses a
    cross-origin answer without the header. The daemon is read-only and,
    beyond loopback, behind the bearer token `serve()` resolved from
    `[serve] token_file`: with one set, every JSON route but `/peers` is
    401 without it, checked before any store is opened; `/` and the
    console's files stay open so the page can load. The default access
    log to stderr is silenced: the daemon shares a terminal with the loop,
    and a line per poll would bury the lines that matter.
    """

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        # The token check comes before any route reads the store, and after
        # the question of which routes are open: a 401 touches nothing.
        # `/config` demands the write token on every bind, as the actions
        # do, when the opt-in is on; off, it is 404 behind the read token.
        token = (self.server.action_token if path == CONFIG_PATH
                 and self.server.config_edit else self.server.token)
        if token is not None and not self.open_route(path) \
                and not authorized(self.headers.get("Authorization"), token):
            return self.answer(401, {})
        self.dispatch(path, parts.query)

    def dispatch(self, path, query):
        """Answer `path` from its route: the JSON ones by name, `/peers`
        from config, anything else as a console file."""
        if path == "/status":
            code, body = status(self.server.target,
                                started_ms=self.server.started_ms)
        elif path == "/runs":
            code, body = runs(self.server.target, query)
        elif path == "/shipped":
            code, body = shipped(self.server.target, query)
        elif path == "/ledger":
            code, body = ledger(self.server.target, query)
        elif path == "/attention":
            code, body = attention(self.server.target)
        elif path == "/board":
            code, body = board(self.server.target)
        elif path == "/peers":
            code, body = 200, {"self": self.server.self_address,
                               "peers": list(self.server.peers)}
        elif path == CONFIG_PATH:
            code, body = ((404, {"error": "not found", "path": path})
                          if not self.server.config_edit
                          else read_config(self.server.target))
        elif (shaped := shaped_route(path)) is not None:
            handler, segment = shaped
            code, body = handler(self.server.target, segment)
        else:
            found = static_file(self.server.console_dir, path)
            if isinstance(found[0], bytes):
                return self.answer_bytes(*found)
            code, body = found
        self.answer(code, body)

    def do_POST(self):
        """`POST /actions/NAME` when `[serve] actions = true`; 405 on any
        other path, as every non-GET method is.

        The order is token, then opt-in, then route: a 401 touches nothing
        and tells an unauthenticated client nothing about whether actions
        are on; a daemon without the opt-in is 404 on every `/actions/`
        path, the token notwithstanding; an unknown action under the prefix
        is 404 too. The token is the actions' own (`resolve_action_token()`),
        demanded on a loopback bind as much as any other; with actions off
        the bind's read token applies, so a non-loopback daemon is 401
        before it is 404. The body is JSON (`parse_action_body()`), 400
        when it is not.
        """
        path = urlsplit(self.path).path
        if not path.startswith(ACTIONS_PREFIX):
            return self.refuse()
        token = (self.server.action_token if self.server.actions
                 else self.server.token)
        if token is not None and not authorized(
                self.headers.get("Authorization"), token):
            return self.answer(401, {})
        action = path[len(ACTIONS_PREFIX):]
        if not self.server.actions or action not in ACTIONS:
            return self.answer(404, {"error": "not found", "path": path})
        try:
            body = self.read_body()
        except ValueError as bad:
            return self.answer(400, {"error": str(bad)})
        if action == REQUEUE_ACTION:
            code, body = requeue_action(self.server.target, body)
        else:
            code, body = unit_action(self.server.target, action,
                                     self.server.unit_name)
        self.answer(code, body)

    def do_PUT(self):
        """`PUT /config` when `[serve] config_edit = true`; 405 on any
        other path. Token, then opt-in, then body, in the actions' order
        and for their reasons: the write token on every bind, 404 without
        the opt-in whatever the token, 400 for a body that is not a JSON
        object; `write_config()` judges the text."""
        path = urlsplit(self.path).path
        if path != CONFIG_PATH:
            return self.refuse()
        token = (self.server.action_token if self.server.config_edit
                 else self.server.token)
        if token is not None and not authorized(
                self.headers.get("Authorization"), token):
            return self.answer(401, {})
        if not self.server.config_edit:
            return self.answer(404, {"error": "not found", "path": path})
        try:
            body = self.read_body()
        except ValueError as bad:
            return self.answer(400, {"ok": False, "error": str(bad)})
        self.answer(*write_config(self.server.target, body))

    def read_body(self):
        """The request body as a JSON object (`parse_action_body()`);
        ValueError past `MAX_BODY` or when it is not one."""
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_BODY:
            raise ValueError(f"body must be under {MAX_BODY} bytes")
        return parse_action_body(self.rfile.read(length))

    def open_route(self, path):
        """Whether `path` is served without the token: `/peers` and the
        console's files -- `/`, and any path no JSON route claims. The
        JSON routes are named before the static branch in `do_GET`, so a
        file that shadows one is not an opening; the `/actions/` prefix is
        never one either."""
        if path in OPEN_PATHS:
            return True
        if path in JSON_PATHS or path == CONFIG_PATH \
                or path.startswith(ACTIONS_PREFIX):
            return False
        return shaped_route(path) is None

    def refuse(self):
        self.answer(405, {"error": "method not allowed",
                          "method": self.command,
                          "path": self.path.split("?")[0]},
                    allow="GET")

    def do_OPTIONS(self):
        # A CORS preflight: the browser asks, before a cross-origin GET
        # carrying `Authorization` -- or the console's `POST /actions/...`
        # or `PUT /config` carrying it and a JSON `Content-Type` -- whether
        # it may send it.
        # The answer is the same on every path, never carries credentials,
        # discloses nothing and reads nothing, so it runs without the
        # bearer check; the POST itself is still refused without one.
        self.answer_bytes(b"", "application/json", code=204, extra=[
            ("Access-Control-Allow-Methods", "GET, POST, PUT"),
            ("Access-Control-Allow-Headers",
             "authorization, accept, content-type"),
            ("Access-Control-Max-Age", "600"),
        ])

    def __getattr__(self, name):
        # `BaseHTTPRequestHandler` dispatches on `do_<METHOD>` and answers
        # 501 HTML when the attribute is missing; here every method but GET,
        # POST and OPTIONS is the same 405 JSON, whether or not the RFC
        # names it.
        if name.startswith("do_"):
            return self.refuse
        raise AttributeError(name)

    def answer(self, code, body, allow=None):
        self.answer_bytes(json.dumps(body).encode(), "application/json",
                          code=code, allow=allow)

    def answer_bytes(self, payload, content_type, code=200, allow=None,
                     extra=()):
        """Send `payload` as `content_type` with the headers every answer
        carries -- the open origin included, so a page served by one
        daemon can read another's -- plus `Allow` when given and any
        `(name, value)` pairs in `extra`."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(payload)))
        if allow is not None:
            self.send_header("Allow", allow)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


class StatusServer(ThreadingHTTPServer):
    """The bound server, carrying the one target its handler answers for,
    the directory it serves the console from, the moment it was bound,
    which `/status` reports as the daemon's `started_ms`, and what `/peers`
    answers: the target's `[console] daemons`, read once at bind, and the
    address the daemon bound as `HOST:PORT` -- the label it announces, so a
    page loaded from it can tell this daemon from the peers, not the
    machine's name. `actions` and `unit_name` are `[serve] actions` and
    `[serve] name`, read once at bind too: whether `POST /actions/...`
    answers and which unit instance it addresses; `config_edit` is `[serve]
    config_edit`, whether the `/config` routes answer; `action_token` is the
    bearer value those routes demand on every bind, resolved at bind from
    `[serve] token_file` when the read `token` is None, so a loopback
    daemon with actions on exits at bind without the key rather than
    answering them open."""

    daemon_threads = True

    def __init__(self, target, address, console_dir=CONSOLE_DIR, token=None):
        self.target = target
        self.console_dir = Path(console_dir)
        self.token = token
        self.peers = console_config(target).daemons
        knobs = serve_config(target)
        self.actions = knobs.actions
        self.config_edit = knobs.config_edit
        self.unit_name = knobs.name
        self.action_token = resolve_action_token(target, knobs, token)
        self.started_ms = int(time() * 1000)
        super().__init__(address, StatusHandler)
        host, port = self.server_address[:2]
        self.self_address = f"{host}:{port}"


def make_server(target, host, port, console_dir=CONSOLE_DIR, token=None):
    """Bind a `StatusServer` for `target` at `host:port` and return it.

    Port 0 binds an ephemeral port; the address actually bound is
    `server.server_address`. The caller runs `serve_forever()` and closes it.
    `console_dir` is where `/` is served from -- the repository's own
    `console/dist/` unless a test points it elsewhere. `token`, when given,
    is the bearer value every JSON route demands; `serve()` resolves it
    from the bind address and the config through `resolve_token()`.
    """
    return StatusServer(target, (host, port), console_dir, token)


class _Stopped(Exception):
    """Raised inside `serve_forever()` by the signal handler to unwind it."""


def serve(target, address, out=None):
    """`--serve`'s whole body: bind, announce, answer until SIGINT/SIGTERM.

    The handler for the stop signals raises out of `serve_forever()` rather
    than calling `shutdown()`: `shutdown()` waits for the serving loop to
    notice, and the loop is the thread the signal interrupted.
    """
    out = out or sys.stdout
    host, port = parse_address(address)
    token = resolve_token(target, host)
    server = make_server(target, host, port, token=token)

    def on_signal(signum, _frame):
        raise _Stopped(signum)

    previous = {signum: signal.signal(signum, on_signal)
                for signum in STOP_SIGNALS}
    try:
        bound_host, bound_port = server.server_address[:2]
        guard = "open" if token is None else "behind a bearer token"
        opened = [name for name, on in (("actions", server.actions),
                                        ("config edit", server.config_edit))
                  if on]
        mode = f"with {' and '.join(opened)}" if opened else "read-only"
        print(f"[holo2] serving {bound_host}:{bound_port} {mode} for"
              f" {target.path}, {guard}", file=out)
        try:
            server.serve_forever()
        except _Stopped:
            print("[holo2] serve stopping on signal", file=out)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        server.server_close()
    return 0
