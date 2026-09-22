"""One typed choice question, with failures represented as data."""

import http.client
import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from holophyte import redact

DEFAULTS = dict(
    url="https://api.typesafe.ai/v1/systemone",
    key_env="TYPESAFE_API_KEY",
    min_confidence=0.6,
)


@dataclass(frozen=True)
class Question:
    instructions: str
    criteria: dict[str, str]


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
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("[questions] url must be an HTTP(S) URL")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("[questions] key_env must be an environment variable name")
    if not probability(values["min_confidence"]):
        raise ValueError("[questions] min_confidence must be a number in [0, 1]")
    return values


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


def ask(question, state, *, config):
    """Read/register the key at call time; never include remote errors in logs."""
    try:
        options = settings(config)
    except ValueError:
        return Failure("invalid_config")
    key = os.environ.get(options["key_env"], "")
    if not key:
        return Failure("missing_key: " + options["key_env"])
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
            return parse(json.load(response), question)
    except TimeoutError:
        return Failure("timeout")
    except urllib.error.URLError as error:
        return Failure(
            "timeout" if isinstance(error.reason, TimeoutError) else "service_error"
        )
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return Failure("service_error")
