import tomllib
from pathlib import Path

import review_runner
from holophyte.agents import harness
from holophyte.config.config_tables import (
    AGENT_FALLBACK_KEYS,
    BOARD_KEYS,
    BOARD_MODE_KEYS,
    BOARD_PREFIX_ALIAS,
    CONSOLE_KEYS,
    LOOP_KEYS,
    MERGE_KEYS,
    REPORT_KEYS,
    STORY_KEYS,
    SUPERVISOR_KEYS,
    TRIM_KEYS,
)


def load_config(path):
    path = Path(path)
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"[holo2] malformed config {path}: {exc}") from exc


VERIFY_TIMEOUT = 300  # per-command wall-clock cap, verify and worktree setup

IMPL_MODEL = "opus"
IMPL_EFFORT = "high"
IMPL_TIMEOUT = 1800  # hard wall-clock cap on one implementer turn, seconds
REVIEW_MODEL = review_runner.MODEL
REVIEW_EFFORT = review_runner.EFFORT
REVIEW_EFFORTS = review_runner.EFFORTS
review_profile = review_runner.profile_for
REVIEW_PROFILE = review_profile(REVIEW_MODEL, REVIEW_EFFORT)
CRITIC_MODEL = "gpt-6-luna"
CRITIC_EFFORT = "medium"

AGENT_CONFIG_KEYS = {
    "implement": "implementer",
    "review": "reviewer",
    "adjudicate": "adjudicator",
    "write": "writer",
    "trim": "trimmer",
    "critic": "critic",
}

REVIEW_ROUTE_KEYS = ("review_model", "review_effort")
REVIEW_FALLBACK_KEYS = ("review_fallback_model", "review_fallback_effort")
REVIEW_TIER_KEYS = ("review_service_tier", "review_fallback_service_tier")

DEFAULT_IMPLEMENTER = "claude"
DEFAULT_REVIEWER = "docker"
DOCKER_PROBE_TIMEOUT = 5

# A table not named here is not checked, so a config for a later version loads.
KNOWN_KEYS = {
    "verify": frozenset({"always", "before_merge", "timeout_sec"}),
    "agents": frozenset(AGENT_CONFIG_KEYS.values()) | frozenset(REVIEW_ROUTE_KEYS)
              | frozenset(REVIEW_FALLBACK_KEYS) | frozenset(REVIEW_TIER_KEYS)
              | frozenset(AGENT_FALLBACK_KEYS) | frozenset({"budget_scale",
                  "implementer_isolation", "implementer_image",
                  "implementer_credential", "implementer_session",
                  "implementer_resume", "review_mode"}),
    "worktree": frozenset({"setup", "setup_timeout_sec", "branch_prefix",
                           "carry", "env_source", "env_allow"}),
}
KNOWN_KEYS["supervisor"] = frozenset(SUPERVISOR_KEYS)
KNOWN_KEYS["loop"] = frozenset(LOOP_KEYS)
KNOWN_KEYS["board"] = (frozenset(BOARD_KEYS) | frozenset(BOARD_MODE_KEYS)
                      | {BOARD_PREFIX_ALIAS})
KNOWN_KEYS["merge"] = frozenset(MERGE_KEYS) | frozenset(
    {"capture_env_source", "capture_env_allow"})
KNOWN_KEYS["report"] = frozenset(REPORT_KEYS)
KNOWN_KEYS["story"] = frozenset(STORY_KEYS)
KNOWN_KEYS["trim"] = frozenset(TRIM_KEYS)
KNOWN_KEYS["console"] = frozenset(CONSOLE_KEYS)
KNOWN_KEYS["questions"] = frozenset(("url", "key_env", "min_confidence"))
KNOWN_KEYS["harnesses"] = frozenset(harness.ADAPTERS)

BUDGET_SCALE = 1.0
BUDGET_SCALE_RANGE = (1.0, 3.0)
REVIEW_MODES = ("single", "verified")


def config_table(project, name):
    table = project.config().get(name)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [{name}] must be a table, got "
            f"{type(table).__name__}")
    return table
