"""`holo follow`: one line per run event, ledger entry and stall as it happens."""
import json
import re
import sys
from pathlib import Path
from time import sleep, time

from holophyte.config.project import Project
from holophyte.holo.grammar import FOLLOW
from holophyte.holo.render import age, clock, colour_on, moment, symbol, zone

EVERY = 2.0
AGO = re.compile(r"([0-9]+(?:\.[0-9]+)?)([smhd])")
UNIT_MS = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
LEDGER_MARKS = {"merge": "✓", "failure": "✗", "intervention": "!"}
EVENT, LEDGER, STALL = "event", "ledger", "stall"
LEDGER_TWIN = "intervention"
LEDGER_PAGE = 1000


def now_ms():
    return int(time() * 1000)


def ago_ms(text):
    match = AGO.fullmatch(text)
    if match is None:
        raise ValueError(f"--since takes an age such as 90s, 30m, 1h or 2d,"
                         f" got {text!r}")
    return int(float(match.group(1)) * UNIT_MS[match.group(2)])


def every_seconds(text):
    try:
        seconds = float(text)
    except ValueError:
        seconds = 0.0
    if not seconds > 0 or seconds == float("inf"):
        raise ValueError(f"the interval must be a number of seconds above"
                         f" zero, got {text!r}")
    return seconds


def first_line(text):
    lines = (text or "").strip().splitlines()
    return lines[0] if lines else ""


def event_entry(event):
    return {"stream": EVENT, "id": event.id, "at": event.at, "run": event.runId,
            "ticket": event.ticket, "kind": event.kind,
            "summary": first_line(event.summary)}


def ledger_entry(entry):
    from holophyte.serve.serve_runs import ledger_entry as view_entry
    body = view_entry(entry, {"run": entry.runId, "ticket": entry.ticket})
    return {"stream": LEDGER, "id": entry.id, **body,
            "summary": first_line(entry.text)}


def migration_key(row):
    return (row["at"], row["schema_from"], row["schema_to"])


def stall_entry(item, now):
    silent = age(item["heartbeat_age_ms"] // 1000)
    if item["kind"] == "supervisor":
        summary = f"supervisor silent: heartbeat {silent} ago"
    else:
        summary = (f"run {item['run']} silent: heartbeat {silent} ago"
                   f" ({item['phase']})")
    return {"stream": STALL, "at": now, "run": item.get("run"),
            "ticket": item.get("ticket"), "kind": item["kind"],
            "summary": summary}


def stalled(item):
    if item["kind"] == "stale_run":
        return ("stale_run", item["run"])
    if item["kind"] == "supervisor" and item["state"] == "stale":
        return ("supervisor",)
    return None


class Follow:
    def __init__(self, project, start):
        self.project = project
        self.start = start
        self.after = {EVENT: 0, LEDGER: 0}
        self.migrations = set()
        self.held = []
        self.stalled = set()

    def poll(self, now):
        import store.read
        conn = store.read.open_readonly(self.project.store_path)
        try:
            pool = self.held + self.events(conn) + self.ledger(conn)
        finally:
            conn.close()
        # Only entries dated before `now` print, so both reads of one poll
        # see the same moment and a later one cannot overtake an earlier.
        self.held = [entry for entry in pool if entry["at"] >= now]
        due = sorted((entry for entry in pool if entry["at"] < now),
                     key=lambda entry: entry["at"])
        return due + self.stalls(now)

    def events(self, conn):
        import store.read
        events = store.read.narrative_events_after(conn, self.after[EVENT],
                                                   self.start)
        if events:
            self.after[EVENT] = events[-1].id
        return [event_entry(event) for event in events
                if event.kind != LEDGER_TWIN]

    def ledger(self, conn):
        import store.read
        entries = []
        while True:
            page = store.read.ledger_after(conn, self.after[LEDGER],
                                           self.start, LEDGER_PAGE)
            entries += page
            if page:
                self.after[LEDGER] = page[-1].id
            if len(page) < LEDGER_PAGE:
                return ([ledger_entry(entry) for entry in entries]
                        + self.migrated(conn))

    def migrated(self, conn):
        from holophyte.serve.serve_runs import migration_rows
        rows = migration_rows(conn, self.start, LEDGER_PAGE,
                              str(self.project.path))
        fresh = [row for row in reversed(rows)
                 if migration_key(row) not in self.migrations]
        self.migrations.update(migration_key(row) for row in fresh)
        return [{"stream": LEDGER, **row, "summary": first_line(row["text"])}
                for row in fresh]

    def stalls(self, now):
        from holophyte.serve.views import attention
        code, body = attention(self.project, now)
        if code != 200:
            return []
        current = {stalled(item): item for item in body["items"]
                   if stalled(item) is not None}
        fresh = [stall_entry(item, now) for key, item in current.items()
                 if key not in self.stalled]
        self.stalled = set(current)
        return fresh


def mark(entry):
    if entry["stream"] == STALL:
        return "✗"
    if entry["stream"] == LEDGER:
        return LEDGER_MARKS.get(entry["kind"], ">")
    return ">"


def line(entry, tz=None, colour=False):
    summary = entry["summary"]
    if entry["stream"] == LEDGER:
        summary = f"{entry['kind']}: {summary}"
    return (f"{moment(entry['at'], tz):%H:%M:%S}  {symbol(mark(entry), colour)}"
            f"  {entry.get('ticket') or '-'}  {summary}")


def options(args):
    every = every_seconds(args.every[-1]) if args.every else EVERY
    return every, ago_ms(args.since[-1]) if args.since else 0


def check_intervals(args):
    try:
        if args.command is FOLLOW:
            options(args)
        elif getattr(args, "watch", None) is not None:
            if args.json:
                raise ValueError("--watch redraws the page; it takes no --json")
            every_seconds(args.watch)
    except ValueError as bad:
        args.leaf.error(str(bad))


def follow(args, target, zone_name):
    every, ago = options(args)
    project = Project.locate(Path(target), adopt=False)
    if not project.store_path.exists():
        print(f"[holo2] holo follow: {project.store_path} does not exist",
              file=sys.stderr)
        return 1
    tz, colour = zone(zone_name), colour_on(sys.stdout)
    start = now_ms() - ago
    tracker = Follow(project, start)
    print(f"[holo2] following {project.path} from {clock(start, tz, True)},"
          f" every {every:g} s; Ctrl-C ends", file=sys.stderr, flush=True)
    try:
        while True:
            for entry in tracker.poll(now_ms()):
                print(json.dumps(entry) if args.json else line(entry, tz, colour),
                      flush=True)
            sleep(every)
    except KeyboardInterrupt:
        return 0
