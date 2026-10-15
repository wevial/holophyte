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
    working_ms: int | None
    work_started_ms: int | None
    agent_ms: int | None
    verify_ms: int | None
    run_count: int
    verify_started_ms: int | None
    time_box_ms: int | None
    round: int
    max_rounds: int
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
    workers: int
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
    name: str | None
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
    project: str | None
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


class RunDetailRun(TypedDict):
    id: int
    ticket: str
    ticket_url: str | None
    title: str
    phase: str
    attempt: int
    started_ms: int
    ended_ms: int | None
    outcome: str | None
    elapsed_ms: int
    working_ms: int | None
    agent_ms: int | None
    verify_ms: int | None
    time_box_ms: int | None
    branch: str | None
    host: str | None
    heartbeat_age_ms: int | None
    merge_sha: str | None
    commit_url: str | None
    pr_url: str | None
    work_started_ms: int | None
    verify_started_ms: int | None
    approved_at: int | None
    approved_by: str | None
    max_rounds: int


class InstructionTriage(TypedDict):
    decision: Literal["fix", "question", "unclear"]
    confidence: float | None
    route: Literal["fix", "answer"]
    reason: str


InstructionOutcome = Literal["changed", "kept", "asked"]


class FindingFingerprint(TypedDict):
    path: str
    line: NotRequired[int | None]
    severity: str


class RoundFinding(TypedDict):
    path: str
    line: NotRequired[int | None]
    severity: str
    criterion: NotRequired[str | None]
    title: NotRequired[str]
    message: str
    evidence_only: NotRequired[bool]
    evidence: NotRequired[Literal["reproduced", "traced", "concern"]]
    reviewer: NotRequired[str]
    stale_approval: NotRequired[str]
    fingerprint: NotRequired[FindingFingerprint]
    kind: NotRequired[Literal["thread", "finding"]]
    author: NotRequired[str]
    author_kind: NotRequired[str]
    verdict: NotRequired[str]
    summary: NotRequired[str]
    raw: NotRequired[str]
    url: NotRequired[str]
    request: NotRequired[str]
    triage: NotRequired[InstructionTriage]
    outcome: NotRequired[InstructionOutcome]
    reply: NotRequired[str]


class RoundInstruction(TypedDict):
    kind: Literal["instruction"]
    path: str
    line: int | None
    author: str
    request: str
    url: str
    severity: NotRequired[str]
    message: NotRequired[str]
    triage: NotRequired[InstructionTriage]
    outcome: NotRequired[InstructionOutcome]
    reply: NotRequired[str]


class OperatorNote(TypedDict):
    kind: Literal["operator_note"]
    event_id: int
    note: str
    author: str
    hint: NotRequired[bool]
    consumed: bool
    run_id: int
    round: int


class Round(TypedDict):
    round: int
    started_ms: int
    ended_ms: int | None
    verdict: str
    reviewer_model: str | None
    findings: list[RoundFinding]
    instructions: list[RoundInstruction]
    operator_notes: list[OperatorNote]


class AdvisoryFinding(TypedDict):
    tone: Literal["advisory"]
    message: str


class RunEvent(TypedDict):
    at: int
    kind: str
    summary: str


class ChainRound(TypedDict):
    round: int
    verdict: str
    reviewer_model: str | None


class ChainRun(TypedDict):
    id: int
    attempt: int
    outcome: str | None
    phase: str
    started_ms: int
    ended_ms: int | None
    elapsed_ms: int
    working_ms: int | None
    agent_ms: int | None
    verify_ms: int | None
    max_rounds: int
    turn_count: int
    rounds: list[ChainRound]


class RunChain(TypedDict):
    started_ms: int
    elapsed_ms: int
    working_ms: int | None
    agent_ms: int | None
    verify_ms: int | None
    runs: list[ChainRun]


class RunDetail(TypedDict):
    run: RunDetailRun
    rounds: list[Round]
    findings: list[AdvisoryFinding]
    events: list[RunEvent]
    chain: NotRequired[RunChain]


class MergeFact(TypedDict):
    name: str
    ok: bool
    detail: str


