"""What stops an automation ahead of its mode: the host's kill switch, then a pause."""
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


def switch_reason(switch):
    if switch.problem is not None:
        return f"automations off: {switch.problem}"
    if switch.off:
        return f"automations off: {switch.record.get('note')}"
    return None


def inhibitor(conn, home, automation):
    reason = switch_reason(read_switch(home))
    if reason is not None:
        return reason
    pause = store.automations.automation_pause(conn, automation)
    if pause is not None:
        return f"automation {automation} paused: {pause.note}"
    return None
