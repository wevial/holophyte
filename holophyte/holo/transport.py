"""A `holo` command run on the client config's host over ssh or http, or here."""
import http.client
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, urlencode

from holophyte.holo.grammar import FOLLOW, JSON, READS, REQUIRED, arguments
from holophyte.holo.resolve import (
    DEFAULT,
    ENVIRONMENT,
    HOST,
    HOST_FORMS,
    REMOTE_COMMAND,
    TIMEZONE,
    TOKEN_FILE,
    TRANSPORT_KEY,
    URL,
    client_path,
    refuse,
)
from holophyte.holo.results import (
    PREFIX,
    STDIN,
    build_result,
    human_line,
    parse_run,
)

TRANSPORT = "HOLO_TRANSPORT"
LOCAL = "local"
SSH = "ssh"
HTTP = "http"
HTTP_TIMEOUT_S = 30
SSH_FAILED = 255
HOST_ONLY = {"--serve": "the daemon", "--supervise": "the acting sweep"}
CLIENT_ONLY = {("story", "file"): "reads a story directory on this machine"}


def road(config):
    if config.get(TRANSPORT_KEY):
        return config[TRANSPORT_KEY]
    return SSH if config.get(HOST) else HTTP if config.get(URL) else None


def remote_host(config):
    choice = os.environ.get(TRANSPORT)
    if choice == LOCAL:
        return None
    if choice:
        refuse(f"[holo2] {TRANSPORT}={choice!r}: the one value is {LOCAL!r}")
    return config.get(URL if road(config) == HTTP else HOST)


def json_form(command):
    return JSON in command.flags or command.records is not None


def seat_project(option, default):
    if option is not None and not option.strip():
        refuse("[holo2] -p is empty; give a project name or path")
    return option or os.environ.get(ENVIRONMENT) or default


def remote_words(args, project):
    command = args.command
    values, note = arguments(args, command)
    positionals = [value for value in values if value is not None]
    words = list(command.words)
    if note is not None:
        words.append(f"--note={note}")
    for flag in command.flags:
        given = getattr(args, flag.dest)
        if flag is JSON or flag.const is not None or not given:
            continue
        if flag.metavar is None:
            words.append(flag.name)
            continue
        for value in given:
            words += ([flag.name, *value] if isinstance(value, list)
                      else [f"{flag.name}={value}"])
    if project is not None:
        words += [f"-p={project}"] if project.startswith("-") else ["-p", project]
    if args.verbose:
        words.append("--verbose")
    words += ["--json"] * json_form(command)
    return words + ["--", *positionals] * bool(positionals)


def remote_line(config, words):
    return (f"{TRANSPORT}={LOCAL} {config.get(REMOTE_COMMAND, 'holo')}"
            f" {shlex.join(words)}")


def ssh(host, line, stdin, capture):
    sys.stdout.flush()
    sys.stderr.flush()
    tail = ""
    with tempfile.TemporaryFile() as out:
        try:
            child = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", host, line], stdin=stdin,
                stdout=out if capture else None, stderr=subprocess.PIPE)
        except OSError as bad:
            return SSH_FAILED, "", str(bad)
        with child:
            for raw in child.stderr:
                text = raw.decode(errors="replace")
                sys.stderr.write(text)
                sys.stderr.flush()
                tail = text.strip() or tail
        out.seek(0)
        return child.returncode, out.read().decode(errors="replace"), tail


def ssh_failed(host, tail):
    failure = f"ssh to {host} failed: {tail or 'exit 255'}"
    print(PREFIX + failure, file=sys.stderr)
    return failure


def call(host, line, label, stdin=subprocess.DEVNULL, capture=False):
    print(f"[holo2] {label} via {SSH} to {host}", file=sys.stderr)
    code, text, tail = ssh(host, line, stdin, capture)
    if code == SSH_FAILED:
        return 1, None, ssh_failed(host, tail)
    return code, text, tail


def drain(stream, tail):
    for raw in stream:
        text = raw.decode(errors="replace")
        sys.stderr.write(text)
        sys.stderr.flush()
        tail[0] = text.strip() or tail[0]


