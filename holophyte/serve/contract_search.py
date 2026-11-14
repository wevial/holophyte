from typing import TypedDict

from holophyte.scout.scout_contract import ScoutPhase


class SearchTicket(TypedDict):
    ticket: str
    title: str
    status: str


class SearchRun(TypedDict):
    id: int
    ticket: str
    title: str
    phase: str
    outcome: str | None
    started_ms: int


class SearchScout(TypedDict):
    id: int
    title: str
    phase: ScoutPhase


class SearchFinding(TypedDict):
    run: int
    round: int
    severity: str
    message: str
    path: str
    line: int | None


class Search(TypedDict):
    q: str
    limit: int
    tickets: list[SearchTicket]
    runs: list[SearchRun]
    scouts: list[SearchScout]
    findings: list[SearchFinding]
