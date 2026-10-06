"""`holo mcp`'s tools as data: each row's input schema, tier and `holo` argv."""
import json
import subprocess
import sys
from typing import Callable, NamedTuple

READ = 0
TIMEOUT_SEC = 120
BOARD_DIFF_SUMMARY = "[holo2] board diff: "
VIEWS = ("detail", "files", "ledger", "turns")


class Field(NamedTuple):
    name: str
    schema: dict
    words: Callable | None


class Tool(NamedTuple):
    name: str
    description: str
    words: tuple
    fields: tuple = ()
    required: tuple = ()
    tier: int = READ
    json: bool = True
    answered: str | None = None


class Answer(NamedTuple):
    error: bool
    text: str
    body: dict | None = None


def option(flag):
    return lambda value: [f"{flag}={value}"]


PROJECT = Field("project", {
    "type": "string", "minLength": 1,
    "description": "a registered [serve] name or a repository path; omitted,"
                   " the project holo finds itself, or the host form"},
    option("--project"))
SINCE = Field("since", {
    "type": "string", "minLength": 1,
    "description": "the window: Nh, Nd or all; default 7d"}, option("--since"))
LIMIT = Field("limit", {"type": "integer", "minimum": 1,
                        "description": "at most this many runs"},
              option("--limit"))
RUN = Field("run", {"type": "integer", "minimum": 1,
                    "description": "the run's id"}, None)
VIEW = Field("view", {
    "enum": list(VIEWS), "default": VIEWS[0],
    "description": "the run's detail, or its files, ledger or agent turns"},
    lambda value: [] if value == VIEWS[0] else [f"--{value}"])
KEY = Field("key", {"type": "string", "minLength": 1,
                    "description": "the ticket's key, such as HOLO-1"}, None)

TOOLS = (
    Tool("status", "what the factory is doing now: one project, or the host"
         " when none is named (holo status --json)", ("status",), (PROJECT,)),
    Tool("attention", "what waits on the operator: one project's, or the"
         " host's when none is named (holo attention --json)",
         ("attention",), (PROJECT,)),
    Tool("report", "a window's counts, runs and notes (holo report --json)",
         ("report",), (PROJECT, SINCE)),
    Tool("runs", "recent ended runs (holo runs --json)", ("runs",),
         (PROJECT, LIMIT)),
    Tool("run", "one run's detail, files, ledger or turns (holo run N --json)",
         ("run",), (PROJECT, RUN, VIEW), required=("run",)),
    Tool("board", "the board's columns (holo board --json)", ("board",),
         (PROJECT,)),
    Tool("ticket", "one ticket's detail (holo ticket KEY --json)",
         ("ticket",), (PROJECT, KEY), required=("key",)),
    Tool("board_diff", "where the store's ready queue differs from the"
         " board's, as text (holo board diff)", ("board", "diff"),
         (PROJECT,), json=False, answered=BOARD_DIFF_SUMMARY),
    Tool("sweep_preview", "what an acting sweep would do, as text; it writes"
         " only sightings (holo sweep)", ("sweep",), (PROJECT,), json=False),
)


def input_schema(tool):
    return {"type": "object",
            "properties": {field.name: field.schema for field in tool.fields},
            "required": list(tool.required), "additionalProperties": False}


def argv(tool, arguments):
    words, positionals = list(tool.words), []
    for field in tool.fields:
        value = arguments.get(field.name)
        if value is None:
            continue
        if field.words is None:
            positionals.append(str(value))
        else:
            words += field.words(value)
    words += ["--json"] * tool.json
    return [sys.executable, "-m", "holophyte.holo", *words,
            *["--", *positionals] * bool(positionals)]


def answered(tool, code, out):
    if code == 0:
        return True
    lines = out.strip().splitlines()
    return (code == 1 and tool.answered is not None and bool(lines)
            and lines[-1].startswith(tool.answered))


def run_tool(tool, arguments):
    try:
        done = subprocess.run(argv(tool, arguments), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True,
                              timeout=TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return Answer(True, f"[holo2] {tool.name}: no answer in {TIMEOUT_SEC}s")
    said = "\n".join(text.strip() for text in (done.stdout, done.stderr)
                     if text.strip())
    body = document(done.stdout) if tool.json else None
    if not answered(tool, done.returncode, done.stdout):
        text = json.dumps(body) if body is not None else said
        return Answer(True, text or f"[holo2] {tool.name}: exit"
                      f" {done.returncode}", body)
    if not tool.json:
        return Answer(False, done.stdout)
    if body is None:
        return Answer(True, f"[holo2] {tool.name}: no JSON object printed\n"
                      + said)
    return Answer(False, json.dumps(body), body)


def document(text):
    try:
        body = json.loads(text)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None
