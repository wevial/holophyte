"""An automation's act limits, the host's daily GitHub reruns, and its self-pause."""
import datetime
import zoneinfo

import store.automations
from holophyte.automation.main_watch import RED_AGAIN
from holophyte.host.registry import HostError

QUEUED, PER_HOUR, PER_DAY = 3, 6, 30
RERUNS_PER_DAY = 10
HOUR_MS = 60 * 60 * 1000
DAY_MS = 24 * HOUR_MS
PACIFIC = zoneinfo.ZoneInfo("America/Los_Angeles")


def midnight(now):
    """The Pacific midnight that starts `now`'s day, in ms, and that day."""
    local = datetime.datetime.fromtimestamp(now / 1000, PACIFIC)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), start.date().isoformat()


def reruns_per_day(host):
    automations = host.table().get("automations", {})
    value = automations.get("reruns_per_day", RERUNS_PER_DAY)
    if type(value) is not int or value < 1:
        raise HostError(f"[holo2] {host.path}: [automations] reruns_per_day"
                        f" must be a whole number of at least 1, got {value!r}")
    return value


def limit_reason(counts, reruns, cap):
    if counts.queued >= QUEUED:
        return f"rate: {QUEUED} firings queued"
    if counts.hour >= PER_HOUR:
        return f"rate: {PER_HOUR} actions in the last hour"
    if counts.today >= PER_DAY:
        return f"rate: {PER_DAY} actions today"
    if reruns >= cap:
        return f"GitHub rerun cap: {cap} today"
    return None


class HostReruns:
    """The sweep state's `reruns`, counted per Pacific day against `cap`."""

    def __init__(self, state, cap):
        self.state, self.cap = state, cap

    def today(self, now):
        saved = self.state.get("reruns") or {}
        return saved.get("count", 0) if saved.get("day") == midnight(now)[1] else 0

    def count(self, now):
        self.state["reruns"] = {"day": midnight(now)[1],
                                "count": self.today(now) + 1}

    def limit(self, project, automation, now):
        day_since = midnight(now)[0]

        def limited(conn):
            counts = store.automations.act_counts(
                conn, project, automation, hour_since=now - HOUR_MS,
                day_since=day_since)
            return limit_reason(counts, self.today(now), self.cap)
        return limited


def leased_actions(conn, project, automation):
    return [firing for firing in store.automations.firings(conn)
            if firing.projectId == project and firing.automation == automation
            and firing.mode == "act" and "limit" not in firing.detail]


def same_failure_twice(fired, trigger, now):
    if trigger.detail.get("result") != RED_AGAIN:
        return None
    checks = trigger.detail.get("result_checks")
    for firing in fired:
        if (firing.id != trigger.id
                and firing.detail.get("result") == RED_AGAIN
                and firing.detail.get("result_checks") == checks
                and firing.detail.get("result_at", 0) > now - DAY_MS):
            return (f"same failure twice: firings {firing.id} and"
                    f" {trigger.id} were red again on {', '.join(checks)}"
                    " within 24 hours")
    return None


def refused_twice(fired, trigger):
    newest = fired[:2]
    if (len(newest) < 2 or trigger.id not in {firing.id for firing in newest}
            or not all(firing.state == store.automations.REFUSED
                       and firing.detail.get("github_refused")
                       for firing in newest)):
        return None
    return (f"refused twice: GitHub refused firings {newest[1].id} and"
            f" {newest[0].id} in a row")


def self_pause(conn, firing_id, *, now):
    """Pauses the automation as a person would when either rule holds."""
    trigger = store.automations.firing(conn, firing_id)
    fired = leased_actions(conn, trigger.projectId, trigger.automation)
    note = (same_failure_twice(fired, trigger, now)
            or refused_twice(fired, trigger))
    if note is not None:
        store.automations.pause_automation(
            conn, trigger.automation, note,
            actor=store.automations.AUTOMATION, now=now)
    return note
