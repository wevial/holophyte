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

    def __init__(self, on_event=None):
        self.on_event = on_event
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
        if self.on_event is not None:
            self.tell(event)
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

    def tell(self, event):
        try:
            self.on_event(event)
        except Exception as error:
            print(f"[stream] dropped the turn's event hook: {error}", flush=True)
            self.on_event = None

    def output(self):
        return "\n".join(self.kept)

    def tail(self):
        return "\n".join(self.partial)[-TAIL_CHARS:]


def read_stream(proc, cmd, timeout, quiet, reap, on_event=None):
    """Return `(returncode, kept lines)`; a quiet pipe raises `TurnStalled`."""
    record = StreamRecord(on_event)
    clocks = Clocks(proc, cmd, timeout, quiet, reap, record)
    pending = b""
    with selectors.DefaultSelector() as selector:
        selector.register(proc.stdout.fileno(), selectors.EVENT_READ)
        while True:
            clocks.check()
            if not selector.select(TICK_S):
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), CHUNK)
            if not chunk:
                break
            *complete, pending = (pending + chunk).split(b"\n")
            for raw in complete:
                record.take(raw)
            if complete:
                clocks.heard = monotonic()
    if pending:
        record.take(pending)
    while True:
        clocks.check()
        try:
            return proc.wait(timeout=TICK_S), record.output()
        except subprocess.TimeoutExpired:
            continue


class Clocks:

    def __init__(self, proc, cmd, timeout, quiet, reap, record):
        self.proc, self.cmd, self.reap, self.record = proc, cmd, reap, record
        self.timeout, self.quiet = timeout, quiet
        self.started = self.heard = monotonic()

    def check(self):
        now = monotonic()
        if self.timeout is not None and now - self.started >= self.timeout:
            self.reap()
            raise subprocess.TimeoutExpired(self.cmd, self.timeout,
                                            output=self.record.tail())
        if (self.quiet and now - self.heard >= self.quiet
                and self.proc.poll() is None):
            self.reap()
            raise TurnStalled(self.cmd, self.quiet, self.record.lines,
                              self.record.last_tool, output=self.record.tail())
