"""The pipeline and harnesses `GET /pipeline` and `GET /harnesses` answer."""
from typing import Literal, TypedDict

from holophyte.serve.contract_run import PipelineMerge, PipelineStage


class CurrentPipeline(TypedDict):
    name: str
    format: int
    revision: int | None
    merge: PipelineMerge
    stages: list[PipelineStage]


class PipelineWindow(TypedDict):
    days: int
    since_ms: int


StageOutcomes = TypedDict("StageOutcomes", {"pass": int, "fail": int,
                                            "skipped": int, "error": int})


class StageNumbers(TypedDict):
    id: str
    in_pipeline: bool
    attempts: int
    median_seconds: float | None
    median_tokens: float | None
    median_cost_usd: float | None
    fail_rate: float | None
    findings_fixed: int
    outcomes: StageOutcomes


class PipelineNumbers(TypedDict):
    pipeline: CurrentPipeline
    window: PipelineWindow
    stages: list[StageNumbers]


class HarnessProbe(TypedDict):
    ok: bool
    at_ms: int
    why: str | None
    seat: str


class HarnessModel(TypedDict):
    id: str
    source: Literal["curated", "configured"]


class Harness(TypedDict):
    name: str
    binary: str
    path: str | None
    found: bool
    configured: bool
    seats: list[str]
    probe: HarnessProbe | None
    models: list[HarnessModel]


class Harnesses(TypedDict):
    harnesses: list[Harness]
