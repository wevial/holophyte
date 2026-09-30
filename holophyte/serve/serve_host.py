from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import time
from urllib.parse import unquote, urlsplit

import store.read
from holophyte.admission import project_of
from holophyte.admission import state as admission_state
from holophyte.cli.report import host_label
from holophyte.cli.status import load_sweep_state
from holophyte.config.config_tables import sweep_config
from holophyte.config.serve_settings import serve_config
from holophyte.host.registry import HostError, settings
from holophyte.host.supervisor import SWEEPABLE_PHASES, factory_revision
from holophyte.loop.pool_handoff import workers_on_previous_build
from holophyte.loop.reexec import SWEEP_UNIT, systemctl_user
from holophyte.redact import known_secrets, outbound
from holophyte.serve.serve_actions import (
    ACTIONS,
    ACTIONS_PREFIX,
    action_failure,
)
from holophyte.serve.serve_board import post_path, post_ticket, put_ticket, ticket_path
from holophyte.serve.serve_config import require_tomlkit
from holophyte.serve.serve_watch import CODE_CHECK_SEC, adopted_socket
from holophyte.serve.server import (
    CONSOLE_DIR,
    Scope,
    StatusHandler,
    StatusServer,
    attention,
    authorized,
    is_json_route,
    is_loopback,
    listen_address,
    load_token,
    run,
    static_file,
    supervisor_view,
)
from store.schema import SCHEMA_VERSION, SchemaNewer, _readable_from

PROJECTS_PREFIX = "/projects/"
MACHINE_TOKEN_KEY = "[serve] machine_token_file"
RUN_SWEEP = "run-sweep"
# A file, not a store: no store owns the sweep, and stores may be down.
HOST_LEDGER = "host-actions.jsonl"
HOST_ACTIONS = ACTIONS - {"restart-supervisor"}
# The sweep unit's `TimeoutStartSec`.
SWEEP_TIMEOUT_SEC = 120
SWEEP_FIELDS = ("started", "ended", "revision", "pid", "exit", "projects")
SWEEP_OK = frozenset({"fresh", "running"})
# Under the drawer's and the tray's two-second request limit.
HOST_READ_WAIT_S = 1.0
ROOT_READERS = 8


def check_schema(project):
    """`store.read.open_readonly()` checks no schema version."""
    path = project.store_path
    if not path.exists():
        return
    conn = store.read.open_readonly(path)
    try:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        if version > SCHEMA_VERSION:
            floor = _readable_from(conn, version)
            if floor is None or floor > SCHEMA_VERSION:
                raise SchemaNewer(path, version, floor)
    finally:
        conn.close()


def project_error(project, bad):
    if isinstance(bad, SchemaNewer):
        text = f"schema newer than build: {bad}"
    else:
        text = f"{type(bad).__name__}: {bad}"
    try:
        secrets = known_secrets(project.config())
    except (Exception, SystemExit):
        secrets = known_secrets(None)
    return outbound(text, secrets)


def each_project(entries, read):
    """Side by side, so one store's lock wait overlaps the others'."""
    entries = list(entries)
    if not entries:
        return []

    def bounded(entry):
        with store.read.lock_wait(HOST_READ_WAIT_S):
            return read(entry)
    with ThreadPoolExecutor(min(len(entries), ROOT_READERS)) as pool:
        return list(pool.map(bounded, entries))


def missing_row(scope):
    target = scope.project
    if not target.store_path.exists():
        return None
    conn = store.read.open_readonly(target.store_path)
    try:
        if project_of(conn, target) is not None:
            return None
    finally:
        conn.close()
    return {"error": "no project row", "project": scope.unit_name,
            "project_row": None,
            "detail": f"the store has no project row for {target.path};"
                      f" `factory.py project add {target.path}` writes it"}


def beat_stale_ms(knobs):
    return 2 * knobs.sweep_sec * 1000


def project_summary(entry, now, stale_ms):
    row = {"name": entry.name, "path": str(entry.path), "store": None,
           "error": entry.error, "host": None, "schema_version": None,
           "admission": None, "hold_note": None, "project_row": None,
           "supervisor": None, "runs": [], "workers_on_previous_build": None}
    if entry.error is not None:
        return row
    target = entry.target
    try:
        if not target.store_path.exists():
            return row
        row["store"] = str(target.store_path)
        check_schema(target)
        row.update(_store_facts(target, now, stale_ms))
    except (Exception, SystemExit) as bad:
        row["error"] = project_error(target, bad)
    return row


