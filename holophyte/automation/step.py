"""The host sweep's automation step: each opted-in project's rules, round robin."""
import time

import store.read
from holophyte import deadline
from holophyte.admission import project_of
from holophyte.automation import main_watch
from holophyte.automation.automation_config import (
    DRY_RUN,
    MAIN_WATCH,
    main_watch_mode,
)
from holophyte.automation.gate import inhibitor, read_switch
from holophyte.loop.runs import open_store

AUTOMATION_SHARE = 0.25


def inhibited(entry, home):
    conn = store.read.open_readonly(entry.target.store_path)
    try:
        return inhibitor(conn, home, MAIN_WATCH)
    finally:
        conn.close()


def watched(entries, out, key, home):
    """The entries in `dry_run`; a wrong mode is off for this pass, said once."""
    found = []
    for entry in entries:
        try:
            if inhibited(entry, home) is not None:
                continue
        except (Exception, SystemExit) as bad:
            print(f"[holo2] {key(entry)}: automations failed: {bad}",
                  file=out)
            continue
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


def run_automations(entries, state, stop, sweep_sec, now, out, *, key, save,
                    home):
    switch = read_switch(home)
    if switch.problem is not None:
        print(f"[holo2] automations off: {switch.problem}", file=out)
    if switch.off:
        return
    order = watched(entries, out, key, home)
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
