from __future__ import annotations

import collections
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

from holophyte.config.config_tables import split_address
from holophyte.config.serve_settings import console_config, serve_config
from holophyte.host.supervisor import factory_revision
from holophyte.loop.reexec import reexec_self
from holophyte.serve.serve_actions import (
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
from holophyte.serve.serve_config import (
    CONFIG_PATH,
    read_config,
    require_tomlkit,
    write_config,
)
from holophyte.serve.serve_levers import LEVERS
from holophyte.serve.serve_runs import (
    RUN_FILES_PATH,
    RUN_LEDGER_PATH,
    RUN_PATH,
    RUN_TRANSCRIPT_PATH,
    RUN_TURNS_PATH,
    ledger,
    run_detail,
    run_files,
    run_ledger,
    run_transcript,
    run_turns,
    runs,
    shipped,
)
from holophyte.serve.serve_watch import (
    CODE_CHECK_SEC,
    DRAIN_SEC,
    CodeWatch,
    InFlight,
    Moved,
    adopted_socket,
)
from holophyte.serve.views import (  # noqa: F401
    BOARD_STATES,
    attention,
    board,
    parked_item,
    status,
    supervisor_view,
    ticket_detail,
)

ADDRESS_SHAPE = "PORT|HOST:PORT"
LOOPBACK = "127.0.0.1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "127.1"})
TOKEN_KEY = "[serve] token_file"
MACHINE_TOKEN_KEY = "[serve] machine_token_file"
TOKEN_FORBIDDEN_MODE = stat.S_IRWXG | stat.S_IRWXO
# `/peers` and the console's files carry no store data.
OPEN_PATHS = frozenset({"/peers"})
STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
EXEC = os.execv
CONSOLE_DIR = Path(__file__).resolve().parents[2] / "console" / "dist"
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
TICKET_PATH = re.compile(r"^/tickets/([^/]+)$")
JSON_PATHS = frozenset({"/status", "/runs", "/shipped", "/ledger",
                        "/attention", "/board"})
# A None `token` or `action_token` is open; a None `prefix` is a project daemon.
Scope = collections.namedtuple(
    "Scope", ("project", "token", "action_token", "actions", "config_edit",
              "unit_name", "prefix", "beat_stale_ms"))


def parse_address(text):
    """A bare port binds loopback: there the bind is the only boundary."""
    text = str(text)
    if text.isdecimal():
        return LOOPBACK, int(text)
    try:
        return split_address(text)
    except ValueError:
        raise ValueError(f"--serve takes {ADDRESS_SHAPE} (a non-negative"
                         f" integer port, loopback when no host is given),"
                         f" got {text!r}") from None


def is_loopback(host):
    """A name is judged as typed, never resolved."""
    if host in LOOPBACK_HOSTS:
        return True
    try:
        packed = socket.inet_pton(socket.AF_INET, host)
    except OSError:
        return False
    return packed[0] == 127


def load_token(path, key=TOKEN_KEY):
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
    tokens = (load_token(knobs.token_file),)
    if knobs.machine_token_file is not None:
        tokens += (load_token(knobs.machine_token_file, MACHINE_TOKEN_KEY),)
    return tokens


def resolve_token(project, host):
    if is_loopback(host):
        return None
    knobs = serve_config(project)
    if knobs.token_file is None:
        raise SystemExit(
            f"[holo2] {project.config_path}: --serve {host} binds beyond"
            f" loopback, which needs {TOKEN_KEY} = \"PATH\" naming a"
            " file whose contents every request presents as"
            " `Authorization: Bearer ...`")
    return load_tokens(knobs)


def resolve_action_token(project, knobs, token):
    on = [key for key, flag in (("actions", knobs.actions),
                                ("config_edit", knobs.config_edit)) if flag]
    if not on:
        return None
    if token is not None:
        return token
    if knobs.token_file is None:
        raise SystemExit(
            f"[holo2] {project.config_path}: [serve] {' and '.join(on)} = true"
            f" needs {TOKEN_KEY} = \"PATH\" on every bind, loopback"
            " included: the routes it opens answer only to"
            " `Authorization: Bearer ...`")
    return load_tokens(knobs)


def authorized(header, tokens):
    scheme, _, value = (header or "").partition(" ")
    if scheme != "Bearer":
        return False
    presented = value.strip().encode()
    # Every token is compared, so the time taken names none of them.
    matches = [hmac.compare_digest(presented, token.encode())
               for token in tokens]
    return any(matches)


def static_file(console_dir, path):
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


# Tried in this order, after the fixed paths.
SHAPED_ROUTES = (
    (RUN_PATH, run_detail),
    (RUN_FILES_PATH, run_files),
    (RUN_TURNS_PATH, run_turns),
    (RUN_TRANSCRIPT_PATH, run_transcript),
    (RUN_LEDGER_PATH, run_ledger),
    (TICKET_PATH, ticket_detail),
)


def shaped_route(path):
    for shape, handler in SHAPED_ROUTES:
        match = shape.match(path)
        if match is not None:
            return handler, match.group(1)
    return None


def is_json_route(path):
    return (path in JSON_PATHS or path == CONFIG_PATH
            or path.startswith(ACTIONS_PREFIX)
            or shaped_route(path) is not None)


class StatusHandler(BaseHTTPRequestHandler):
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
        if not super().parse_request():
            return False
        # Once draining, a request is refused rather than cut off by the exec.
        self.counted = self.server.begin()
        if not self.counted:
            self.close_connection = True
            self.answer(503, {"error": "daemon restarting"})
        return self.counted

    def do_GET(self):
        parts = urlsplit(self.path)
        self.get(self.server.scope, parts.path, parts.query)

    def get(self, scope, path, query):
        token = (scope.action_token if path == CONFIG_PATH
                 and scope.config_edit else scope.token)
        if token is not None and not self.open_route(path) \
                and not authorized(self.headers.get("Authorization"), token):
            return self.answer(401, {})
        self.dispatch(scope, path, query)

    def dispatch(self, scope, path, query):
        project = scope.project
        beat = ({} if scope.beat_stale_ms is None
                else {"beat_stale_ms": scope.beat_stale_ms})
        if path == "/status":
            code, body = status(project, started_ms=self.server.started_ms,
                                **beat)
            if scope.prefix is not None and code == 200:
                body.update(actions=scope.actions,
                            config_edit=scope.config_edit)
        elif path == "/runs":
            code, body = runs(project, query)
        elif path == "/shipped":
            code, body = shipped(project, query)
        elif path == "/ledger":
            code, body = ledger(project, query)
        elif path == "/attention":
            code, body = attention(project, **beat)
        elif path == "/board":
            # Only a host daemon serves the Board's write routes.
            code, body = board(project, editable=scope.prefix is not None
                               and scope.actions)
        elif path == "/peers":
            code, body = 200, {"self": self.server.self_address,
                               "peers": list(self.server.peers)}
        elif path == CONFIG_PATH:
            code, body = ((404, {"error": "not found", "path": path})
                          if not scope.config_edit else read_config(project))
        elif (shaped := shaped_route(path)) is not None:
            handler, segment = shaped
            code, body = handler(project, segment)
        else:
            found = static_file(self.server.console_dir, path)
            if isinstance(found[0], bytes):
                return self.answer_bytes(*found)
            code, body = found
        self.answer(code, body)

    def do_POST(self):
        self.post(self.server.scope, urlsplit(self.path).path)

    def post(self, scope, path):
        if not path.startswith(ACTIONS_PREFIX):
            return self.refuse()
        token = scope.action_token if scope.actions else scope.token
        if token is not None and not authorized(
                self.headers.get("Authorization"), token):
            return self.answer(401, {})
        action = path[len(ACTIONS_PREFIX):]
        if not scope.actions or action not in self.server.action_names:
            return self.answer(404, {"error": "not found", "path": path})
        try:
            body = self.read_body()
        except ValueError as bad:
            return self.answer(400, {"error": str(bad)})
        self.answer(*self.act(scope, action, body))

    def act(self, scope, action, body):
        project = scope.project
        try:
            if action == "send-back":
                return send_back_action(
                    project, body.get("run"), body.get("note"),
                    body.get("author", "maintainer"))
            if action == REQUEUE_ACTION:
                return requeue_action(project, body)
            if action in LEVERS:
                return LEVERS[action](project, body)
            return unit_action(project, action, scope.unit_name)
        except (Exception, SystemExit) as failure:
            return self.act_failed(scope, action, failure)

    def act_failed(self, scope, action, failure):
        return action_failure(scope.project, action, failure)

    def do_PUT(self):
        self.put(self.server.scope, urlsplit(self.path).path)

    def put(self, scope, path):
        if path != CONFIG_PATH:
            return self.refuse()
        token = scope.action_token if scope.config_edit else scope.token
        if token is not None and not authorized(
                self.headers.get("Authorization"), token):
            return self.answer(401, {})
        if not scope.config_edit:
            return self.answer(404, {"error": "not found", "path": path})
        try:
            body = self.read_body()
        except ValueError as bad:
            return self.answer(400, {"ok": False, "error": str(bad)})
        self.answer(*self.edit_config(scope, body))

    def edit_config(self, scope, body):
        return write_config(scope.project, body)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 <= length <= MAX_BODY:
            raise ValueError(f"body must be under {MAX_BODY} bytes")
        return parse_action_body(self.rfile.read(length))

    def open_route(self, path):
        if path in OPEN_PATHS:
            return True
        return not is_json_route(path)

    def refuse(self):
        self.answer(405, {"error": "method not allowed",
                          "method": self.command,
                          "path": self.path.split("?")[0]},
                    allow="GET")

    def do_OPTIONS(self):
        """Open: a preflight reads and discloses nothing, the request is gated."""
        self.answer_bytes(b"", "application/json", code=204, extra=[
            ("Access-Control-Allow-Methods", "GET, POST, PUT"),
            ("Access-Control-Allow-Headers",
             "authorization, accept, content-type, if-match"),
            ("Access-Control-Max-Age", "600"),
        ])

    def __getattr__(self, name):
        """Any other method is 405 JSON, not the base class's 501 HTML."""
        if name.startswith("do_"):
            return self.refuse
        raise AttributeError(name)

    def answer(self, code, body, allow=None):
        self.answer_bytes(json.dumps(body).encode(), "application/json",
                          code=code, allow=allow)

    def answer_bytes(self, payload, content_type, code=200, allow=None,
                     extra=()):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        # So a page served by one daemon can read another's.
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
    daemon_threads = True
    code_check = None
    action_names = ACTIONS

    def __init__(self, project, address, console_dir=CONSOLE_DIR, token=None,
                 sock=None):
        self.project = project
        self.console_dir = Path(console_dir)
        self.peers = console_config(project).daemons
        knobs = serve_config(project)
        # Resolved at bind: actions without a key fail here, never serve open.
        self.scope = Scope(project, token,
                           resolve_action_token(project, knobs, token),
                           knobs.actions, knobs.config_edit, knobs.name,
                           None, None)
        self.started_ms = int(time() * 1000)
        self.listen(address, StatusHandler, sock)

    def listen(self, address, handler, sock):
        if sock is None:
            super().__init__(address, handler)
        else:
            super().__init__(sock.getsockname()[:2], handler,
                             bind_and_activate=False)
            self.socket.close()
            self.socket = sock
        host, port = self.server_address[:2]
        self.self_address = f"{host}:{port}"

    def service_actions(self):
        if self.code_check is not None:
            self.code_check()


def make_server(project, host, port, console_dir=CONSOLE_DIR, token=None,
                sock=None):
    return StatusServer(project, (host, port), console_dir, token, sock)


class _Stopped(Exception):
    pass


def listen_address(address, sock, out, source="--serve"):
    if sock is None:
        return parse_address(address)
    bound = sock.getsockname()[:2]
    if address and parse_address(address) != bound:
        print(f"[holo2] {source} {address} is ignored: serving on"
              f" {bound[0]}:{bound[1]}, the socket the service manager"
              " handed over", file=out)
    return bound


def serve(project, address, out=None, interval=CODE_CHECK_SEC):
    out = out or sys.stdout
    require_tomlkit()
    sock = adopted_socket()
    host, port = listen_address(address, sock, out)
    token = resolve_token(project, host)
    server = make_server(project, host, port, token=token, sock=sock)
    guard = "open" if token is None else "behind a bearer token"
    opened = [name for name, on in (("actions", server.scope.actions),
                                    ("config edit", server.scope.config_edit))
              if on]
    mode = f"with {' and '.join(opened)}" if opened else "read-only"
    return run(server, f"{mode} for {project.path}, {guard}", out, interval,
               adopted=sock is not None)


def run(server, description, out, interval, adopted=False, watch=None):
    if watch is None:
        watch = CodeWatch(interval, out, factory_revision)
    server.code_check = watch

    # Raise, not `shutdown()`: that waits on the thread the signal interrupted.
    def on_signal(signum, _frame):
        raise _Stopped(signum)

    previous = {signum: signal.signal(signum, on_signal)
                for signum in STOP_SIGNALS}
    try:
        bound_host, bound_port = server.server_address[:2]
        print(f"[holo2] serving {bound_host}:{bound_port} {description}",
              file=out)
        try:
            server.serve_forever(poll_interval=min(0.5, interval))
        except _Stopped:
            print("[holo2] serve stopping on signal", file=out)
        except Moved:
            pass
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        server.server_close()
    if watch.moved_to is None:
        return 0
    if not server.drain(DRAIN_SEC):
        print(f"[holo2] requests still in flight after {DRAIN_SEC}s;"
              " leaving them", file=out)
    moved = f"factory code moved from {watch.started_from} to {watch.moved_to}"
    if adopted:
        print(f"[holo2] {moved}; serve exiting for the service manager to"
              " start the new code", file=out, flush=True)
        return 0
    reexec_self(f"{moved}; serve re-executing", EXEC, out)
    return 0