def _store_facts(target, now, stale_ms):
    conn = store.read.open_readonly(target.store_path)
    try:
        (version,) = conn.execute("PRAGMA user_version").fetchone()
        admission, note = admission_state(conn, target)
        row_id = project_of(conn, target)
        beat = store.read.supervisor_beat(conn)
        runs = ([] if admission == "disabled"
                else store.read.live_runs(conn, SWEEPABLE_PHASES))
    finally:
        conn.close()
    return {"schema_version": version, "admission": admission,
            "hold_note": note, "project_row": row_id,
            "host": host_label(target, socket.gethostname()),
            "supervisor": supervisor_view(target, beat, now,
                                          sweep_config(target), stale_ms),
            "runs": [{"id": run.id, "ticket": run.linearIdentifier,
                      "phase": run.phase,
                      "heartbeat_age_ms": now - run.lastHeartbeat}
                     for run in runs],
            "workers_on_previous_build": workers_on_previous_build(target)}


def _ms(value):
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else None


def sweep_view(home, now, sweep_sec):
    view = dict.fromkeys(SWEEP_FIELDS)
    view["error"] = None
    try:
        doc = load_sweep_state(home)
    except (OSError, ValueError) as bad:
        return {**view, "state": "unreadable", "error": str(bad)}
    if doc is None:
        return {**view, "state": "none"}
    if not isinstance(doc, dict):
        return {**view, "state": "unreadable",
                "error": "sweep.json is not a JSON object"}
    view.update({key: doc.get(key) for key in SWEEP_FIELDS})
    started, ended = _ms(doc.get("started")), _ms(doc.get("ended"))
    if started is not None and (ended is None or ended < started):
        state = ("running" if now - started <= SWEEP_TIMEOUT_SEC * 1000
                 else "killed")
    elif ended is not None and now - ended <= 2 * sweep_sec * 1000:
        state = "fresh"
    else:
        state = "stale"
    return {**view, "state": state}


def host_status(server, now=None):
    now = int(time() * 1000) if now is None else now
    knobs = server.settings
    sweep = sweep_view(server.host.home, now, knobs.sweep_sec)
    stale_ms = beat_stale_ms(knobs)
    return 200, {
        "now": now,
        "daemon": {"started_ms": server.started_ms, "pid": os.getpid()},
        "build": {"daemon": getattr(server.code_check, "started_from", None),
                  "sweep": sweep["revision"], "head": factory_revision()},
        "sweep": sweep,
        "actions": server.actions,
        "projects": each_project(
            server.host.projects(),
            lambda entry: project_summary(entry, now, stale_ms)),
    }


def host_attention(server, now=None):
    now = int(time() * 1000) if now is None else now
    knobs = server.settings
    sweep = sweep_view(server.host.home, now, knobs.sweep_sec)
    items = []
    if sweep["state"] not in SWEEP_OK:
        items.append({"kind": "sweep_stale", "project": None,
                      "state": sweep["state"], "started": sweep["started"],
                      "ended": sweep["ended"], "level": "attention"})
    working = False
    stale_ms = beat_stale_ms(knobs)
    for found, busy in each_project(
            server.host.projects(),
            lambda entry: _project_items(entry, now, stale_ms)):
        items.extend(found)
        working = working or busy
    level = "attention" if items else ("working" if working else "none")
    return 200, {"level": level, "items": items, "now": now}


def _project_items(entry, now, stale_ms):
    summary = project_summary(entry, now, stale_ms)
    name, path = entry.name, summary["path"]
    base = {"project": name, "path": path, "level": "attention"}
    if summary["error"] is not None:
        return [{"kind": "project_error", **base,
                 "error": summary["error"]}], False
    if summary["store"] is None:
        return [{"kind": "no_store", **base,
                 "detail": f"{path} has no store; `factory.py project add"
                           f" {path}` creates and registers it"}], False
    items = []
    if summary["project_row"] is None:
        items.append({"kind": "no_project_row", **base,
                      "detail": f"the store has no project row for {path};"
                                f" `factory.py project add {path}` writes it"})
    try:
        _, body = attention(entry.target, now, beat_stale_ms=stale_ms)
    except (Exception, SystemExit) as bad:
        return items + [{"kind": "project_error", **base,
                         "error": project_error(entry.target, bad)}], False
    items.extend({**item, "project": name} for item in body["items"])
    return items, body["level"] == "working"


