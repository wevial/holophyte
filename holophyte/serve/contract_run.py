from typing import Literal, NotRequired, TypedDict


class RoundCriterion(TypedDict):
    id: str
    n: int
    status: Literal["met", "not_met", "unwitnessed"]
    note: str


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
    criteria: list[RoundCriterion]


class PrecheckCriterion(TypedDict):
    id: str
    n: int
    witnessed: bool
    note: str


class Precheck(TypedDict):
    sha: str
    criteria: list[PrecheckCriterion]
