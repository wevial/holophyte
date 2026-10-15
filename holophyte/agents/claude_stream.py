import json
import os
import selectors
import subprocess
from time import monotonic

TAIL_CHARS = 4000
CHUNK = 65536
TICK_S = 1


class TurnStalled(subprocess.TimeoutExpired):

    def __init__(self, cmd, quiet, lines, last_tool, output=None):
        super().__init__(cmd, quiet, output=output)
        self.quiet = quiet
        self.lines = lines
        self.last_tool = last_tool

    def __str__(self):
        return (f"Command '{self.cmd}' printed no stream event for"
                f" {self.quiet} seconds")


class StreamRecord:

    def __init__(self):
        self.kept = []
        self.partial = []
        self.lines = 0
        self.last_tool = None

    def take(self, raw):
        self.lines += 1
        line = raw.decode("utf-8", "replace").rstrip("\r")
        try:
            event = json.loads(line)
        except ValueError:
            self.kept.append(line)
            self.partial.append(line)
            return
        if not isinstance(event, dict):
            return
        if event.get("type", "result") == "result":
            self.kept.append(line)
        elif event.get("type") == "assistant":
            self.assistant(event)

    def assistant(self, event):
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                self.last_tool = block["name"]
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                self.partial.append(block["text"])

    def output(self):
        return "\n".join(self.kept)

    def tail(self):
        return "\n".join(self.partial)[-TAIL_CHARS:]


def read_stream(proc, cmd, timeout, quiet, reap):
    """Return `(returncode, kept lines)`; a quiet pipe raises `TurnStalled`."""
    record = StreamRecord()
    started = heard = monotonic()
    pending = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout.fileno(), selectors.EVENT_READ)
        while True:
            now = monotonic()
            if timeout is not None and now - started >= timeout:
                reap()
                raise subprocess.TimeoutExpired(cmd, timeout, output=record.tail())
            if quiet and now - heard >= quiet:
                reap()
                raise TurnStalled(cmd, quiet, record.lines, record.last_tool,
                                  output=record.tail())
            if not selector.select(TICK_S):
                continue
            chunk = os.read(proc.stdout.fileno(), CHUNK)
            if not chunk:
                break
            *complete, pending = (pending + chunk).split(b"\n")
            for raw in complete:
                record.take(raw)
            if complete:
                heard = monotonic()
    if pending:
        record.take(pending)
    left = None if timeout is None else max(0, timeout - (monotonic() - started))
    try:
        return proc.wait(timeout=left), record.output()
    except subprocess.TimeoutExpired:
        reap()
        raise subprocess.TimeoutExpired(cmd, timeout, output=record.tail()) from None
