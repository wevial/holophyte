"""`GET /harnesses`: each harness, its binary, seats, last probe and models."""
from __future__ import annotations

import dataclasses
import os
import shlex
import shutil

from holophyte.agents.harness import ADAPTERS, PROFILES_KEY, SHADOW_KEY
from holophyte.agents.probe_log import last_probes
from holophyte.config.reader import AGENT_CONFIG_KEYS
from holophyte.redact import known_secrets
from holophyte.stages.standard import scrubbed

HARNESSES_PATH = "/harnesses"
FALLBACK = "_fallback"


def harnesses(project, query):
    fresh = dataclasses.replace(project, _config=None)
    try:
        config = fresh.config()
    except SystemExit:
        config = None
    agents = table(config, "agents")
    paths = table(config, "harnesses")
    if agents is None or paths is None:
        return 503, {"error": "the project config cannot be read, so its"
                              " harnesses cannot be listed"}
    seats = configured_seats(agents)
    probes = last_probes(fresh)
    body = {"harnesses": [harness(adapter, paths, seats, probes)
                          for adapter in ADAPTERS.values()]}
    return 200, scrubbed(body, known_secrets(config))


def table(config, name):
    if not isinstance(config, dict):
        return None
    value = config.get(name, {})
    return value if isinstance(value, dict) else None


def harness(adapter, paths, seats, probes):
    configured_path = paths.get(adapter.harness_key)
    configured = (isinstance(configured_path, str)
                  and os.path.isabs(configured_path))
    if configured:
        binary, path = configured_path, configured_path
        found = os.path.isfile(path) and os.access(path, os.X_OK)
    else:
        binary, path = adapter.binary, shutil.which(adapter.binary)
        found = path is not None
    used = [(seat, model) for seat, name, model in seats
            if name == adapter.name]
    named = list(dict.fromkeys(seat for seat, _ in used))
    return {"name": adapter.name, "binary": binary, "path": path,
            "found": found, "configured": configured, "seats": named,
            "probe": newest_probe(named, probes),
            "models": models(adapter, [model for _, model in used])}


def configured_seats(agents):
    keys = [key + suffix for key in AGENT_CONFIG_KEYS.values()
            for suffix in ("", FALLBACK)] + [SHADOW_KEY]
    entries = [(key, agents.get(key)) for key in keys]
    profiles = agents.get(PROFILES_KEY)
    if isinstance(profiles, dict):
        entries += [(f"{PROFILES_KEY}.{name}", value)
                    for name, value in profiles.items()]
    seats = []
    for seat, value in entries:
        name, model = seat_route(seat, value)
        if name is not None:
            seats.append((seat, name, model))
    return seats


def seat_route(seat, value):
    if isinstance(value, dict):
        name = value.get("harness", "codex" if seat == "critic" else None)
        model = value.get("model")
        return name, model if isinstance(model, str) and model else None
    return command_adapter(value), None


def command_adapter(command):
    try:
        words = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return None
    if not words:
        return None
    word = os.path.basename(words[0])
    if word in ADAPTERS:
        return word
    return next((adapter.name for adapter in ADAPTERS.values()
                 if adapter.binary == word), None)


def newest_probe(seats, probes):
    found = []
    for seat in seats:
        fallback = "." not in seat and seat.endswith(FALLBACK)
        key = seat[:-len(FALLBACK)] if fallback else seat
        entry = probes.get(key, {}).get("fallback" if fallback else "primary")
        if entry is not None:
            found.append({"ok": entry["ok"], "at_ms": entry["at_ms"],
                          "why": entry["why"], "seat": seat})
    return max(found, key=lambda probe: probe["at_ms"], default=None)


def models(adapter, configured):
    curated = list(adapter.models)
    extra = [model for model in dict.fromkeys(configured)
             if model is not None and model not in curated]
    return ([{"id": model, "source": "curated"} for model in curated]
            + [{"id": model, "source": "configured"} for model in extra])
