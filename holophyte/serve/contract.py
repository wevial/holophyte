import argparse
import sys
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from holophyte.serve.contract_emit import check, render, write

ROOT = Path(__file__).resolve().parents[2]
COMMAND = "python3 -m holophyte.serve.contract"
SCHEMA_DIR = Path("console/contract")
ZOD_FILE = Path("console/src/lib/contract.gen.ts")

Level = Literal["attention", "working", "none"]


class ActiveRoute(TypedDict):
    command: str | None
    down: NotRequired[bool]
    fallback: NotRequired[str]


class RouteLabels(TypedDict):
    implementer: str
    reviewer: str
    reviewer_fallback: str | None
    adjudicator: str
    writer: str
    trimmer: str


class ToilWindow(TypedDict):
    interventions: int
    merged: int
    per_merge: float | None
    by_action: dict[str, int]
    by_actor: dict[str, int]


Toil = TypedDict("Toil", {"24h": ToilWindow, "7d": ToilWindow})


class Daemon(TypedDict):
    started_ms: int
    pid: int


class Supervisor(TypedDict):
    state: Literal["live", "stale", "none"]
    pid: int | None
    heartbeat_age_ms: int | None
    host: str | None


class Thresholds(TypedDict):
    heartbeat_stale_ms: int
    strikes: int
    run_cap: float


class StatusRun(TypedDict):
    id: int
    ticket: str
    ticket_url: str | None
    title: str
    phase: str
    stop_requested: str | None
    stop_action: Literal["pause", "abort", "abort_close"] | None
    started_ms: int
    heartbeat_age_ms: int
    elapsed_ms: int
    working_ms: int
    work_started_ms: int | None
    agent_ms: int
    verify_ms: int
    run_count: int
    verify_started_ms: int | None
    time_box_ms: int | None
    round: int
    strikes: int
    host: str | None


class QueuedTicket(TypedDict):
    ticket: str
    ticket_url: str | None
    title: str
    reason: str | None
    pr_url: str | None
    waiting_ms: int


class Status(TypedDict):
    project: str
    admission: str
    hold_note: str | None
    schema_version: int
    active_routes: dict[str, ActiveRoute]
    route_labels: RouteLabels
    workers_on_previous_build: int
    host: str
    now: int
    toil: Toil
    daemon: Daemon
    supervisor: Supervisor
    thresholds: Thresholds
    actions: bool
    config_edit: bool
    runs: list[StatusRun]
    queued: list[QueuedTicket]


class PullRequestFacts(TypedDict):
    number: int | None
    checks: str | None
    review: str | None
    threads: int | None
    open_threads: int | str | None
    title: str | None


class Triage(TypedDict):
    choice: str | None
    confidence: float | None
    backend: str | None
    model: str | None
    requeued: bool | None


class BlockedItem(TypedDict):
    kind: Literal["blocked"]
    ticket: str
    ticket_url: str | None
    question: str | None
    run: int | None
    asked_ms: int | None
    pr_url: str | None
    level: Literal["attention"]
    project: NotRequired[str]


class PrOpenItem(TypedDict):
    kind: Literal["pr_open"]
    ticket: str
    ticket_url: str | None
    title: str
    run: int | None
    pr_url: str
    park_kind: str | None
    reason: str
    asked_ms: int | None
    pr: PullRequestFacts
    level: Literal["attention"]
    project: NotRequired[str]


class PausedItem(TypedDict):
    kind: Literal["paused"]
    ticket: str
    ticket_url: str | None
    title: str
    note: str | None
    run: int | None
    asked_ms: int | None
    pr_url: str | None
    level: Literal["attention"]
    project: NotRequired[str]


class StaleRunItem(TypedDict):
    kind: Literal["stale_run"]
    run: int
    ticket: str
    ticket_url: str | None
    phase: str
    heartbeat_age_ms: int
    pr_url: str | None
    level: Literal["attention"]
    project: NotRequired[str]


