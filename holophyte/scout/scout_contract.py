"""The scout reads' answer shapes, emitted with the other routes' contract."""
from typing import Literal, TypedDict

from holophyte.serve.contract_routes import RouteProbe

Total = int | float | str | bool | None
ScoutPhase = Literal["queued", "planning", "swarming", "synthesizing",
                     "answering", "done", "failed", "stopped"]
TaskPhase = Literal["queued", "running", "done", "failed", "stopped"]


class ScoutSubject(TypedDict):
    kind: Literal["run", "defect", "ticket", "pr"]
    ref: str


class ScoutRow(TypedDict):
    id: int
    ask: str
    kind: Literal["bug", "research", "recon", "guardrails"]
    phase: ScoutPhase
    reason: str | None
    lead: str | None
    helper: str | None
    swarm: int | None
    tasks_done: int
    tasks_total: int
    created_ms: int
    started_ms: int | None
    ended_ms: int | None
    elapsed_ms: int | None
    heartbeat_age_ms: int | None
    subject: ScoutSubject | None


class Scouts(TypedDict):
    scouts: list[ScoutRow]
    limit: int
    truncated: bool


class ScoutBudget(TypedDict):
    minutes: int
    helper_turns: int | None
    waves: int | None


class ScoutFull(ScoutRow):
    base_sha: str | None
    host: str | None
    budget: ScoutBudget
    totals: dict[str, Total] | None


ScoutTask = TypedDict("ScoutTask", {
    "id": int, "pass": int, "wave": int, "question": str,
    "paths": list[str], "mode": Literal["read", "run"], "route": str | None,
    "phase": TaskPhase, "started_ms": int | None, "ended_ms": int | None,
    "duration_ms": int | None, "tokens": int | None,
    "cost_usd": float | None, "excerpt": str | None})


class FileEvidence(TypedDict):
    file: str
    line: int
    verified: bool
    why: str | None


class CommandEvidence(TypedDict):
    command: str
    output: str


class UrlEvidence(TypedDict):
    url: str


class TextEvidence(TypedDict):
    text: str


FindingStatus = Literal["confirmed", "partial", "unconfirmed"]


class ScoutFinding(TypedDict):
    title: str
    status: FindingStatus
    evidence: list[FileEvidence | CommandEvidence | UrlEvidence | TextEvidence]


ActionKind = Literal["fix", "test", "investigate", "decide", "revise"]


class ScoutReportAction(TypedDict):
    title: str
    why: str
    kind: ActionKind
    body: str


class Hypothesis(TypedDict):
    hypothesis: str
    separates: str
    verdict: Literal["supported", "ruled_out", "open"]


class Reproduction(TypedDict):
    level: Literal["test", "real_tool", "not_reproduced"]
    hypotheses: list[Hypothesis]


class PriorJudgement(TypedDict):
    scout: int
    finding: int
    verdict: Literal["holds", "fixed", "unchecked"]
    why: str


class ScoutReport(TypedDict):
    summary: str
    findings: list[ScoutFinding]
    actions: list[ScoutReportAction]
    reproduction: Reproduction | None
    cut: list[str] | None
    prior: list[PriorJudgement] | None


ScoutReportPass = TypedDict("ScoutReportPass", {
    "pass": int, "at": int, "report": ScoutReport})


class Revises(TypedDict):
    ticket: str
    revision: int


ScoutAction = TypedDict("ScoutAction", {
    "position": int, "pass": int, "title": str, "why": str,
    "kind": ActionKind, "body": str, "revises": Revises | None,
    "ticket": str | None, "filed_ms": int | None})


class ScoutEvent(TypedDict):
    id: int
    at: int
    kind: str
    summary: str


FindingBlock = TypedDict("FindingBlock", {
    "type": Literal["finding"], "pass": int, "index": int})


class ActionBlock(TypedDict):
    type: Literal["action"]
    position: int


class ProposalChoice(TypedDict):
    label: str
    mode: Literal["ask", "steer"]


class ProposalBlock(TypedDict):
    type: Literal["proposal"]
    question: str
    choices: list[ProposalChoice]


class ThreadMessage(TypedDict):
    id: int
    author: str
    mode: Literal["ask", "steer", "answer"]
    text: str
    blocks: list[FindingBlock | ActionBlock | ProposalBlock]
    answers: int | None
    at: int
    consumed_by: int | None
    consumed_ms: int | None


class ScoutDetail(TypedDict):
    scout: ScoutFull
    tasks: list[ScoutTask]
    reports: list[ScoutReportPass]
    actions: list[ScoutAction]
    events: list[ScoutEvent]
    thread: list[ThreadMessage]


class ScoutRoute(TypedDict):
    name: str
    harness: str
    model: str
    default: bool
    probe: RouteProbe | None


class ScoutDefaults(TypedDict):
    lead: str
    helper: str
    swarm: int
    minutes: int
    helper_turns: int
    waves: int


class ScoutRoutes(TypedDict):
    leads: list[ScoutRoute]
    helpers: list[ScoutRoute]
    defaults: ScoutDefaults
