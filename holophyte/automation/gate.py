"""The host's kill switch, read ahead of every automation."""
import json
from typing import NamedTuple

import store.automations

SWITCH_FILE = "automations.json"


class Switch(NamedTuple):
    off: bool
    record: dict | None
    problem: str | None


def read_switch(home):
    path = home / SWITCH_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Switch(False, None, None)
    except (OSError, UnicodeDecodeError, ValueError) as bad:
        return Switch(True, None, f"{path} is unreadable ({bad})")
    if not isinstance(record, dict) or not isinstance(record.get("off"), bool):
        return Switch(True, None, f"{path} holds no boolean `off`")
    return Switch(record["off"], record, None)


def inhibitor(home, conn, automation):
    switch = read_switch(home)
    if switch.off:
        return (f"automations are off on this host:"
                f" {switch.problem or switch.record.get('note')}")
    pause = store.automations.automation_pause(conn, automation)
    if pause is not None:
        return f"automation {automation} is paused: {pause.note}"
    return None
