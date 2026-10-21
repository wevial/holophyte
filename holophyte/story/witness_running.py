"""The witness set a loop's witness step is running, in a file beside the store."""
import json
import os
from pathlib import Path
from time import time

from holophyte.host.supervisor_lock import pid_alive


def running_path(target):
    return Path(target.holo_dir) / "witness-running.json"


def restarts_path(target):
    return Path(target.holo_dir) / "witness-restarts.json"


def _write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    scratch.write_text(json.dumps(record))
    os.replace(scratch, path)


def mark_running(target, story, keys, ticket=None, sha=None, step=False):
    _write(running_path(target),
           {"pid": os.getpid(), "story": story, "keys": keys,
            "started_ms": int(time() * 1000), "ticket": ticket, "sha": sha,
            "step": step})


def clear_running(target):
    running_path(target).unlink(missing_ok=True)


def running_set(target, now):
    try:
        record = json.loads(running_path(target).read_text())
        pid, started_ms = int(record["pid"]), int(record["started_ms"])
        story, keys = str(record["story"]), [str(key) for key in record["keys"]]
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not pid_alive(pid):
        return None
    return {"story": story, "keys": keys, "started_ms": started_ms,
            "elapsed_ms": now - started_ms}


def running_step(target):
    try:
        record = json.loads(running_path(target).read_text())
        step = (int(record["pid"]), str(record["story"]), int(record["ticket"]),
                str(record["sha"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if record.get("step") is not True or not pid_alive(step[0]):
        return None
    return step


def _restarts(target):
    try:
        counts = json.loads(restarts_path(target).read_text())
    except (OSError, ValueError):
        return {}
    return counts if isinstance(counts, dict) else {}


def restarts(target, ticket):
    count = _restarts(target).get(str(ticket), 0)
    return count if isinstance(count, int) else 0


def add_restart(target, ticket):
    counts = _restarts(target)
    counts[str(ticket)] = restarts(target, ticket) + 1
    _write(restarts_path(target), counts)


def clear_restarts(target, ticket):
    counts = _restarts(target)
    if counts.pop(str(ticket), None) is not None:
        _write(restarts_path(target), counts)