def record_host_action(home, row):
    handle = os.open(home / HOST_LEDGER,
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(handle, "a", encoding="utf-8") as out:
        out.write(json.dumps(row) + "\n")
        out.flush()
        os.fsync(out.fileno())


def run_sweep(home, body, now=None):
    now = int(time() * 1000) if now is None else now
    note, author = body.get("note"), body.get("author")
    row = {"at": now, "action": "run_sweep", "unit": SWEEP_UNIT,
           "via": f"POST {ACTIONS_PREFIX}{RUN_SWEEP}", "pid": os.getpid(),
           "author": author if isinstance(author, str) else "maintainer",
           "note": note if isinstance(note, str) else None}
    reply = {"action": RUN_SWEEP, "unit": SWEEP_UNIT}
    try:
        record_host_action(home, row)
    except OSError as bad:
        return 200, {**reply, "ok": False, "recorded": None,
                     "detail": f"could not record in {home / HOST_LEDGER}"
                               f" ({bad}); nothing run"}
    # A oneshot's blocking `start` would wait out the whole sweep.
    ok, detail = systemctl_user("start", SWEEP_UNIT, "--no-block")
    return 200, {**reply, "ok": ok, "detail": detail,
                 "recorded": str(home / HOST_LEDGER)}


def project_token(knobs):
    if knobs.token_file is None:
        return ()
    try:
        return (load_token(knobs.token_file),)
    except SystemExit:
        return ()


class HostHandler(StatusHandler):
    def handle_one_request(self):
        with store.read.lock_wait(HOST_READ_WAIT_S):
            super().handle_one_request()

    def route(self, path):
        try:
            return self.server.resolve(path)
        except HostError as bad:
            self.answer(503, {"error": str(bad)})
            return None

    def admitted(self, tokens):
        return tokens is None or authorized(
            self.headers.get("Authorization"), tokens)

    def do_GET(self):
        parts = urlsplit(self.path)
        found = self.route(parts.path)
        if found is None:
            return
        scope, path = found
        if scope is not None:
            return self.get(scope, path, parts.query)
        if path in ("/status", "/attention"):
            if not self.admitted(self.server.read_token):
                return self.answer(401, {})
            show = host_status if path == "/status" else host_attention
            try:
                return self.answer(*show(self.server))
            except HostError as bad:
                return self.answer(503, {"error": str(bad)})
        if path == "/peers":
            return self.answer(200, {"self": self.server.self_address,
                                     "peers": list(self.server.peers)})
        if is_json_route(path):
            return self.answer(404, {
                "error": "not found", "path": path,
                "detail": "a host daemon answers project routes under"
                          f" {PROJECTS_PREFIX}NAME"})
        found = static_file(self.server.console_dir, path)
        if isinstance(found[0], bytes):
            return self.answer_bytes(*found)
        self.answer(*found)

    def dispatch(self, scope, path, query):
        if not is_json_route(path):
            return self.answer(404, {"error": "not found",
                                     "path": scope.prefix + path})
        refused = self.refused(scope)
        if refused is not None:
            return self.answer(*refused)
        try:
            if path == "/status" and (rowless := missing_row(scope)):
                return self.answer(503, rowless)
            super().dispatch(scope, path, query)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except (Exception, SystemExit) as bad:
            self.answer(*self.failure(scope, path, bad))

    def do_POST(self):
        found = self.route(urlsplit(self.path).path)
        if found is None:
            return
        scope, path = found
        if scope is not None:
            board = post_path(path)
            if board is not None:
                return post_ticket(self, scope, path, *board)
            return self.post(scope, path)
        if not path.startswith(ACTIONS_PREFIX):
            return self.refuse()
        server = self.server
        if not self.admitted(server.write_token if server.actions
                             else server.read_token):
            return self.answer(401, {})
        if not server.actions or path != ACTIONS_PREFIX + RUN_SWEEP:
            return self.answer(404, {"error": "not found", "path": path})
        try:
            body = self.read_body()
        except ValueError as bad:
            return self.answer(400, {"error": str(bad)})
        self.answer(*run_sweep(server.host.home, body))

    def act(self, scope, action, body):
        return self.refused(scope) or super().act(scope, action, body)

    def act_failed(self, scope, action, failure):
        return self.failure(scope, ACTIONS_PREFIX + action, failure)

    def do_PUT(self):
        found = self.route(urlsplit(self.path).path)
        if found is None:
            return
        scope, path = found
        if scope is None:
            return self.refuse()
        identifier = ticket_path(path)
        if identifier is not None:
            return put_ticket(self, scope, path, identifier)
        self.put(scope, path)

    def edit_config(self, scope, body):
        refused = self.refused(scope)
        if refused is not None:
            return refused
        try:
            return super().edit_config(scope, body)
        except (Exception, SystemExit) as bad:
            return self.failure(scope, "/config", bad)

    def refused(self, scope):
        if scope.project is None:
            name = unquote(scope.prefix[len(PROJECTS_PREFIX):])
            return 404, {"error": "not found", "path": scope.prefix,
                         "detail": f"no project {name!r}"
                                   f" in {self.server.host.path}"}
        try:
            check_schema(scope.project)
        except (Exception, SystemExit) as bad:
            return self.failure(scope, "", bad)
        return None

    def failure(self, scope, path, bad):
        name = scope.unit_name
        if isinstance(bad, (SchemaNewer, sqlite3.Error)):
            if isinstance(bad, SchemaNewer) and self.server.code_check:
                self.server.code_check.check_now()
            return 503, {"error": project_error(scope.project, bad),
                         "project": name}
        code, body = action_failure(scope.project, scope.prefix + path, bad)
        return code, {**body, "project": name}


class HostServer(StatusServer):
    action_names = HOST_ACTIONS

    def __init__(self, host, knobs, address, console_dir=CONSOLE_DIR,
                 read_token=None, write_token=None, sock=None):
        self.host = host
        self.project = None
        self.scope = None
        self.settings = knobs
        self.console_dir = Path(console_dir)
        self.peers = knobs.daemons
        self.read_token = read_token
        self.write_token = write_token
        self.actions = knobs.actions and write_token is not None
        self.started_ms = int(time() * 1000)
        self.listen(address, HostHandler, sock)

    def resolve(self, path):
        if not path.startswith(PROJECTS_PREFIX):
            return None, path
        segment, slash, rest = path[len(PROJECTS_PREFIX):].partition("/")
        prefix = PROJECTS_PREFIX + segment
        stale_ms = beat_stale_ms(self.settings)
        entry = self.host.project(unquote(segment))
        # An unknown name is scoped too: 401 or 404 as any unknown route.
        if entry is None:
            return Scope(None, self.read_token, self.write_token,
                         self.actions, False, None, prefix, stale_ms), \
                slash + rest
        knobs = serve_config(entry.target)
        own = project_token(knobs)
        read = None if self.read_token is None else self.read_token + own
        write = None if self.write_token is None else self.write_token + own
        return Scope(entry.target, read, write, self.actions,
                     knobs.config_edit and write is not None, entry.name,
                     prefix, stale_ms), slash + rest


def host_tokens(host, knobs, bound_host, entries):
    needs = []
    if not is_loopback(bound_host):
        needs.append(f"a bind beyond loopback ({bound_host})")
    if knobs.actions:
        needs.append("[serve] actions = true")
    editing = [entry.name for entry in entries
               if entry.error is None and serve_config(entry.target).config_edit]
    if editing:
        needs.append("[serve] config_edit in " + ", ".join(editing))
    if knobs.machine_token_file is None:
        if needs:
            raise SystemExit(
                f"[holo2] {host.path}: {'; '.join(needs)} needs"
                f" {MACHINE_TOKEN_KEY} = \"PATH\" naming a file whose contents"
                " every request presents as `Authorization: Bearer ...`")
        return None, None
    machine = (load_token(knobs.machine_token_file,
                          f"{host.path} {MACHINE_TOKEN_KEY}"),)
    return (None if is_loopback(bound_host) else machine), machine


def name_ignored(entries, knobs, out):
    for entry in entries:
        if entry.error is not None:
            print(f"[holo2] {entry.path} is not served: {entry.error}",
                  file=out)
            continue
        own = serve_config(entry.target).machine_token_file
        if own is not None and own != knobs.machine_token_file:
            print(f"[holo2] {entry.name}: [serve] machine_token_file {own} is"
                  f" ignored; the host's {MACHINE_TOKEN_KEY} is the one"
                  " machine token", file=out)


def serve_host(host, address=None, out=None, interval=CODE_CHECK_SEC):
    out = out or sys.stdout
    require_tomlkit()
    try:
        knobs = settings(host)
        entries = host.projects()
    except HostError as bad:
        raise SystemExit(str(bad)) from None
    sock = adopted_socket()
    text = address or knobs.bind
    if sock is None and not text:
        raise SystemExit(
            f"[holo2] {host.path}: the host daemon needs [serve] bind ="
            " \"PORT|HOST:PORT\" or --serve PORT|HOST:PORT, unless the"
            " service manager hands it a socket")
    try:
        bound_host, port = listen_address(
            text, sock, out, "--serve" if address else f"{host.path} [serve] bind")
    except ValueError as bad:
        raise SystemExit(f"[holo2] {host.path}: [serve] bind: {bad}") from None
    read, write = host_tokens(host, knobs, bound_host, entries)
    name_ignored(entries, knobs, out)
    server = HostServer(host, knobs, (bound_host, port), read_token=read,
                        write_token=write, sock=sock)
    guard = "open" if read is None else "behind the machine token"
    mode = "with actions" if server.actions else "without actions"
    return run(server, f"{mode} for {len(entries)} projects in {host.path},"
                       f" {guard}", out, interval, adopted=sock is not None)
