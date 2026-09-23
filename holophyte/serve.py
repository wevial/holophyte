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
`[serve] machine_token_file` (KO-647) names a second file, held to the
same rules and read wherever `token_file` is: one token for every daemon
on the machine, accepted beside the project's own on every route that
demands a bearer, so a single project can still be shared without it.

`[serve] actions = true` (KO-348) is the one exception to read-only: it
opens `POST /actions/...` routes behind the token, each a legal rung of
the operator ladder -- `restart-supervisor` and `launch-loop` run
`systemctl --user` against the deploy units named by `[serve] name`, and
`requeue` is `store.requeue()`, what `--requeue KO-n --note TEXT` does.
`send-back` releases a parked candidate with a private maintainer note;
`hold`, `release-hold`, `pause` and `resume` are `holophyte.serve_levers`.
The actions demand the token on every bind, loopback included -- a bind
address guards reads, not a hand on the units -- so the opt-in needs
`[serve] token_file`. Each records its interventions row before it acts
and answers `{"action", "ok", "detail"}` (send-back: `{"ok", "run",
"event_id"}`); a failed `systemctl` or a store refusal is `ok: false`,
never a 500, and an action that cannot be recorded does not run. Off,
every `/actions/` path is 404 and no write connection is opened.

`[serve] config_edit = true` (KO-356) opens the target's own `config.toml`
the same way: `GET /config` is the file's text with the value of every key
named `...token` or `...key` replaced by `[redacted]` (`token_file`, a
path, stays), and `PUT /config` takes `{"text": ...}`, puts the current
secret back under every `[redacted]` so a round trip through the page
never blanks one, parses it and runs `config.check_document()` -- the
checks startup runs -- over the parsed document; a refusal is 400 carrying
the loader's own sentence and nothing is written. An accepted document is
written beside a `config.toml.bak-STAMP` copy of the previous text, by
rename, after its `config_edit` interventions row. `GET /config` also
carries the redacted text parsed as `values`, and `PUT /config` takes
`{"patch": {"loop.workers": 3, ...}}` instead of `text` (KO-364): the
file edited in place with `tomlkit` -- comments, order and layout kept --
then held, recorded, backed up and written as a text is, so the console
never parses TOML. `tomlkit` is the factory's one dependency
(`requirements.txt`); a daemon started without it exits naming it. The
routes demand the
token on every bind as the actions do, since a writable config is
`[worktree] setup` and `[agents]` -- commands the next loop start runs --
and the change applies at that start, not to a running loop. Off, both
are 404.

The daemon follows the factory code as the supervisor does (KO-648): it
records the `factory_revision()` it started from and, every
`CODE_CHECK_SEC` between requests, reads the checkout's `HEAD` again. When
the two differ it stops accepting, lets the requests in flight finish,
closes its socket and re-executes itself through `reexec_self()` with the
same command line, so it binds the same address again -- the port typed,
not the one an ephemeral `:0` happened to get. A checkout whose `HEAD`
cannot be read logs that once and keeps serving the build it has.

