"""`--serve HOST:PORT`: a read-only HTTP daemon answering `/status` as JSON.

One `ThreadingHTTPServer` per target, bound to the one address the command
line names, so a drawer or dashboard on another host of the tailnet can poll
the factory without ssh. Every request opens the store through
`store.read.open_readonly()`, reads, and closes it: the daemon never holds a
connection between requests and never holds a write connection at all,
which is why this module imports `store.read` and nothing from `store`
itself. The handler calls the typed read views and formats JSON; no SQL
lives here, so a later daemon can replace the module wholesale against the
same store.

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

import store.read
from holophyte.config import sweep_config
from holophyte.report import host_label
from holophyte.supervisor import SWEEPABLE_PHASES

ADDRESS_SHAPE = "HOST:PORT"
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


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
        return 503, {"error": "no store",
                     "detail": f"{target.path} has no store yet; nothing has"
                               " run against it on this host",
                     "target": str(target.path)}
    conn = store.read.open_readonly(target.store_path)
    try:
        runs = store.read.live_runs(conn, SWEEPABLE_PHASES)
        beat = store.read.supervisor_beat(conn)
    finally:
        conn.close()
    knobs = sweep_config(target)
    if beat is None:
        supervisor = {"state": "none", "pid": None, "heartbeat_age_ms": None,
                      "host": None}
    else:
        age = now - beat.lastBeat
        supervisor = {
            "state": "live" if age < knobs.heartbeat_stale_ms else "stale",
            "pid": beat.pid, "heartbeat_age_ms": age,
            "host": host_label(target, beat.host)}
    return 200, {
        "target": str(target.path),
        "host": host_label(target, socket.gethostname()),
        "now": now,
        "supervisor": supervisor,
        "thresholds": {"heartbeat_stale_ms": knobs.heartbeat_stale_ms,
                       "strikes": knobs.stale_strikes},
        "runs": [{"id": run.id, "ticket": run.linearIdentifier,
                  "phase": run.phase,
                  "heartbeat_age_ms": now - run.lastHeartbeat,
                  "elapsed_ms": now - run.startedAt,
                  "time_box_ms": run.timeBoxMs,
                  "host": host_label(target, run.host)}
                 for run in runs],
    }


class StatusHandler(BaseHTTPRequestHandler):
    """`GET /status` and nothing else: 404 for other paths, 405 otherwise.

    Every answer is JSON with `Cache-Control: no-store`, the error ones
    included, so a client can parse whatever comes back. The default access
    log to stderr is silenced: the daemon shares a terminal with the loop,
    and a line per poll would bury the lines that matter.
    """

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/status":
            code, body = status(self.server.target)
        else:
            code, body = 404, {"error": "not found", "path": path}
        self.answer(code, body)

    def refuse(self):
        self.answer(405, {"error": "method not allowed",
                          "method": self.command,
                          "path": self.path.split("?")[0]},
                    allow="GET")

    do_POST = do_PUT = do_DELETE = do_PATCH = refuse

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
