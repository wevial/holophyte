import fcntl
import json
import os
import tempfile
from pathlib import Path

from holophyte.agents.agent_routes import route_prose
from holophyte.redact import redact_prose


def probe_reason(project, probe):
    if probe.ok:
        return None
    executable = probe.command[:1]
    private = {*executable, *(repr(part)[1:-1] for part in executable)}
    return route_prose(project, redact_prose(probe.reason(), private))[:200]


def record_probe(project, probe, fallback, started, ended):
    if probe is None:
        return
    entry = {
        "ok": probe.ok,
        "at_ms": int(ended * 1000),
        "duration_ms": max(0, int((ended - started) * 1000)),
        "why": probe_reason(project, probe),
    }
    project.holo_dir.mkdir(parents=True, exist_ok=True)
    with (project.holo_dir / "route-probes.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snapshot = last_probes(project)
        seat = snapshot.setdefault(probe.seat, {})
        route = "fallback" if fallback else "primary"
        previous = seat.get(route)
        if previous is not None and previous["at_ms"] > entry["at_ms"]:
            return
        seat[route] = entry
        with tempfile.NamedTemporaryFile(
                mode="w", dir=project.holo_dir, prefix="route-probes-",
                suffix=".json", delete=False) as stream:
            path = Path(stream.name)
            try:
                json.dump(snapshot, stream)
                stream.flush()
                os.replace(path, project.holo_dir / "route-probes.json")
            finally:
                path.unlink(missing_ok=True)


def valid_entry(entry):
    return (isinstance(entry, dict)
            and set(entry) == {"ok", "at_ms", "duration_ms", "why"}
            and type(entry["ok"]) is bool
            and type(entry["at_ms"]) is int
            and type(entry["duration_ms"]) is int
            and entry["duration_ms"] >= 0
            and (entry["why"] is None or isinstance(entry["why"], str)))


def last_probes(project):
    try:
        snapshot = json.loads((project.holo_dir / "route-probes.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(snapshot, dict):
        return {}
    for routes in snapshot.values():
        if not isinstance(routes, dict):
            return {}
        if any(route not in ("primary", "fallback") or not valid_entry(entry)
               for route, entry in routes.items()):
            return {}
    return snapshot
