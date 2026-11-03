"""The host sweep's automation step: each opted-in project's rules, round robin."""
import time

from holophyte import deadline
from holophyte.admission import project_of
from holophyte.automation import main_watch
from holophyte.automation.automation_config import DRY_RUN, main_watch_mode
from holophyte.loop.runs import open_store

AUTOMATION_SHARE = 0.25


def watched(entries, out, key):
    """The entries in `dry_run`; a wrong mode is off for this pass, said once."""
    found = []
    for entry in entries:
        try:
            mode = main_watch_mode(entry.target)
        except ValueError as bad:
            print(f"[holo2] {key(entry)}: {bad}; the main watch is off for"
                  " it this pass", file=out)
            continue
        if mode == DRY_RUN:
            found.append(entry)
    return found


def observe_store(target, now):
    conn = open_store(target)
    try:
        project = project_of(conn, target)
        if project is not None:
            main_watch.observe(target, conn, project, now)
    finally:
        conn.close()


def observe_one(target, now, end, stop):
    try:
        with deadline.bounded(end, stop) as bound:
            observe_store(target, now)
    except (deadline.DeadlineReached, deadline.CallRefused) as reached:
        return str(reached), None
    except (Exception, SystemExit) as bad:  # noqa: BLE001 - the boundary
        return None, bad
    if bound.refused:
        return bound.refused[0], None
    return None, None


def run_automations(entries, state, stop, sweep_sec, now, out, *, key, save):
    order = watched(entries, out, key)
    if not order:
        return
    names = [key(entry) for entry in order]
    cursor = state.get("automation_cursor")
    start = names.index(cursor) if cursor in names else 0
    order = order[start:] + order[:start]
    end = time.monotonic() + sweep_sec * AUTOMATION_SHARE
    after_cut = None
    for index, entry in enumerate(order):
        name = key(entry)
        state["automation_cursor"] = name
        if stop.is_set() or time.monotonic() >= end:
            return
        save(state)
        share = (end - time.monotonic()) / (len(order) - index)
        reached, bad = observe_one(entry.target, now,
                                   time.monotonic() + share, stop)
        if reached is not None:
            print(f"[holo2] {name}: automations cut, {reached}; the next"
                  " pass starts after it", file=out)
        if bad is not None:
            print(f"[holo2] {name}: automations failed: {bad}", file=out)
        state["automation_cursor"] = names[(start + index + 1) % len(names)]
        if reached is not None or bad is not None:
            after_cut = state["automation_cursor"]
        save(state)
    state["automation_cursor"] = after_cut or names[(start + 1) % len(names)]
