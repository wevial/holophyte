from dataclasses import dataclass

from holophyte.agents.harness import (
    PROFILE_NAME,
    SCOUT_HELPER,
    SCOUT_LEAD,
    TABLE_KEYS,
    Seat,
    harness_binary,
    route_adapter,
)
from holophyte.config.reader import config_table

DEFAULT_LEADS = {
    "opus": {"harness": "claude", "model": "opus", "effort": "high"},
    "sol": {"harness": "codex", "model": "gpt-6.1-sol", "effort": "high"},
}
DEFAULT_HELPERS = {
    "luna": {"harness": "codex", "model": "gpt-6-luna", "effort": "low"},
    "haiku": {"harness": "claude", "model": "haiku", "effort": "low"},
}
DEFAULT_LEAD = "opus"
DEFAULT_HELPER = "luna"
COUNTS = {"budget_min": 30, "swarm": 4, "helper_turns": 12}
WAVES = (1, 2)
DEFAULT_WAVES = 2
STEER_BUDGET_MIN = 10
STEER_HELPERS = 4


@dataclass(frozen=True)
class ScoutConfig:
    leads: dict
    helpers: dict
    lead_prices: dict
    helper_prices: dict
    lead: str
    helper: str
    budget_min: int
    swarm: int
    helper_turns: int
    waves: int
    steer_budget_min: int
    steer_helpers: int
    github: bool

    @property
    def prices(self):
        return {**self.lead_prices, **self.helper_prices}


def scout_config(project):
    table = config_table(project, "scout")
    where = f"[holo2] {project.config_path}"
    leads, lead_prices = route_seats(project, where, table, "leads",
                                     DEFAULT_LEADS, SCOUT_LEAD)
    helpers, helper_prices = route_seats(project, where, table, "helpers",
                                         DEFAULT_HELPERS, SCOUT_HELPER)
    lead = default_route(where, table, "lead", DEFAULT_LEAD, "leads", leads)
    helper = default_route(where, table, "helper", DEFAULT_HELPER, "helpers",
                           helpers)
    counts = {key: count(where, table, key, default)
              for key, default in COUNTS.items()}
    if counts["swarm"] > counts["helper_turns"]:
        raise SystemExit(
            f"{where}: [scout] swarm: {counts['swarm']} helpers at once is "
            f"more than helper_turns = {counts['helper_turns']}")
    waves = table.get("waves", DEFAULT_WAVES)
    if type(waves) is not int or waves not in WAVES:
        raise SystemExit(
            f"{where}: [scout] waves must be 1 or 2, got {waves!r}")
    steer_budget_min = count(where, table, "steer_budget_min",
                             STEER_BUDGET_MIN)
    steer_helpers = count(where, table, "steer_helpers", STEER_HELPERS)
    if steer_helpers > counts["helper_turns"]:
        raise SystemExit(
            f"{where}: [scout] steer_helpers: {steer_helpers} helpers for a "
            f"steered pass is more than helper_turns = "
            f"{counts['helper_turns']}")
    github = table.get("github", False)
    if not isinstance(github, bool):
        raise SystemExit(
            f"{where}: [scout] github must be true or false, got {github!r}")
    return ScoutConfig(leads, helpers, lead_prices, helper_prices, lead,
                       helper, waves=waves, steer_budget_min=steer_budget_min,
                       steer_helpers=steer_helpers, github=github, **counts)


def route_seats(project, where, table, key, defaults, role):
    routes = table.get(key, defaults)
    if not isinstance(routes, dict):
        raise SystemExit(
            f"{where}: [scout] {key}: write each route as a "
            f"[scout.{key}.NAME] table")
    seats, prices = {}, {}
    for name, route in routes.items():
        label = f"[scout.{key}.{name}]"
        if not PROFILE_NAME.fullmatch(name):
            raise SystemExit(
                f"{where}: {label}: a route name takes only letters, digits, "
                f"_ and -")
        if not isinstance(route, dict):
            raise SystemExit(
                f"{where}: {label}: write it as a table with harness, model "
                f"and effort")
        prices[name] = route_price(where, label, route)
        route = {option: value for option, value in route.items()
                 if option != "usd_per_mtok"}
        adapter = route_adapter(where, label, route, role, TABLE_KEYS)
        for option in ("model", "effort"):
            if option not in route:
                raise SystemExit(
                    f"{where}: {label} {option} must be a non-empty string, "
                    f"got nothing")
        seats[name] = Seat(adapter, harness_binary(project, where, adapter),
                           route, role)
    return seats, prices


def route_price(where, label, route):
    price = route.get("usd_per_mtok")
    if price is None:
        return None
    if isinstance(price, bool) or not isinstance(price, (int, float)) \
            or not price >= 0:
        raise SystemExit(
            f"{where}: {label} usd_per_mtok must be a number of at least 0, "
            f"got {price!r}")
    return price


def default_route(where, table, key, default, routes_key, seats):
    name = table.get(key, default)
    if not isinstance(name, str) or name not in seats:
        raise SystemExit(
            f"{where}: [scout] {key}: {name!r} names no route in "
            f"[scout.{routes_key}]; the routes are "
            f"{', '.join(sorted(seats)) or 'none'}")
    return name


def count(where, table, key, default):
    value = table.get(key, default)
    if type(value) is not int or value < 1:
        raise SystemExit(
            f"{where}: [scout] {key} must be an integer of at least 1, got "
            f"{value!r}")
    return value