def stream(host, line, label, each):
    print(f"[holo2] {label} via {SSH} to {host}", file=sys.stderr, flush=True)
    sys.stdout.flush()
    try:
        child = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", host, line],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
    except OSError as bad:
        ssh_failed(host, str(bad))
        return 1
    tail = [""]
    reader = threading.Thread(target=drain, args=(child.stderr, tail), daemon=True)
    reader.start()
    try:
        for raw in child.stdout:
            each(raw.decode(errors="replace"))
        child.wait()
        reader.join()
    except KeyboardInterrupt:
        child.terminate()
        child.wait()
        return 0
    if child.returncode == SSH_FAILED:
        ssh_failed(host, tail[0])
        return 1
    return child.returncode


def followed(args, timezone):
    from holophyte.holo.follow import line
    from holophyte.holo.render import colour_on, zone
    tz, colour = zone(timezone), colour_on(sys.stdout)

    def each(text):
        body = document(text)
        if body is None:
            sys.stdout.write(text)
        elif args.json:
            print(json.dumps({**body, "transport": SSH}))
        else:
            print(line(body, tz, colour))
        sys.stdout.flush()
    return each


def watched(args, host, line, timezone):
    from holophyte.holo.follow import every_seconds
    from holophyte.holo.status_page import show_snap, watch
    print(f"[holo2] holo status --watch via {SSH} to {host}", file=sys.stderr)

    def frame(out, colour):
        code, text, tail = ssh(host, line, subprocess.DEVNULL, True)
        body = document(text) if code != SSH_FAILED else None
        if body is not None:
            show_snap(body, timezone, out, colour)
        elif code == SSH_FAILED:
            print(PREFIX + f"ssh to {host} failed: {tail or 'exit 255'}", file=out)
        else:
            out.write(text)
    return watch(every_seconds(args.watch), frame, timezone)


def run_project(argv, host, config):
    if road(config) == HTTP:
        return unrouted(None, "holo project")
    line = remote_line(config, argv)
    return call(host, line, f"holo {' '.join(argv[:2])}")[0]


def ticket_stdin(args, stack):
    if args.command.words != ("file",):
        return subprocess.DEVNULL
    if args.arg0 == STDIN:
        return None
    try:
        stream = stack.enter_context(open(args.arg0, "rb"))
    except OSError as bad:
        args.leaf.error(f"cannot read {args.arg0}: {bad.strerror}")
    args.arg0 = STDIN
    return stream


def named_project(args, config, label, over):
    project = seat_project(args.project, config.get(DEFAULT))
    if project is None and args.command.mode not in HOST_FORMS:
        refuse(f"[holo2] {label} needs a project and no source names one:"
               f" -p NAME|PATH, {ENVIRONMENT} and {DEFAULT} in {client_path()}"
               f" (over {over} the current repository does not answer)")
    return project


def run(args, host, config):
    if road(config) == HTTP:
        return run_http(args, host, config)
    command, label = args.command, f"holo {' '.join(args.command.words)}"
    if command.mode in HOST_ONLY:
        refuse(f"[holo2] {label} runs on the host itself: it starts"
               f" {HOST_ONLY[command.mode]}, a long-lived process there; run"
               f" it on {host}, not over {SSH}")
    if command.words in CLIENT_ONLY:
        refuse(f"[holo2] {label} {CLIENT_ONLY[command.words]}, which {SSH}"
               f" does not carry; run it on {host}")
    project = named_project(args, config, label, SSH)
    with ExitStack() as stack:
        stdin = ticket_stdin(args, stack)
        words = remote_words(args, project)
        line = remote_line(config, words)
        if command is FOLLOW:
            return stream(host, line, label,
                          followed(args, config.get(TIMEZONE)))
        if getattr(args, "watch", None) is not None:
            return watched(args, host, line, config.get(TIMEZONE))
        if run_page(args):
            files = [*words[:1], "--files", *words[1:]]
            line += f" && {remote_line(config, files)}"
        code, text, tail = call(host, line, label, stdin, json_form(command))
    if text is not None:
        return render(args, code, text, tail, config.get(TIMEZONE))
    if command.records is not None and args.json:
        result = build_result(args, code, [tail], (None, None))
        print(json.dumps({**result, "transport": SSH}))
    return code


def run_page(args):
    from holophyte.holo.reads import run_part
    return (args.command.words == ("run",) and not args.json
            and run_part(args) is None)


