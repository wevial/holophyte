import http.client
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import store
from holophyte import redact
from holophyte.config.reader import CRITIC_MODEL, REVIEW_EFFORTS

DEFAULTS = dict(
    url="https://api.typesafe.ai/v1/systemone",
    key_env="TYPESAFE_API_KEY",
    min_confidence=0.6,
)
MAX_RESPONSE_BYTES = 64 * 1024
BACKENDS = ("jev", "claude", "codex")
CLI_DEFAULTS = {"claude": ("haiku", "high"), "codex": (CRITIC_MODEL, "low")}
CLI_EFFORTS = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": REVIEW_EFFORTS,
}
FALLBACK_KEYS = ("backend_fallback", "fallback_model", "fallback_effort")


@dataclass(frozen=True)
class Question:
    instructions: str
    criteria: dict[str, str]
    name: str


@dataclass(frozen=True)
class Route:
    backend: str
    model: str | None
    effort: str | None


@dataclass(frozen=True)
class Answer:
    choice: str
    confidence: float


@dataclass(frozen=True)
class Failure:
    reason: str


def settings(config):
    table = config.get("questions", {})
    if not isinstance(table, dict):
        raise ValueError("[questions] must be a table")
    values = DEFAULTS | table
    url, name = values["url"], values["key_env"]
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("[questions] url must be an HTTPS URL (including localhost)")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("[questions] key_env must be an environment variable name")
    if not probability(values["min_confidence"]):
        raise ValueError("[questions] min_confidence must be a number in [0, 1]")
    values |= route_settings(table, "backend", "model", "effort")
    if "backend_fallback" in table and values["backend"] == "jev":
        raise ValueError("[questions] backend_fallback needs a claude or codex backend")
    if "backend_fallback" in table:
        values |= route_settings(table, *FALLBACK_KEYS)
    else:
        for key in FALLBACK_KEYS[1:]:
            if key in table:
                raise ValueError(f"[questions] {key} needs backend_fallback")
        values |= dict.fromkeys(FALLBACK_KEYS)
    return values


def route_settings(table, backend_key, model_key, effort_key):
    backend = table.get(backend_key, "jev")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError(
            f"[questions] {backend_key} must be one of {', '.join(BACKENDS)}")
    if backend == "jev":
        for key in (model_key, effort_key):
            if key in table:
                raise ValueError(
                    f"[questions] {key} applies only to the claude and codex backends")
        return {backend_key: backend, model_key: None, effort_key: None}
    model, effort = CLI_DEFAULTS[backend]
    model, effort = table.get(model_key, model), table.get(effort_key, effort)
    if not isinstance(model, str) or not model.strip():
        raise ValueError(f"[questions] {model_key} must be a non-empty model name")
    if not isinstance(effort, str) or effort not in CLI_EFFORTS[backend]:
        raise ValueError(f"[questions] {effort_key} for {backend} must be one of "
                         f"{', '.join(CLI_EFFORTS[backend])}")
    return {backend_key: backend, model_key: model, effort_key: effort}


def probability(value):
    return type(value) in (float, int) and 0 <= value <= 1 and math.isfinite(value)


def safe(value, secrets):
    if isinstance(value, str):
        return redact.outbound(value, secrets)
    if isinstance(value, dict):
        return {redact.outbound(k, secrets): safe(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe(v, secrets) for v in value]
    return value


def parse(document, question):
    try:
        answer = document["answers"]["q"]
        choice, confidence = answer["choice"], answer["confidence"]
        probabilities = answer["probabilities"]
        valid = (
            choice in question.criteria
            and probability(confidence)
            and isinstance(probabilities, dict)
            and set(probabilities) == set(question.criteria)
            and all(probability(v) for v in probabilities.values())
        )
        if valid:
            return Answer(choice, float(confidence))
    except (KeyError, TypeError):
        pass
    return Failure("invalid_response")


def ask(question, state, *, config, conn=None, run_id=None):
    """Read/register the key at call time; never include remote errors in logs."""
    try:
        options = settings(config)
    except ValueError:
        return Failure("invalid_config")
    from holophyte import question_cli

    route = question_cli.active_route(options, config, conn, run_id)
    started = time.monotonic()
    if route is None:
        route = Route(options["backend"], options["model"], options["effort"])
        result, usage = Failure("route_down"), {}
    else:
        result, usage, _ = answer_on(route, question, state, config, options)
    record(conn, run_id, question, route, result, usage, started)
    return result


def answer_on(route, question, state, config, options):
    if route.backend == "jev":
        return (*ask_jev(question, state, config, options), "")
    from holophyte import question_cli

    return question_cli.answer(route, question, state, config)


def record(conn, run_id, question, route, result, usage, started):
    if conn is None or run_id is None:
        return
    outcome = result.choice if isinstance(result, Answer) else result.reason
    payload = dict(
        question=question.name,
        backend=route.backend,
        model=route.model,
        effort=route.effort,
        outcome=outcome,
        latency_ms=round((time.monotonic() - started) * 1000),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cost_usd=usage.get("cost_usd"),
    )
    store.record_event(conn, run_id, "question",
                       f"question {question.name} via {route.backend}: {outcome}",
                       level="detail", payload=json.dumps(payload))


def count(value):
    return value if type(value) is int and value >= 0 else None


def jev_usage(document):
    usage = document.get("usage") if isinstance(document, dict) else None
    if not isinstance(usage, dict):
        return {}
    return dict(input_tokens=count(usage.get("input_tokens")),
                output_tokens=count(usage.get("output_tokens")))


def ask_jev(question, state, config, options):
    key = os.environ.get(options["key_env"], "")
    if not key:
        return Failure("missing_key: " + options["key_env"]), {}
    redact.register_values([key])
    secrets = redact.known_secrets(config)
    body = safe(
        dict(
            state=state,
            model="jev-latest",
            questions={
                "q": {
                    "type": "choice",
                    "instructions": question.instructions,
                    "criteria": question.criteria,
                }
            },
        ),
        secrets,
    )
    try:
        request = urllib.request.Request(
            options["url"],
            json.dumps(body).encode(),
            {"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                return Failure("response_too_large"), {}
            document = json.loads(payload)
            return parse(document, question), jev_usage(document)
    except TimeoutError:
        return Failure("timeout"), {}
    except urllib.error.URLError as error:
        return Failure(
            "timeout" if isinstance(error.reason, TimeoutError) else "service_error"
        ), {}
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return Failure("service_error"), {}
