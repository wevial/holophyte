from typing import Literal, NotRequired, TypedDict


class Screenshot(TypedDict):
    caption: str
    url: str


class PullRequestFacts(TypedDict):
    number: int | None
    checks: str | None
    review: str | None
    threads: int | None
    open_threads: int | str | None
    title: str | None
    state: Literal["draft", "ready", "closed", "merged"] | None
    additions: NotRequired[int | None]
    deletions: NotRequired[int | None]
    changed_files: NotRequired[int | None]
    preview_url: NotRequired[str | None]
    screenshots: NotRequired[list[Screenshot]]


class WaitsOn(TypedDict):
    ticket: str
    pr_url: str | None