def document(text):
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def render(args, code, text, tail, timezone):
    page = run_page(args)
    head, _, files = text.partition("\n") if page else (text, "", "")
    body = document(head)
    if body is None:
        sys.stdout.write(text)
        return code
    if args.json:
        print(json.dumps({**body, "transport": SSH}))
    elif page and "error" not in body:
        from holophyte.holo.run_page import show_page
        files = document(files) or {"error": tail or "the host sent no files"}
        show_page(body, files, timezone)
        return 0
    elif args.command.mode == "--report":
        from holophyte.holo.report_page import show_body
        return show_body(args, body, timezone) if code == 0 else code
    elif args.command in READS:
        from holophyte.holo.reads import show
        show(args, f"holo {' '.join(args.command.words)}", code == 0, body)
    elif args.command.records is not None:
        detail = body.get("detail", "")
        said = tail.removeprefix(PREFIX) == detail.removeprefix(PREFIX)
        if body.get("ok") or not said:
            print(human_line(body, detail.splitlines()),
                  file=sys.stdout if body.get("ok") else sys.stderr)
    else:
        from holophyte.holo.status_page import show_snap
        show_snap(body, timezone)
    return code


class Route(NamedTuple):
    method: str
    path: str
    body: tuple = ()
    local: tuple = ()


HTTP_ROUTES = {
    ("runs",): Route("GET", "/runs"),
    ("run",): Route("GET", "/runs/{N}"),
    ("attention",): Route("GET", "/attention"),
    ("board",): Route("GET", "/board", local=(("editable", False),)),
    ("ticket",): Route("GET", "/tickets/{KEY}"),
    ("requeue",): Route("POST", "/actions/requeue", ("ticket", "note")),
    ("send-back",): Route("POST", "/actions/send-back",
                          ("run", "note", "author")),
    ("hold",): Route("POST", "/actions/hold", ("note",)),
    ("release",): Route("POST", "/actions/release-hold", ("note",)),
    ("pause",): Route("POST", "/actions/pause", ("run", "note")),
    ("resume",): Route("POST", "/actions/resume", ("ticket", "note")),
    ("abort",): Route("POST", "/actions/abort", ("run", "note", "close")),
    ("start",): Route("POST", "/actions/launch-loop"),
}


class Unredirected(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     Unredirected())


class Failed(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


class Daemon:
    def __init__(self, url, config):
        self.url = url.rstrip("/")
        self.token_file = Path(config[TOKEN_FILE]).expanduser()
        try:
            self.token = self.token_file.read_text().strip()
        except OSError as bad:
            refuse(f"[holo2] {TOKEN_FILE} {self.token_file}: {bad.strerror}")
        except UnicodeDecodeError:
            refuse(f"[holo2] {TOKEN_FILE} {self.token_file} is not text")
        if not self.token:
            refuse(f"[holo2] {TOKEN_FILE} {self.token_file} is empty")
        if not (self.token.isascii() and self.token.isprintable()):
            raise Failed(f"{TOKEN_FILE} {self.token_file} is not one line of"
                         " printable ASCII, so no bearer header carries it;"
                         " nothing sent")

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            self.url + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/json",
                     "Content-Type": "application/json"})
        try:
            with DIRECT.open(request, timeout=HTTP_TIMEOUT_S) as response:
                code, raw = response.status, response.read()
        except urllib.error.HTTPError as answered:
            code, raw = answered.code, answered.read()
        except (OSError, http.client.HTTPException) as bad:
            raise Failed(f"http to {self.url} failed:"
                         f" {getattr(bad, 'reason', None) or bad}") from None
        if code == 401:
            raise Failed(f"{self.url} answered 401: the token in"
                         f" {self.token_file} is not one it accepts")
        answer = document(raw.decode(errors="replace"))
        if answer is None:
            raise Failed(f"{self.url}{path} answered {code} with no JSON"
                         " object; is url the host daemon?")
        return code, answer


def failure(args, failed):
    print(PREFIX + str(failed), file=sys.stderr)
    if args is not None and args.command.records is not None and args.json:
        result = build_result(args, failed.code, [str(failed)], (None, None))
        print(json.dumps({**result, "transport": HTTP}))
    return failed.code


def unrouted(args, label, by_ssh=True):
    other = (f"transport = \"ssh\" in {client_path()} runs it" if by_ssh
             else f"{SSH} does not carry it either; run it on the host")
    return failure(args, Failed(
        f"{label} has no HTTP route on the host daemon; {other}"))


