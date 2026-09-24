"""holophyte.serve_host: `factory.py --serve` with no project, one daemon
for every project in the host registry (consolidation stage 1).

Each project's routes answer under `/projects/NAME/...` with the bodies
the project daemon answers at its root, `NAME` being the project's
`[serve] name`. The name resolves through `Host.project()` alone -- the
registry, re-read when `host.toml` changed -- and a name outside it is 404
before any file of that project is opened. The root answers for the host:
`/status` lists every project (`project_summary()`) with the daemon's,
the last sweep's and the checkout's builds and the last sweep
(`sweep_view()`); `/attention` merges every project's items, each carrying
its `project`, with a `sweep_stale` item when the sweep is not fresh;
`/peers`, `/` and the console's files are open as on a project daemon.

One project's failure is that project's answer, never the daemon's: a
store stamped newer than this build (checked before every project route,
since `store.read.open_readonly()` checks no version) is 503 and makes the
code watch read `HEAD` at once, so a daemon whose checkout moved leaves for
the new code; a locked or corrupt store (`sqlite3.Error`) is 503; anything
else is 500 through `action_failure()`. At the root the same failures are
the project's `error` and the others are listed whole.

Tokens (the design's Auth row): `host.toml [serve] machine_token_file` is
the one bearer at the root and under every prefix; a project's own `[serve]
token_file` is accepted beside it under that project's prefix alone, for
one release. Reads beyond loopback, and every write on any bind, demand it;
a non-loopback bind, `[serve] actions` or a project's `config_edit` without
it is a startup error naming the key, and a project's `machine_token_file`
is ignored, named once at start. `[serve] actions` in `host.toml` opens the
project actions under each prefix (`restart-supervisor` retired) and, at
the root, `POST /actions/run-sweep`, which appends its row to
`HOLOPHYTE_HOME/host-actions.jsonl` before it asks `systemctl` for the
sweep unit: no store owns the sweep, and the stores may be what is down.

`sweep.json` is the host sweep's record; this module reads `started`,
`ended` (epoch ms), `revision`, `pid`, `exit` and `projects`. The file's
writer is the host sweep.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
from pathlib import Path
from time import time
from urllib.parse import urlsplit

import store.read
from holophyte.admission import project_of
from holophyte.admission import state as admission_state
from holophyte.config import serve_config
from holophyte.config_tables import sweep_config
from holophyte.host import HostError, settings
from holophyte.pool_handoff import workers_on_previous_build
from holophyte.redact import known_secrets, outbound
from holophyte.reexec import SWEEP_UNIT, systemctl_user
from holophyte.report import host_label
from holophyte.serve import (
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
from holophyte.serve_actions import (
    ACTIONS,
    ACTIONS_PREFIX,
    action_failure,
)
from holophyte.serve_config import require_tomlkit
from holophyte.serve_watch import CODE_CHECK_SEC, adopted_socket
from holophyte.status import load_sweep_state
from holophyte.supervisor import SWEEPABLE_PHASES, factory_revision
from store.schema import SCHEMA_VERSION, SchemaNewer, _readable_from

PROJECTS_PREFIX = "/projects/"
MACHINE_TOKEN_KEY = "[serve] machine_token_file"
RUN_SWEEP = "run-sweep"
HOST_LEDGER = "host-actions.jsonl"
# The per-project actions a host daemon answers: the supervisor unit is
# retired in host mode, and `run-sweep` at the root takes its place.
HOST_ACTIONS = ACTIONS - {"restart-supervisor"}
# The sweep unit's `TimeoutStartSec`: a run started longer ago than this
# with no `ended` was killed.
SWEEP_TIMEOUT_SEC = 120
SWEEP_FIELDS = ("started", "ended", "revision", "pid", "exit", "projects")
# The sweep states `/attention` leaves alone.
SWEEP_OK = frozenset({"fresh", "running"})


def check_schema(project):
    """Raise `SchemaNewer` when `project`'s store is stamped newer than
    this build can read -- the check `store.open()` makes, read-only."""
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
    """One project's failure as the text its `error` carries, redacted."""
    if isinstance(bad, SchemaNewer):
        text = f"schema newer than build: {bad}"
    else:
        text = f"{type(bad).__name__}: {bad}"
    try:
        secrets = known_secrets(project.config())
    except (Exception, SystemExit):
        secrets = known_secrets(None)
    return outbound(text, secrets)


def beat_stale_ms(knobs):
    """A store's beat is stale past two host sweep intervals."""
    return 2 * knobs.sweep_sec * 1000


def project_summary(entry, now, stale_ms):
    """One registry entry as root `/status` lists it: its name, path and
    store (null when there is none), `error`, and from the store its
    schema version, admission, `project_row` (the store's row for this
    path, null when it has none), supervisor beat, live runs and the
    workers left on a previous build."""
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
    """The last sweep as root `/status` shows it: `sweep.json`'s fields and
    a `state`: `none` with no file, `unreadable`, `running` while a run
    started within `SWEEP_TIMEOUT_SEC` has not ended, `killed` past it,
    `fresh` when the last run ended within two intervals, else `stale`."""
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
    """Root `/status`: `(200, body)`."""
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
        "projects": [project_summary(entry, now, stale_ms)
                     for entry in server.host.projects()],
    }