class FailedItem(TypedDict):
    kind: Literal["failed"]
    run: int
    ticket: str
    ticket_url: str | None
    reason: str | None
    ended_ms: int
    attempt: int
    pr_url: str | None
    triage: Triage | None
    level: Literal["attention"]
    project: NotRequired[str]


class StuckWaitItem(TypedDict):
    kind: Literal["stuck_wait"]
    ticket: str
    ticket_url: str | None
    title: str
    dependency: str
    dependency_state: Literal["needs_spec", "abandoned", "parked", "failed"]
    reason: str | None
    behind: int
    level: Literal["attention"]
    project: NotRequired[str]


class SupervisorItem(TypedDict):
    kind: Literal["supervisor"]
    state: Literal["stale", "none"]
    heartbeat_age_ms: int | None
    level: Literal["attention"]
    project: NotRequired[str]


AttentionItem = (BlockedItem | PrOpenItem | PausedItem | StaleRunItem
                 | FailedItem | StuckWaitItem | SupervisorItem)


class Attention(TypedDict):
    level: Level
    items: list[AttentionItem]
    now: int
    project: str


class Peers(TypedDict):
    self: str
    peers: list[str]


class Build(TypedDict):
    daemon: str | None
    sweep: str | None
    head: str | None


class ConsoleBuild(TypedDict):
    served: str | None
    tree: str | None
    stale: bool
    reason: str | None
    failed_ms: int | None
    tried: str | None


SweepState = Literal["none", "unreadable", "running", "killed", "fresh",
                     "stale"]


class Sweep(TypedDict):
    started: int | None
    ended: int | None
    revision: str | None
    pid: int | None
    exit: int | None
    projects: dict[str, str] | None
    error: str | None
    state: SweepState


class HostRun(TypedDict):
    id: int
    ticket: str
    phase: str
    heartbeat_age_ms: int


class HostProject(TypedDict):
    name: str
    path: str
    store: str | None
    error: str | None
    host: str | None
    schema_version: int | None
    admission: str | None
    hold_note: str | None
    project_row: int | None
    supervisor: Supervisor | None
    runs: list[HostRun]
    workers_on_previous_build: int | None


class HostStatus(TypedDict):
    now: int
    daemon: Daemon
    build: Build
    console: ConsoleBuild
    sweep: Sweep
    actions: bool
    projects: list[HostProject]


class SweepStaleItem(TypedDict):
    kind: Literal["sweep_stale"]
    project: None
    state: SweepState
    started: int | None
    ended: int | None
    level: Literal["attention"]


class ProjectErrorItem(TypedDict):
    kind: Literal["project_error"]
    project: str
    path: str
    level: Literal["attention"]
    error: str


class NoStoreItem(TypedDict):
    kind: Literal["no_store"]
    project: str
    path: str
    level: Literal["attention"]
    detail: str


class NoProjectRowItem(TypedDict):
    kind: Literal["no_project_row"]
    project: str
    path: str
    level: Literal["attention"]
    detail: str


HostAttentionItem = (SweepStaleItem | ProjectErrorItem | NoStoreItem
                     | NoProjectRowItem | AttentionItem)


class HostAttention(TypedDict):
    level: Level
    items: list[HostAttentionItem]
    now: int


ROUTES = {
    "status": (Status, "GET /status"),
    "attention": (Attention, "GET /attention"),
    "peers": (Peers, "GET /peers"),
    "host-status": (HostStatus, "GET /status at a host daemon's root"),
    "host-attention": (HostAttention, "GET /attention at a host daemon's root"),
}


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(prog=COMMAND, description=(
        f"Write {SCHEMA_DIR}/*.schema.json and {ZOD_FILE} from the poll"
        " routes' TypedDicts."))
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if a file would change")
    args = parser.parse_args(argv)
    files = render(ROUTES, SCHEMA_DIR, ZOD_FILE, COMMAND)
    if not args.check:
        write(root, files)
        return 0
    drift = check(root, files, SCHEMA_DIR)
    if not drift:
        return 0
    for line in drift:
        print(line, file=sys.stderr)
    print(f"regenerate with `{COMMAND}`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