class MergeReadiness(TypedDict):
    run: int
    ticket: str | None
    pr_url: str | None
    head_sha: str | None
    ready: bool
    reason: str | None
    detail: str | None
    facts: list[MergeFact]


class ConsoleAsk(TypedDict):
    id: int
    question: str
    author: str
    asked_ms: int
    answered_ms: int | None
    url: str | None
    answer: str | None


class RunAsks(TypedDict):
    run: int
    ticket: str
    pr_url: str | None
    asks: list[ConsoleAsk]


class ShippedRow(TypedDict):
    id: int
    ticket: str
    ticket_url: str | None
    title: str
    rounds: int
    findings: int
    started_ms: int
    actual_min: float | None
    working_ms: int | None
    agent_ms: int | None
    verify_ms: int | None
    run_count: int
    ended_ms: int
    wall_min: float
    turn_count: int
    estimate_min: float | None
    merge_sha: str | None
    commit_url: str | None
    outcome: str
    outcome_reason: str | None
    pr_url: str | None
    host: str | None


class Shipped(TypedDict):
    rows: list[ShippedRow]
    next_before: int | None
    limit: int


class LedgerEntry(TypedDict):
    at: int
    run: int | None
    ticket: str | None
    kind: str
    source: str
    text: str
    cleared: NotRequired[str | None]
    waited_ms: NotRequired[int | None]
    action: NotRequired[str]
    tone: NotRequired[str]
    schema_to: NotRequired[int]
    schema_from: NotRequired[int]
    project: NotRequired[str]


class ActiveOutage(TypedDict):
    project: int
    run: None
    ticket: None
    kind: Literal["route_down"]
    source: str
    at: int
    reason: str
    text: str


class Ledger(TypedDict):
    entries: list[LedgerEntry]
    active_outages: list[ActiveOutage]
    since: int
    limit: int


class BoardTicket(TypedDict):
    ticket: str
    ticket_url: str | None
    title: str
    time_box_ms: int | None
    run: int | None
    question: str | None
    waits_on: list[str]
    mirrored_ms: int
    column: str | None
    priority: int | None
    labels: list[str]
    revision: int


class BoardColumn(TypedDict):
    state: Literal["backlog", "needs_spec", "blocked_on_deps", "ready",
                   "blocked_on_operator", "in_flight"]
    tickets: list[BoardTicket]


class Board(TypedDict):
    columns: list[BoardColumn]
    editable: bool
    now: int


class RevisionStamp(TypedDict):
    revision: int
    at: int
    author: str


class TicketRevision(RevisionStamp):
    title: str
    body: str
    priority: int | None
    labels: list[str]
    column: str | None


class ClaimedSnapshot(TypedDict):
    revision: None
    title: str
    acceptance_criteria: list[str]
    verification_commands: list[str]


class TicketNote(TypedDict):
    id: int
    at: int
    author: str
    kind: str
    text: str
    posted_ms: int | None
    post_error: str | None


class TicketDetail(TypedDict):
    ticket: str
    ticket_url: str | None
    title: str
    status: str
    body: str
    acceptance_criteria: list[str]
    verification_commands: list[str]
    time_box_ms: int | None
    run: int | None
    mirrored_ms: int
    current: TicketRevision | None
    claimed: TicketRevision | ClaimedSnapshot | None
    revisions: list[RevisionStamp]
    notes: list[TicketNote]


ROUTES = {
    "status": (Status, "GET /status"),
    "attention": (Attention, "GET /attention"),
    "peers": (Peers, "GET /peers"),
    "host-status": (HostStatus, "GET /status at a host daemon's root"),
    "host-attention": (HostAttention, "GET /attention at a host daemon's root"),
    "run-detail": (RunDetail, "GET /runs/N"),
    "run-merge": (MergeReadiness, "GET /runs/N/merge"),
    "run-asks": (RunAsks, "GET /runs/N/asks"),
    "shipped": (Shipped, "GET /shipped"),
    "ledger": (Ledger, "GET /ledger"),
    "board": (Board, "GET /board"),
    "ticket": (TicketDetail, "GET /tickets/KEY"),
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
        write(root, files, SCHEMA_DIR)
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