def host_attention(server, now=None):
    """Root `/attention`: every project's items, each with its `project`,
    after a `sweep_stale` item when the sweep is not fresh; a project that
    cannot be read is one `project_error` item, one with no store or no
    row for its path an item naming `project add`. `(200, body)`."""
    now = int(time() * 1000) if now is None else now
    knobs = server.settings
    sweep = sweep_view(server.host.home, now, knobs.sweep_sec)
    items = []
    if sweep["state"] not in SWEEP_OK:
        items.append({"kind": "sweep_stale", "project": None,
                      "state": sweep["state"], "started": sweep["started"],
                      "ended": sweep["ended"], "level": "attention"})
    working = False
    for entry in server.host.projects():
        found, busy = _project_items(entry, now, beat_stale_ms(knobs))
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
    """Append `row` to the host ledger as one JSON line, synced to disk;
    OSError when it cannot be."""
    handle = os.open(home / HOST_LEDGER,
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(handle, "a") as out:
        out.write(json.dumps(row) + "\n")
        out.flush()
        os.fsync(out.fileno())


def run_sweep(home, body, now=None):
    """`POST /actions/run-sweep`: the ledger row, then `systemctl --user
    start --no-block` on the sweep unit -- a oneshot's `start` would wait
    out the run. A row that cannot be written runs nothing."""
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
    ok, detail = systemctl_user("start", SWEEP_UNIT, "--no-block")
    return 200, {**reply, "ok": ok, "detail": detail,
                 "recorded": str(home / HOST_LEDGER)}


def project_token(knobs):
    """A project's own `[serve] token_file` as a tuple of bearer values,
    empty when it has none or the file is refused."""
    if knobs.token_file is None:
        return ()
    try:
        return (load_token(knobs.token_file),)
    except SystemExit:
        return ()


class HostHandler(StatusHandler):
    """The project handler under `/projects/NAME/...`, a `Scope` per
    request; the host routes at the root."""

    def route(self, path):
        """`(scope, path)` for the request, scope None at the root; None
        once answered, when the registry itself cannot be read."""
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

    def do_PUT(self):
        found = self.route(urlsplit(self.path).path)
        if found is None:
            return
        scope, path = found
        if scope is None:
            return self.refuse()
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
        """The answer for a scope no route may read: a name outside the
        registry (404), a store this build cannot read (503); else None."""
        if scope.project is None:
            return 404, {"error": "not found", "path": scope.prefix,
                         "detail": f"no project {scope.prefix[len(PROJECTS_PREFIX):]!r}"
                                   f" in {self.server.host.path}"}
        try:
            check_schema(scope.project)
        except (Exception, SystemExit) as bad:
            return self.failure(scope, "", bad)
        return None

    def failure(self, scope, path, bad):
        """One project's failure as its answer: 503 for a newer schema or
        a store SQLite cannot read, else 500 with the traceback logged."""
        name = scope.unit_name
        if isinstance(bad, (SchemaNewer, sqlite3.Error)):
            if isinstance(bad, SchemaNewer) and self.server.code_check:
                self.server.code_check.check_now()
            return 503, {"error": project_error(scope.project, bad),
                         "project": name}
        code, body = action_failure(scope.project, scope.prefix + path, bad)
        return code, {**body, "project": name}


class HostServer(StatusServer):
    """A `StatusServer` for the registry: no project of its own, a `Scope`
    built per request from the entry its prefix names. The `[serve]` and
    `[console]` keys of `host.toml` are read once, at bind; its
    `[[project]]` list at every request whose file changed."""

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
        """`(scope, rest)` for `/projects/NAME/rest`; `(None, path)` for any
        other path. A name the registry does not hold still gets a scope --
        with no project, and the host's tokens -- so it is 401 or 404 as
        any unknown route is."""
        if not path.startswith(PROJECTS_PREFIX):
            return None, path
        name, slash, rest = path[len(PROJECTS_PREFIX):].partition("/")
        prefix = PROJECTS_PREFIX + name
        stale_ms = beat_stale_ms(self.settings)
        entry = self.host.project(name)
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
    """`(read, write)` bearer values for a host daemon on `bound_host`:
    the machine token beyond loopback for reads, on every bind for writes.
    SystemExit naming the key when something needs it and it is not set."""
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
    """Print, once, each entry the daemon cannot serve and each project
    `machine_token_file` the host's replaces."""
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
    """`factory.py --serve [ADDRESS]`: the host daemon. It serves on the
    socket the service manager handed over, else on `address`, else on
    `host.toml`'s `[serve] bind`; then as `serve.run()` does."""
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
