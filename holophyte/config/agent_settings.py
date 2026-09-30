import math
import re
import shlex

from holophyte.agents import harness
from holophyte.config.reader import (
    AGENT_CONFIG_KEYS,
    BUDGET_SCALE,
    BUDGET_SCALE_RANGE,
    REVIEW_EFFORT,
    REVIEW_EFFORTS,
    REVIEW_FALLBACK_KEYS,
    REVIEW_MODEL,
    REVIEW_MODES,
    REVIEW_ROUTE_KEYS,
    config_table,
)


def implementer_session(project):
    value = config_table(project, "agents").get("implementer_session")
    if value is None:
        return None
    key = f"[holo2] {project.config_path}: [agents] implementer_session"
    if not isinstance(value, str):
        raise SystemExit(f"{key} must be a regular expression string")
    try:
        pattern = re.compile(value)
    except re.error as exc:
        raise SystemExit(f"{key} invalid regular expression: {exc}") from exc
    if pattern.groups != 1:
        raise SystemExit(f"{key} must have exactly one capture group")
    return pattern


def agent_command(project, role, goal, *, fallback=False, entry=0):
    key = AGENT_CONFIG_KEYS[role] + ("_fallback" if fallback else "")
    command = config_table(project, "agents").get(key)
    if command is None:
        return None
    if fallback:
        command = fallback_entries(project, role)[entry]
    if isinstance(command, dict):
        return harness.seat(project, role, fallback=fallback).turn(goal)
    if not isinstance(command, str):
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {key} must be "
            f"a command string, got {type(command).__name__}")
    try:
        argv = shlex.split(command)
    except ValueError as bad:
        # `shlex` says "No closing quotation"; the key it was in is ours.
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {key}"
            f" cannot be split into a command: {bad}")
    if not argv:
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {key}"
            " is empty")
    return argv + [goal]


def fallback_entries(project, role):
    key = AGENT_CONFIG_KEYS[role] + "_fallback"
    value = config_table(project, "agents").get(key)
    if key != "reviewer_fallback" or not isinstance(value, list):
        return [] if value is None else [value]
    if not value or not all(isinstance(each, str) and each.strip()
                            for each in value):
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {key} must be a command "
            "string or a non-empty list of non-empty command strings")
    return value


def review_route(project, *, fallback=False):
    agents = config_table(project, "agents")
    for key in (*REVIEW_ROUTE_KEYS, *REVIEW_FALLBACK_KEYS):
        if key in agents and "reviewer" in agents:
            raise SystemExit(
                f"[holo2] {project.config_path}: [agents] {key} beside [agents] "
                f"reviewer: the command opts out of the container "
                f"the pair routes -- drop one of the two")
    present = [key for key in REVIEW_FALLBACK_KEYS if key in agents]
    if len(present) == 1:
        missing, = set(REVIEW_FALLBACK_KEYS) - set(present)
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {present[0]} needs "
            f"[agents] {missing} beside it")
    if fallback and not present:
        return None
    model_key, effort_key = REVIEW_FALLBACK_KEYS if fallback else REVIEW_ROUTE_KEYS
    model = agents.get(model_key, REVIEW_MODEL)
    if not isinstance(model, str) or not model.strip():
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {model_key} must be a "
            f"non-empty Codex model id, got {model!r}")
    effort = agents.get(effort_key, REVIEW_EFFORT)
    if effort not in REVIEW_EFFORTS:
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] {effort_key} must be one of "
            f"{', '.join(REVIEW_EFFORTS)}, got {effort!r}")
    return model, effort


def budget_scale(project):
    value = config_table(project, "agents").get("budget_scale")
    if value is None:
        return BUDGET_SCALE
    low, high = BUDGET_SCALE_RANGE
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] budget_scale must be a "
            f"number from {low} to {high}, got {value!r}")
    return value


def review_mode(project):
    value = config_table(project, "agents").get("review_mode", REVIEW_MODES[0])
    if isinstance(value, str) and value in REVIEW_MODES:
        return value
    raise SystemExit(
        f"[holo2] {project.config_path}: [agents] review_mode must be one of "
        f"{', '.join(REVIEW_MODES)}, got {value!r}")
