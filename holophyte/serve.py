"""`--serve HOST:PORT`: a read-only HTTP daemon answering `/status`,
`/runs` and `/attention` as JSON.

One `ThreadingHTTPServer` per target, bound to the one address the command
line names, so a drawer or dashboard on another host of the tailnet can poll
the factory without ssh. Every request opens the store through
`store.read.open_readonly()`, reads, and closes it: the daemon never holds a
connection between requests and never holds a write connection at all,
which is why this module imports `store.read` and nothing from `store`
itself. The handler calls the typed read views and formats JSON; no SQL
lives here, so a later daemon can replace the module wholesale against the
same store. `/runs` is the `--report` table as JSON: the same rows
`report_rows()` prints, in the same order, so a dashboard and the terminal
never disagree about the history. `/attention` is "what needs the
operator": one ordered list of items with a level, computed here where the
store is, so the drawer, a native app and a phone client all show the same
answer and the rule lives in one place rather than in each client.

Every host the body carries passes through `host_label()`, so a configured
`[report] host_label` is what the network sees rather than the machine name.

Run the tests: python3 -m unittest discover -s tests -p 'test_serve*' -v
"""
from __future__ import annotations

import json
import signal
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time
from urllib.parse import parse_qs, urlsplit

import store.read
from holophyte.config import sweep_config
from holophyte.report import ended_rows, host_label
from holophyte.supervisor import SWEEPABLE_PHASES

ADDRESS_SHAPE = "HOST:PORT"
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
# How long a failed run stays on `/attention`: a failure the operator has
# not requeued or merged past in a day is one they have not looked at.
FAILED_WINDOW_MS = 24 * 60 * 60 * 1000


def parse_address(text):
    """`HOST:PORT` as a `(host, port)` pair; ValueError naming the shape.

    The port is a non-negative integer -- 0 asks the kernel for an ephemeral
    one, which is how the tests bind. The host is whatever precedes the last
    colon, so nothing here decides what a valid hostname is: the bind does.
    """
    host, sep, port = str(text).rpartition(":")
    if not sep or not host or not port.isdigit():
        raise ValueError(f"--serve takes {ADDRESS_SHAPE} (a non-negative"
                         f" integer port), got {text!r}")
    return host, int(port)


def status(target, now=None):
    """The `/status` answer for `target`: `(http status, JSON-able body)`.

    A target with no store answers 503 rather than creating one -- a
    read-only daemon that wrote an empty store into a home would shadow the
    adoption `open_store()` performs on first need. Ages are computed here
    against `now` (epoch milliseconds, the clock by default) so the client
    compares one number to `thresholds.heartbeat_stale_ms` and never has to
    agree with the writer host about the time.
    """
    now = int(time() * 1000) if now is None else now
    if not target.store_path.exists():
        return 503, no_store(target)
    conn = store.read.open_readonly(target.store_path)
    try:
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        beat = store.read.supervisor_beat(conn)
    finally:
        conn.close()
    knobs = sweep_config(target)
    return 200, {
        "target": str(target.path),
        "host": host_label(target, socket.gethostname()),
        "now": now,
        "supervisor": supervisor_view(target, beat, now, knobs),
        "thresholds": {"heartbeat_stale_ms": knobs.heartbeat_stale_ms,
                       "strikes": knobs.stale_strikes},
        "runs": [{"id": run.id, "ticket": run.linearIdentifier,
                  "phase": run.phase,
                  "heartbeat_age_ms": now - run.lastHeartbeat,
                  "elapsed_ms": now - run.startedAt,
                  "time_box_ms": run.timeBoxMs,
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


def attention(target, now=None):
    """The `/attention` answer: `(http status, JSON-able body)`.

    `items` is what needs the operator, in the order they should read it:
    every ticket parked `blocked_on_operator` with its question; every live
    run whose heartbeat age exceeds `heartbeat_stale_ms`; every run that
    ended `failed` within `FAILED_WINDOW_MS` and whose ticket is still
    `in_flight` (a requeue walks it to `ready`, a later attempt merges it,
    and either drops the failure); then the supervisor when it is not
    live. Each item carries its `level`. `level` on the body is the worst
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
    items = [{"kind": "blocked", "ticket": ticket.linearIdentifier,
              "question": ticket.blockedQuestion, "level": "attention"}
             for ticket in blocked]
    for run in runs:
        age = now - run.lastHeartbeat
        if age > knobs.heartbeat_stale_ms:
            items.append({"kind": "stale_run", "run": run.id,
                          "ticket": run.linearIdentifier, "phase": run.phase,
                          "heartbeat_age_ms": age, "level": "attention"})
    items.extend({"kind": "failed", "run": run.id,
                  "ticket": run.linearIdentifier, "reason": run.outcomeReason,
                  "ended_ms": run.endedAt, "level": "attention"}
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
    return 200, {"level": level, "items": items, "now": now}


def no_store(target):
    """The 503 body for a target whose store does not exist yet."""
    return {"error": "no store",
            "detail": f"{target.path} has no store yet; nothing has run"
                      " against it on this host",
            "target": str(target.path)}


def parse_limit(query):
    """`?limit=N` as a positive int, None when absent; ValueError otherwise.

    The shape is the report's: a dashboard asks for the newest few rows,
    and `limit=0` or `limit=abc` is a client bug to be told about, not a
    request for nothing.
    """
    values = parse_qs(query, keep_blank_values=True).get("limit")
    if values is None:
        return None
    text = values[-1]
    if not text.isdigit() or int(text) < 1:
        raise ValueError(f"limit must be a positive integer, got {text!r}")
    return int(text)


def json_host(target, host):
    """`host_label()` for JSON: null, not the table's `?`, for a row older
    than the host column, label or not."""
    return None if host is None else host_label(target, host)


def runs(target, query=""):
    """The `/runs` answer: `--report`'s rows as JSON, first `limit` of them.

    Same rows, same order as `report_rows()` -- oldest first -- with the
    tuple's positions named, plus `ended_ms`: the run's `endedAt` in epoch
    milliseconds, which the table never prints and a drawer's "last merge
    KO-n · 2h ago" is read from against `/status`'s `now`. `host` is None
    for a row older than the column, label or not, as on `/status`.
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
                  "host": json_host(target, host), "ended_ms": ended_at}
                 for ticket, actual, estimate, ratio, rounds, outcome, host,
                 ended_at in rows],
        "limit": limit,
    }


