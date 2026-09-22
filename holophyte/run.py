"""The immutable identity and candidate state carried by a claimed run."""
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Connection
from typing import Any

from holophyte.target import Target


@dataclass(frozen=True)
class Run:
    target: Target
    conn: Connection | None
    run_id: int | None
    provider: Any
    task_id: str
    issue_id: str
    task: str
    branch: str
    wt: Path
    budget_min: float
    started: float
    started_at: int | None = None
    sha: str | None = None
    rnd: int = 0
    pr_url: str | None = None
    merge_sha: str | None = None
