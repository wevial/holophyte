from typing import Literal, TypedDict


class PullRequestFacts(TypedDict):
    number: int | None
    checks: str | None
    review: str | None
    threads: int | None
    open_threads: int | str | None
    title: str | None
    state: Literal["draft", "ready", "closed", "merged"] | None


class WaitsOn(TypedDict):
    ticket: str
    pr_url: str | None