def run_http(args, url, config):
    command, label = args.command, f"holo {' '.join(args.command.words)}"
    route = HTTP_ROUTES.get(command.words)
    if getattr(args, "foreground", False):
        return unrouted(args, f"{label} --foreground")
    if route is None:
        return unrouted(args, label, command.mode not in HOST_ONLY
                        and command.words not in CLIENT_ONLY)
    project = named_project(args, config, label, HTTP)
    prefix = "" if project is None else "/projects/" + quote(project, safe="")
    known = fields(args, route)
    try:
        daemon = Daemon(url, config)
        print(f"[holo2] {label} via {HTTP} to {daemon.url}", file=sys.stderr)
        if route.method == "GET":
            return read_over(args, daemon, prefix, route, config.get(TIMEZONE))
        return write_over(args, daemon, prefix, route, known)
    except Failed as failed:
        return failure(args, failed)


def fields(args, route):
    if route.method == "GET":
        return {}
    command = args.command
    values, note = arguments(args, command)
    if note is not None and "note" not in route.body:
        args.leaf.error(f"{route.method} {route.path} takes no note; a hold"
                        " is released over http with holo release NOTE")
    if command.note == REQUIRED and (note is None or not note.strip()):
        args.leaf.error(f"holo {' '.join(command.words)}'s note must say why"
                        " (non-blank text)")
    author = getattr(args, "author", None)
    known = {"note": note, "close": bool(getattr(args, "close_pr", False)),
             "author": author[-1] if author else None}
    first = command.takes[:1]
    if first == ("KEY",):
        known["ticket"] = values[0]
    if first == ("RUN",):
        known["run"] = parse_run(values[0])
        if known["run"] is None:
            args.leaf.error("RUN must be a positive 64-bit run id")
    return known


def said(path, code, body):
    detail = f": {body['detail']}" if body.get("detail") else ""
    return f"{path} answered {code}: {body.get('error') or 'not found'}{detail}"


def read_over(args, daemon, prefix, route, timezone):
    from holophyte.holo.reads import RUN_PARTS, exit_code, show
    parts = [part for part in RUN_PARTS if getattr(args, part, False)]
    if len(parts) > 1:
        args.leaf.error("give at most one of --files, --ledger and --turns")
    named = quote(getattr(args, "arg0", None) or "", safe="")
    path = route.path.format(N=named, KEY=named)
    path += "".join(f"/{part}" for part in parts)
    limit = getattr(args, "limit", None)
    query = f"?{urlencode({'limit': limit[-1]})}" if limit else ""
    code, body = daemon.call("GET", prefix + path + query)
    if code == 200:
        body.update(route.local)
    if code == 200 and run_page(args):
        from holophyte.holo.run_page import show_page
        show_page(body, daemon.call("GET", f"{prefix}{path}/files")[1],
                  timezone)
    elif args.json:
        print(json.dumps({**body, "transport": HTTP}))
    else:
        show(args, f"GET {path}", code == 200, body)
    return exit_code(code)


def ticket_run(daemon, prefix, key):
    path = f"/tickets/{quote(key, safe='')}"
    code, body = daemon.call("GET", prefix + path)
    if code != 200:
        raise Failed(f"GET {said(path, code, body)}")
    if body.get("run") is None:
        raise Failed(f"{key} has no live run on the host; nothing sent")
    return body["run"]


def write_over(args, daemon, prefix, route, known):
    if "run" in route.body and "run" not in known:
        known["run"] = ticket_run(daemon, prefix, known["ticket"])
    payload = {key: known[key] for key in route.body
               if known.get(key) is not None}
    code, reply = daemon.call(route.method, prefix + route.path, payload)
    if code == 404 and not reply.get("detail"):
        raise Failed(f"{route.method} {route.path} answered 404: the host's"
                     " [serve] actions is not true, so it serves no action")
    if code != 200:
        raise Failed(f"{route.method} {said(route.path, code, reply)}",
                     2 if code == 400 else 1)
    from holophyte.holo.render import colour_on
    ok = bool(reply.get("ok"))
    shown = {"action": " ".join(args.command.words), "recorded": None,
             **reply, "ok": ok}
    lines = [line.removeprefix(PREFIX)
             for line in str(reply.get("detail") or "").splitlines()]
    if args.json:
        print(json.dumps({**reply, "transport": HTTP}))
    else:
        out = sys.stdout if ok else sys.stderr
        print(human_line(shown, lines, colour_on(out)), file=out)
    return 0 if ok else 1