Run the tests: python3 -m unittest discover -s tests -p 'test_serve*' -v
"""
from __future__ import annotations

import hmac
import json
import os
import re
import signal
import socket
import stat
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from urllib.parse import unquote, urlsplit

import store.read
from holophyte.config import (
    budget_scale,
    console_config,
    serve_config,
)
from holophyte.config_tables import (
    split_address,
    sweep_config,
)
from holophyte.pr_status import PR_URL_RE
from holophyte.reexec import reexec_self
from holophyte.report import host_label
from holophyte.serve_actions import (
    ACTIONS,
    ACTIONS_PREFIX,
    MAX_BODY,
    REQUEUE_ACTION,
    action_failure,
    parse_action_body,
    requeue_action,
    send_back_action,
    unit_action,
)
from holophyte.serve_config import (
    CONFIG_PATH,
    read_config,
    require_tomlkit,
    write_config,
)
from holophyte.serve_levers import LEVERS, paused_item
from holophyte.serve_runs import (
    RUN_FILES_PATH,
    RUN_LEDGER_PATH,
    RUN_PATH,
    RUN_TRANSCRIPT_PATH,
    RUN_TURNS_PATH,
    json_host,
    ledger,
    no_store,
    run_detail,
    run_files,
    run_ledger,
    run_transcript,
    run_turns,
    runs,
    shipped,
)
from holophyte.serve_watch import CODE_CHECK_SEC, CodeWatch, InFlight, Moved
from holophyte.supervisor import SWEEPABLE_PHASES, factory_revision
from store.working import agent_work, effective_work, verify_work

ADDRESS_SHAPE = "PORT|HOST:PORT"
LOOPBACK = "127.0.0.1"
# The hosts a bind stays open on, as typed: the loopback names and
# addresses (`is_loopback()` adds the rest of 127/8). Anything else, a
# tailnet address or the wildcard included, needs `[serve] token_file`.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "127.1"})
TOKEN_KEY = "[serve] token_file"
MACHINE_TOKEN_KEY = "[serve] machine_token_file"
# Bits the token file must not carry: anyone but its owner reading it.
TOKEN_FORBIDDEN_MODE = stat.S_IRWXG | stat.S_IRWXO
# The paths the token does not guard: the console page and what it needs
# to load and to learn where the token goes. `/peers` and the static files
# carry no store data.
OPEN_PATHS = frozenset({"/peers"})
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# The re-exec seam, as the supervisor's: a test patches it and the test
# runner is never exec-ed.
EXEC = os.execv
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
# `/tickets/KO-n`: one mirrored ticket by its Linear identifier, body
# included (KO-328). Behind the token like the run routes; `SHAPED_ROUTES`
# below pairs each of these with its handler.
TICKET_PATH = re.compile(r"^/tickets/([^/]+)$")
# The fixed JSON paths that read the store; every one is behind the token.
JSON_PATHS = frozenset({"/status", "/runs", "/shipped", "/ledger",
                        "/attention", "/board"})


def parse_address(text):
    """`PORT` or `HOST:PORT` as a `(host, port)` pair; ValueError naming both.

    A bare port binds loopback: there the bind address is the daemon's
    only boundary, so the short form is the safe one, and reaching another
    machine takes typing a host -- and, then, `[serve] token_file`. The port is a
    non-negative integer -- 0 asks the kernel for an ephemeral one, which
    is how the tests bind. With a host, it is whatever precedes the last
    colon, so nothing here decides what a valid hostname is: the bind does.
    The `HOST:PORT` rule is `config_tables.split_address()`'s, the one `[console]
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
    """Return target status, process-owned routes and independent run clocks.

    Read-only; a missing store returns 503. Ages and effective working_ms use
    epoch-ms `now`, defaulting to the clock; started_ms is the daemon's bind time.
    Clients interpolate work only with work_started_ms; elapsed_ms is wall time.
    agent_ms is the part of working_ms the time box is judged against, verify_ms
    the rest; verify_started_ms is set only while the open span is a verify.
    Runs include title, phase, round, heartbeat age and sweep strikes. The scaled
    time box and thresholds agree with the loop's budget checks. `project` aliases
    `target`; `actions` and `config_edit` advertise authenticated daemon mutations."""
    now = int(time() * 1000) if now is None else now
    started_ms = now if started_ms is None else started_ms
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        from holophyte.stop import pending_requests
        stops = pending_requests(conn)
        strikes = {run.id: store.read.strike(conn, run.id) for run in runs}
        beat = store.read.supervisor_beat(conn)
        from holophyte.admission import state
        admission, hold_note = state(conn, target)
        if admission == "disabled":
            runs = []
        schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    knobs = sweep_config(target)
    # `time_box_ms` is the box the run is counted against -- the estimate
    # scaled by `[agents] budget_scale` -- so the console's time-box bar and
    # the sweep agree with the cap the loop armed. `thresholds.run_cap` is
    # the hard ceiling in multiples of that box, so the bar can draw it. The
    # box is judged against `agent_ms`, not `working_ms`, which adds verify.
    from holophyte.agent_turns import route_labels
    from holophyte.serve_runs import active_routes, workers_on_previous_build

    scale = budget_scale(target)
    return 200, {
        "target": str(target.path),
        "project": str(target.path),
        "admission": admission, "hold_note": hold_note,
        "schema_version": schema_version,
        "active_routes": active_routes(target),
        "route_labels": route_labels(target),
        "workers_on_previous_build": workers_on_previous_build(target),
        "host": host_label(target, socket.gethostname()),
        "now": now,
        "daemon": {"started_ms": started_ms, "pid": os.getpid()},
        "supervisor": supervisor_view(target, beat, now, knobs),
        "thresholds": {"heartbeat_stale_ms": knobs.heartbeat_stale_ms,
                       "strikes": knobs.stale_strikes,
                       "run_cap": knobs.run_cap},
        "actions": serve_config(target).actions,
        "config_edit": serve_config(target).config_edit,
        "runs": [{"id": run.id, "ticket": run.linearIdentifier,
                  "ticket_url": run.ticketUrl,
                  "title": run.title,
                  "phase": run.phase,
                  "stop_requested": stops.get(run.id, (None, None))[1],
                  "stop_action": stops.get(run.id, (None, None))[0],
                  "started_ms": run.startedAt,
                  "heartbeat_age_ms": now - run.lastHeartbeat,
                  "elapsed_ms": now - run.startedAt,
                  "working_ms": effective_work(run, now),
                  "work_started_ms": run.workStartedAt,
                  "agent_ms": agent_work(run, now),
                  "verify_ms": verify_work(run, now),
                  "verify_started_ms": run.verifyStartedAt,
                  "time_box_ms": (int(run.timeBoxMs * scale)
                                  if run.timeBoxMs else run.timeBoxMs),
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


def parked_item(ticket):
    """One `blocked_on_operator` ticket as an `/attention` item. A ticket a
    pause parked is `paused` (`paused_item()`, KO-609). One whose run has a
    `prUrl` and `parkKind = pull_request` is `pr_open`: it waits on a review
    or a merge, not an answer, so the item carries the URL, the `reason`
    (the question less its first line) and `pr`: the `number` from the URL
    (null when not GitHub's shape) and the `checks`, `review`, `threads` and
    `title` the reconcile last saw (`runs.prSeen*`; KO-368, KO-622), each
    null for a run never polled; the item's own `title` is the ticket's,
    for the console to fall back on. Any other is `blocked` with its
    `question`."""
    if ticket.outcome == "paused":
        return paused_item(ticket)
    question = ticket.blockedQuestion or ""
    if ticket.prUrl and ticket.parkKind == "pull_request":
        _, separator, reason = question.partition("\n")
        reason = reason if separator else question
        match = PR_URL_RE.match(ticket.prUrl)
        return {"kind": "pr_open", "ticket": ticket.linearIdentifier,
                "ticket_url": ticket.ticketUrl, "title": ticket.title,
                "run": ticket.runId, "pr_url": ticket.prUrl,
                "reason": reason, "asked_ms": ticket.askedMs,
                "pr": {"number": int(match.group(4)) if match else None,
                       "checks": ticket.prSeenChecks,
                       "review": ticket.prSeenReview,
                       "threads": ticket.prSeenThreads,
                       "title": ticket.prSeenTitle},
                "level": "attention"}
    return {"kind": "blocked", "ticket": ticket.linearIdentifier,
            "ticket_url": ticket.ticketUrl,
            "question": ticket.blockedQuestion,
            "run": ticket.runId, "asked_ms": ticket.askedMs,
            "pr_url": ticket.prUrl, "level": "attention"}


def attention(target, now=None):
    """The `/attention` answer: `(http status, JSON-able body)`.

    `items` is what needs the operator, in the order they should read it:
    every ticket parked `blocked_on_operator` with its question, as
    `paused` or `pr_open` when it is one (`parked_item()`); every live
    run whose heartbeat age exceeds `heartbeat_stale_ms`; every run that
    ended `failed` within `FAILED_WINDOW_MS` and is its ticket's latest
    attempt, with no different active run; then a supervisor not live.
    Each item that names a run carries the run's `pr_url` (`runs.prUrl`,
    null when it opened none), and each its `level`. `level` on the body
    is the worst over the items -- `attention` when there is any -- else
    `working` when a run is live, else `none`. `critical`, a client's rank
    for a daemon it cannot reach, is never answered: answering is proof.

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
    items = [parked_item(ticket) for ticket in blocked
             if ticket.boardState not in ("Backlog", "Canceled", "Done")]
    for run in runs:
        age = now - run.lastHeartbeat
        if (age > knobs.heartbeat_stale_ms
                and run.boardState not in ("Backlog", "Canceled", "Done")):
            items.append({"kind": "stale_run", "run": run.id,
                          "ticket": run.linearIdentifier,
                          "ticket_url": run.ticketUrl, "phase": run.phase,
                          "heartbeat_age_ms": age, "pr_url": run.prUrl,
                          "level": "attention"})
    items.extend({"kind": "failed", "run": run.id,
                  "ticket": run.linearIdentifier,
                  "ticket_url": run.ticketUrl, "reason": run.outcomeReason,
                  "ended_ms": run.endedAt, "attempt": run.attempt,
                  "pr_url": run.prUrl, "level": "attention"}
                 for run in failed if run.id == run.lastRunId
                 and run.activeRunId is None
                 and run.ticketStatus in ("ready", "in_flight", "blocked_on_operator")
                 and run.boardState not in ("Backlog", "Canceled", "Done"))
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
            "ticket": ticket.linearIdentifier,
            "ticket_url": ticket.ticketUrl, "title": ticket.title,
            "time_box_ms": ticket.timeBoxMs, "run": ticket.activeRunId,
            "question": ticket.blockedQuestion,
            "waits_on": list(ticket.waitsOn),
            "mirrored_ms": ticket.mirroredAt})
    return 200, {"columns": [{"state": state, "tickets": columns[state]}
                             for state in BOARD_STATES],
                 "now": now}


def ticket_detail(target, identifier):
    """The `/tickets/KO-n` answer: `(http status, JSON-able body)`.
    Serve the mirrored contract, URL and active run without calling Linear.
    An unknown identifier returns 404 with an empty object."""
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        ticket = store.read.ticket_by_identifier(conn, identifier)
    finally:
        conn.close()
    if ticket is None:
        return 404, {}
    return 200, {"ticket": ticket.linearIdentifier,
                 "ticket_url": ticket.ticketUrl, "title": ticket.title,
                 "status": ticket.status, "body": ticket.body,
                 "acceptance_criteria": list(ticket.acceptanceCriteria),
                 "verification_commands": list(ticket.verificationCommands),
                 "time_box_ms": ticket.timeBoxMs, "run": ticket.activeRunId,
                 "mirrored_ms": ticket.mirroredAt}


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


def load_token(path, key=TOKEN_KEY):
    """The token file's contents, whitespace-stripped; SystemExit otherwise.

    The file must exist, be a regular file, carry something, and be
    readable by its owner alone: a group- or world-readable token is one
    the daemon refuses to serve behind, and the refusal names the mode so
    the operator can see the bit to drop. `key` is the config key the
    refusal names. The token itself is never printed.
    """
    path = Path(path)
    try:
        info = path.stat()
    except OSError as error:
        raise SystemExit(f"[holo2] {key}: {path}: {error.strerror}")
    if not stat.S_ISREG(info.st_mode):
        raise SystemExit(f"[holo2] {key}: {path} is not a regular file")
    if info.st_mode & TOKEN_FORBIDDEN_MODE:
        raise SystemExit(
            f"[holo2] {key}: {path} is mode {stat.S_IMODE(info.st_mode):04o};"
            " it must not be group- or world-readable (chmod 600)")
    token = path.read_text().strip()
    if not token:
        raise SystemExit(f"[holo2] {key}: {path} is empty")
    return token


def load_tokens(knobs):
    """The bearer values the daemon accepts: the project's `token_file`
    and, when set, the machine's `machine_token_file`, each held to
    `load_token()`'s rules. The caller has already required `token_file`.
    """
    tokens = (load_token(knobs.token_file),)
    if knobs.machine_token_file is not None:
        tokens += (load_token(knobs.machine_token_file, MACHINE_TOKEN_KEY),)
    return tokens


def resolve_token(target, host):
    """The tokens `--serve` on `host` accepts, or None when the bind is
    loopback.

    A non-loopback bind with no `[serve] token_file` configured exits
    naming the key: the bind address stops being the boundary the moment
    it is not loopback, and a daemon that answered anyway would be open to
    the whole network by default. A loopback bind ignores the key even
    when it is set.
    """
    if is_loopback(host):
        return None
    knobs = serve_config(target)
    if knobs.token_file is None:
        raise SystemExit(
            f"[holo2] {target.config_path}: --serve {host} binds beyond"
            f" loopback, which needs {TOKEN_KEY} = \"PATH\" naming a"
            " file whose contents every request presents as"
            " `Authorization: Bearer ...`")
    return load_tokens(knobs)


def resolve_action_token(target, knobs, token):
    """The tokens `POST /actions/...` and the `/config` routes accept, or
    None when neither `[serve] actions` nor `[serve] config_edit` is on.

    `token` is what `resolve_token()` gave the bind: on a non-loopback bind
    it is already the files' contents and the write routes share them. A
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
    return load_tokens(knobs)


def authorized(header, tokens):
    """Whether `header` is exactly `Bearer TOKEN` for one of `tokens`, each
    compared in constant time.

    Any other scheme, a missing header, a wrong or a truncated value are
    all one answer, so the check leaks nothing about how close the guess
    came; every token is compared whichever matches, so neither does it
    tell which one did.
    """
    scheme, _, value = (header or "").partition(" ")
    if scheme != "Bearer":
        return False
    presented = value.strip().encode()
    matches = [hmac.compare_digest(presented, token.encode())
               for token in tokens]
    return any(matches)


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
    (RUN_TURNS_PATH, run_turns),
    (RUN_TRANSCRIPT_PATH, run_transcript),
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

    def handle_one_request(self):
        self.counted = False
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            print(f"[holo2] client disconnected: {getattr(self, 'path', '?')!r}",
                  file=sys.stderr)
        finally:
            if self.counted:
                self.server.done()

    def parse_request(self):
        # A request is in flight from its parsed headers to its answer
        # (`InFlight`); one whose headers land once the daemon is draining
        # for a re-exec is 503, never started and cut off.
        if not super().parse_request():
            return False
        self.counted = self.server.begin()
        if not self.counted:
            self.close_connection = True
            self.answer(503, {"error": "daemon restarting"})
        return self.counted

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
        when it is not. A handler that raises is 500 (`action_failure()`).
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
        target = self.server.target
        try:
            if action == "send-back":
                code, body = send_back_action(
                    target, body.get("run"), body.get("note"),
                    body.get("author", "maintainer"))
            elif action == REQUEUE_ACTION:
                code, body = requeue_action(target, body)
            elif action in LEVERS:
                code, body = LEVERS[action](target, body)
            else:
                code, body = unit_action(target, action, self.server.unit_name)
        except (Exception, SystemExit) as failure:
            code, body = action_failure(target, action, failure)
        self.answer(code, body)

    def do_PUT(self):
        """`PUT /config` when `[serve] config_edit = true`; 405 on any
        other path. Token, then opt-in, then body, in the actions' order
        and for their reasons: the write token on every bind, 404 without
        the opt-in whatever the token, 400 for a body that is not a JSON
        object; `write_config()` judges the text or the patch."""
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


class StatusServer(InFlight, ThreadingHTTPServer):
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
    bearer values those routes accept on every bind, resolved at bind from
    `[serve] token_file` (and `machine_token_file`) when the read `token`
    is None, so a loopback daemon with actions on exits at bind without
    the key rather than answering them open."""

    daemon_threads = True
    # Called by `serve_forever()` between requests; `serve()` sets it to
    # its `CodeWatch`.
    code_check = None

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

    def service_actions(self):
        if self.code_check is not None:
            self.code_check()


def make_server(target, host, port, console_dir=CONSOLE_DIR, token=None):
    """Bind a `StatusServer` for `target` at `host:port` and return it.

    Port 0 binds an ephemeral port; the address actually bound is
    `server.server_address`. The caller runs `serve_forever()` and closes it.
    `console_dir` is where `/` is served from -- the repository's own
    `console/dist/` unless a test points it elsewhere. `token`, when given,
    is the tuple of bearer values every JSON route accepts; `serve()` resolves it
    from the bind address and the config through `resolve_token()`.
    """
    return StatusServer(target, (host, port), console_dir, token)


class _Stopped(Exception):
    """Raised inside `serve_forever()` by the signal handler to unwind it."""


def serve(target, address, out=None, interval=CODE_CHECK_SEC):
    """`--serve`'s whole body: bind, announce, answer until SIGINT/SIGTERM
    or until the factory code moves, then re-execute.

    The handler for the stop signals raises out of `serve_forever()` rather
    than calling `shutdown()`: `shutdown()` waits for the serving loop to
    notice, and the loop is the thread the signal interrupted. The code
    check raises out of it the same way, from the loop's own thread between
    requests; the re-exec waits for the requests in flight, after the
    socket is closed so the fresh process can bind it. Returns after a
    re-exec only when a test's `EXEC` does.
    """
    out = out or sys.stdout
    require_tomlkit()
    host, port = parse_address(address)
    token = resolve_token(target, host)
    server = make_server(target, host, port, token=token)
    watch = server.code_check = CodeWatch(interval, out, factory_revision)

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
            server.serve_forever(poll_interval=min(0.5, interval))
        except _Stopped:
            print("[holo2] serve stopping on signal", file=out)
        except Moved:
            pass  # `watch.moved_to` says so, past the `finally`
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        server.server_close()
    if watch.moved_to is not None:
        server.drain()
        reexec_self(f"factory code moved from {watch.started_from} to"
                    f" {watch.moved_to}; serve re-executing", EXEC, out)
    return 0
