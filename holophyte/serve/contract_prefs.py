from typing import Literal, NotRequired, TypedDict


class DayLogPrefs(TypedDict):
    preset: Literal["highlights", "everything", "manual"]
    items: dict[str, bool]
    projects: dict[str, bool]


class Prefs(TypedDict):
    day_log: DayLogPrefs | None
    updated_ms: int | None
    error: NotRequired[str]
