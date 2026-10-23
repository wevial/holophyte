from typing import NotRequired, TypedDict


class ActiveRoute(TypedDict):
    command: str | None
    down: NotRequired[bool]
    fallback: NotRequired[str]


class RouteProbe(TypedDict):
    ok: bool
    at_ms: int
    duration_ms: int
    why: str | None
    age_ms: int


class SeatProbes(TypedDict):
    primary: RouteProbe | None
    fallback: RouteProbe | None


class RouteLabels(TypedDict):
    implementer: str
    implementer_fallback: str | None
    reviewer: str
    reviewer_fallback: str | None
    adjudicator: str
    adjudicator_fallback: str | None
    writer: str
    trimmer: str
    trimmer_fallback: str | None
    implementer_profiles: NotRequired[dict[str, str]]
