import os
import tomllib
from pathlib import Path

import review_runner
import ticket_template
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
from holophyte.config.review_settings import REVIEW_KEYS
from holophyte.redact import register_values


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
REVIEW_MODEL = review_runner.MODEL
REVIEW_EFFORT = review_runner.EFFORT
REVIEW_EFFORTS = review_runner.EFFORTS
review_profile = review_runner.profile_for
REVIEW_PROFILE = review_profile(REVIEW_MODEL, REVIEW_EFFORT)
ADVERSARY_CLAUDE = ("opus", "high")
CRITIC_MODEL = "gpt-6-luna"
CRITIC_EFFORT = "medium"

AGENT_CONFIG_KEYS = {
    "implement": "implementer",
    "review": "reviewer",
    "adjudicate": "adjudicator",
    "write": "writer",
    "trim": "trimmer",
    "critic": "critic",
    "adversary": "adversary",
    "consolidate": "consolidator",
}
SHA_ROLES = ("review", "adjudicate", "adversary", "consolidate")

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
                  "turn_cap_min",
                  "implementer_isolation", "implementer_image",
                  "implementer_credential", "implementer_session",
                  "implementer_resume", "review_mode", "adversary_credential",
                  harness.SHADOW_KEY}),
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
KNOWN_KEYS["review"] = frozenset(REVIEW_KEYS)
KNOWN_KEYS["console"] = frozenset(CONSOLE_KEYS)
KNOWN_KEYS["questions"] = frozenset((
    "url", "key_env", "min_confidence", "backend", "model", "effort",
    "backend_fallback", "fallback_model", "fallback_effort", "failures"))
KNOWN_KEYS["harnesses"] = frozenset(harness.ADAPTERS)

BUDGET_SCALE = 1.0
BUDGET_SCALE_RANGE = (1.0, 3.0)
TURN_CAP_MIN = ticket_template.MAX_ESTIMATE_MIN
REVIEW_MODES = ("single", "verified")


def adversary_credential(project):
    value = config_table(project, "agents").get("adversary_credential")
    if value is None:
        return None
    if not (isinstance(value, dict) and set(value) == {"env"}
            and isinstance(value["env"], str)
            and review_runner.CREDENTIAL_NAME.fullmatch(value["env"])):
        raise SystemExit(
            f"[holo2] {project.config_path}: [agents] adversary_credential "
            'must be { env = "NAME" }, NAME a variable in the factory\'s '
            "environment")
    register_values([os.environ.get(value["env"], "")])
    return value["env"]


def config_table(project, name):
    table = project.config().get(name)
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise SystemExit(
            f"[holo2] {project.config_path}: [{name}] must be a table, got "
            f"{type(table).__name__}")
    return table