class StatusHandler(BaseHTTPRequestHandler):
    """`GET /status`, `GET /runs` and `GET /attention`: 404 for other paths,
    405 otherwise.

    "Otherwise" is every other method, HEAD and OPTIONS included: a client
    that speaks anything but GET gets a JSON refusal it can parse, never
    the library's HTML 501 page.

    Every answer is JSON with `Cache-Control: no-store`, the error ones
    included, so a client can parse whatever comes back. The default access
    log to stderr is silenced: the daemon shares a terminal with the loop,
    and a line per poll would bury the lines that matter.
    """

    def do_GET(self):
        parts = urlsplit(self.path)
        path = parts.path
        if path == "/status":
            code, body = status(self.server.target)
        elif path == "/runs":
            code, body = runs(self.server.target, parts.query)
        elif path == "/attention":
            code, body = attention(self.server.target)
        else:
            code, body = 404, {"error": "not found", "path": path}
        self.answer(code, body)

    def refuse(self):
        self.answer(405, {"error": "method not allowed",
                          "method": self.command,
                          "path": self.path.split("?")[0]},
                    allow="GET")

    def __getattr__(self, name):
        # `BaseHTTPRequestHandler` dispatches on `do_<METHOD>` and answers
        # 501 HTML when the attribute is missing; here every method but GET
        # is the same 405 JSON, whether or not the RFC names it.
        if name.startswith("do_"):
            return self.refuse
        raise AttributeError(name)

    def answer(self, code, body, allow=None):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        if allow is not None:
            self.send_header("Allow", allow)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


class StatusServer(ThreadingHTTPServer):
    """The bound server, carrying the one target its handler answers for."""

    daemon_threads = True

    def __init__(self, target, address):
        self.target = target
        super().__init__(address, StatusHandler)


def make_server(target, host, port):
    """Bind a `StatusServer` for `target` at `host:port` and return it.

    Port 0 binds an ephemeral port; the address actually bound is
    `server.server_address`. The caller runs `serve_forever()` and closes it.
    """
    return StatusServer(target, (host, port))


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
    server = make_server(target, host, port)

    def on_signal(signum, _frame):
        raise _Stopped(signum)

    previous = {signum: signal.signal(signum, on_signal)
                for signum in STOP_SIGNALS}
    try:
        bound_host, bound_port = server.server_address[:2]
        print(f"[holo2] serving {bound_host}:{bound_port} read-only for"
              f" {target.path}", file=out)
        try:
            server.serve_forever()
        except _Stopped:
            print("[holo2] serve stopping on signal", file=out)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        server.server_close()
    return 0
