"""`GET /harnesses`: each harness, its binary, seats, last probe and models."""
from __future__ import annotations

import dataclasses
import os
import shlex
import shutil
import tomllib

from holophyte.agents.harness import ADAPTERS, PROFILES_KEY, SHADOW_KEY
from holophyte.agents.probe_log import last_probes
from holophyte.config.reader import AGENT_CONFIG_KEYS
from holophyte.redact import known_secrets
from holophyte.stages.standard import scrubbed

HARNESSES_PATH = "/harnesses"
FALLBACK = "_fallback"
MODEL_OPTIONS = {"--model": "model", "-m": "model", "--config": "config",
                 "-c": "config"}


def harnesses(project, query):
    fresh = dataclasses.replace(project, _config=None)
    try:
        config = fresh.config()
    except (SystemExit, OSError, ValueError):
        config = None
    agents = table(config, "agents")
    paths = table(config, "harnesses")
    if agents is None or paths is None:
        return 503, {"error": "the project config cannot be read, so its"
                              " harnesses cannot be listed"}
    seats = configured_seats(agents, paths)
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
    own = [seat for seat in named
           if {name for each, name, _ in seats if each == seat}
           == {adapter.name}]
    return {"name": adapter.name, "binary": binary, "path": path,
            "found": found, "configured": configured, "seats": named,
            "probe": newest_probe(own, probes),
            "models": models(adapter, [model for _, model in used])}


def configured_seats(agents, paths):
    keys = [key + suffix for key in AGENT_CONFIG_KEYS.values()
            for suffix in ("", FALLBACK)] + [SHADOW_KEY]
    entries = [(key, agents[key]) for key in keys if key in agents]
    profiles = agents.get(PROFILES_KEY)
    if isinstance(profiles, dict):
        entries += [(f"{PROFILES_KEY}.{name}", value)
                    for name, value in profiles.items()]
    return [(seat, name, model) for seat, value in entries
            for name, model in seat_routes(seat, value, paths)]


def seat_routes(seat, value, paths):
    if isinstance(value, dict):
        name = value.get("harness", "codex" if seat == "critic" else None)
        model = value.get("model")
        return [(name, model if isinstance(model, str) and model else None)]
    commands = value if isinstance(value, list) else [value]
    return [command_route(command, paths) for command in commands]


def command_route(command, paths):
    try:
        words = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return None, None
    if not words:
        return None, None
    return command_adapter(words[0], paths), command_model(words[1:])


def command_adapter(executable, paths):
    word = os.path.basename(executable)
    for adapter in ADAPTERS.values():
        if paths.get(adapter.harness_key) == executable:
            return adapter.name
    if word in ADAPTERS:
        return word
    return next((adapter.name for adapter in ADAPTERS.values()
                 if adapter.binary == word), None)


def command_model(words):
    found = {}
    at = 0
    while at < len(words):
        kind, value, at = option_value(words, at)
        if kind == "config" and value is not None:
            key, held, value = value.partition("=")
            value = config_value(value.strip()) if held and (
                key.strip() == "model") else None
        if kind is not None and value:
            found[kind] = value
    return found.get("model") or found.get("config")


def option_value(words, at):
    word = words[at]
    for option, kind in MODEL_OPTIONS.items():
        if word == option:
            following = words[at + 1] if at + 1 < len(words) else None
            return kind, following, at + 2
        attached = option + "=" if option.startswith("--") else option
        if word.startswith(attached):
            return kind, word[len(attached):], at + 1
    return None, None, at + 1


def config_value(value):
    try:
        parsed = tomllib.loads(f"value = {value}")["value"]
    except tomllib.TOMLDecodeError:
        return value or None
    return parsed if isinstance(parsed, str) and parsed else None


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
    extra = [model for model in dict.fromkeys(configured)
             if model is not None and model not in adapter.models]
    return ([{"id": model, "source": "curated"} for model in adapter.models]
            + [{"id": model, "source": "configured"} for model in extra])
