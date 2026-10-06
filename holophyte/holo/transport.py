"""A `holo` command run on the client config's host over ssh, or locally."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
from contextlib import ExitStack

from holophyte.holo.grammar import JSON, READS, arguments
from holophyte.holo.resolve import (
    DEFAULT,
    ENVIRONMENT,
    HOST,
    HOST_FORMS,
    REMOTE_COMMAND,
    client_path,
    refuse,
)
from holophyte.holo.results import PREFIX, STDIN, build_result, human_line

TRANSPORT = "HOLO_TRANSPORT"
LOCAL = "local"
SSH = "ssh"
SSH_FAILED = 255
HOST_ONLY = {"--serve": "the daemon", "--supervise": "the acting sweep"}
CLIENT_ONLY = {("story", "file"): "reads a story directory on this machine"}


def remote_host(config):
    choice = os.environ.get(TRANSPORT)
    if choice == LOCAL:
        return None
    if choice:
        refuse(f"[holo2] {TRANSPORT}={choice!r}: the one value is {LOCAL!r}")
    return config.get(HOST)


def json_form(command):
    return JSON in command.flags or command.records is not None


def seat_project(option, default):
    if option is not None and not option.strip():
        refuse("[holo2] -p is empty; give a project name or path")
    return option or os.environ.get(ENVIRONMENT) or default


def remote_words(args, project):
    command = args.command
    values, note = arguments(args, command)
    words = [*command.words, *(value for value in values if value is not None)]
    if note is not None:
        words.append(f"--note={note}")
    for flag in command.flags:
        given = getattr(args, flag.dest)
        if flag is JSON or not given:
            continue
        if flag.metavar is None:
            words.append(flag.name)
            continue
        for value in given:
            words += ([flag.name, *value] if isinstance(value, list)
                      else [f"{flag.name}={value}"])
    if project is not None:
        words += ["-p", project]
    if args.verbose:
        words.append("--verbose")
    return words + ["--json"] * json_form(command)


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


def call(host, line, label, stdin=subprocess.DEVNULL, capture=False):
    print(f"[holo2] {label} via {SSH} to {host}", file=sys.stderr)
    code, text, tail = ssh(host, line, stdin, capture)
    if code == SSH_FAILED:
        failure = f"ssh to {host} failed: {tail or 'exit 255'}"
        print(PREFIX + failure, file=sys.stderr)
        return 1, None, failure
    return code, text, tail


def run_project(argv, host, config):
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


def run(args, host, config):
    command, label = args.command, f"holo {' '.join(args.command.words)}"
    if command.mode in HOST_ONLY:
        refuse(f"[holo2] {label} runs on the host itself: it starts"
               f" {HOST_ONLY[command.mode]}, a long-lived process there; run"
               f" it on {host}, not over {SSH}")
    if command.words in CLIENT_ONLY:
        refuse(f"[holo2] {label} {CLIENT_ONLY[command.words]}, which {SSH}"
               f" does not carry; run it on {host}")
    project = seat_project(args.project, config.get(DEFAULT))
    if project is None and command.mode not in HOST_FORMS:
        refuse(f"[holo2] {label} needs a project and no source names one:"
               f" -p NAME|PATH, {ENVIRONMENT} and {DEFAULT} in {client_path()}"
               f" (over {SSH} the current repository does not answer)")
    with ExitStack() as stack:
        stdin = ticket_stdin(args, stack)
        line = remote_line(config, remote_words(args, project))
        code, text, tail = call(host, line, label, stdin, json_form(command))
    if text is not None:
        return render(args, code, text, tail)
    if args.json and command.records is not None:
        result = build_result(args, code, [tail], (None, None))
        print(json.dumps({**result, "transport": SSH}))
    return code


def render(args, code, text, tail):
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        sys.stdout.write(text)
        return code
    if args.json:
        print(json.dumps({**body, "transport": SSH}))
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
        from holophyte.cli.status import render as project_lines
        from holophyte.cli.status import render_host
        print("\n".join(render_host(body) if "registry" in body
                        else project_lines(body)))
    return code
