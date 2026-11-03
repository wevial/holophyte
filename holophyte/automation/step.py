"""The host sweep's automation step: each opted-in project's rules, round robin."""
import subprocess
import sys
import time

import store.automations
import store.read
from holophyte import deadline
from holophyte.admission import project_of
from holophyte.automation import main_watch
from holophyte.automation.automation_config import (
    ACT,
    DRY_RUN,
    MAIN_WATCH,
    main_watch_config,
)
from holophyte.automation.gate import read_switch
from holophyte.automation.policy import report_only
from holophyte.cli.host_modes import FACTORY_PATH
from holophyte.host.registry import Host, HostError
from holophyte.loop.reexec import (
    start_transient_unit,
    systemd_runner,
    transient_unit,
)
from holophyte.loop.runs import open_store

AUTOMATION_SHARE = 0.25
SPAWN = subprocess.Popen


def paused(entry):
    conn = store.read.open_readonly(entry.target.store_path)
    try:
        return store.automations.automation_pause(conn, MAIN_WATCH)
    finally:
        conn.close()


def watched(entries, out, key):
    """Each entry in `dry_run` or `act` with its config; a wrong mode is off."""
    found = []
    for entry in entries:
        try:
            if paused(entry) is not None:
                continue
        except (Exception, SystemExit) as bad:
            print(f"[holo2] {key(entry)}: automations failed: {bad}",
                  file=out)
            continue
        try:
            watch = main_watch_config(entry.target)
        except ValueError as bad:
            print(f"[holo2] {key(entry)}: {bad}; the main watch is off for"
                  " it this pass", file=out)
            continue
        if watch.mode in (DRY_RUN, ACT):
            found.append((entry, watch))
    return found


def start_worker(name, target, firing_id):
    log = target.holo_dir / "automations" / f"firing-{firing_id}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-u", str(FACTORY_PATH), "--automation-firing",
            str(firing_id), str(target.path)]
    runner = systemd_runner()
    if runner:
        unit = transient_unit("automation", name, firing_id)
        start_transient_unit(runner, unit, log, argv)
        return f"unit {unit}"
    with open(log, "ab") as handle:
        child = SPAWN(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                      stdout=handle, stderr=handle)
    return f"pid {child.pid}"


def start_workers(name, target, leased, out):
    for firing_id in leased:
        started = start_worker(name, target, firing_id)
        print(f"[holo2] {name}: firing {firing_id} leased to a worker,"
              f" {started}", file=out)


def observe_store(name, target, watch, listed, now, out):
    leased = []
    conn = open_store(target)
    try:
        project = project_of(conn, target)
        if project is not None:
            main_watch.observe(target, conn, project, now, watch, listed,
                               leased)
    finally:
        conn.close()
        start_workers(name, target, leased, out)


def observe_one(name, target, watch, listed, now, end, stop, out):
    try:
        with deadline.bounded(end, stop) as bound:
            observe_store(name, target, watch, listed, now, out)
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
    try:
        listed = report_only(Host.locate(home))
    except HostError as bad:
        print(f"{bad}; automations are off this pass", file=out)
        return
    order = watched(entries, out, key)
    if not order:
        return
    names = [key(entry) for entry, _watch in order]
    cursor = state.get("automation_cursor")
    start = names.index(cursor) if cursor in names else 0
    order = order[start:] + order[:start]
    end = time.monotonic() + sweep_sec * AUTOMATION_SHARE
    after_cut = None
    for index, (entry, watch) in enumerate(order):
        name = key(entry)
        state["automation_cursor"] = name
        if stop.is_set() or time.monotonic() >= end:
            return
        save(state)
        share = (end - time.monotonic()) / (len(order) - index)
        reached, bad = observe_one(name, entry.target, watch, listed, now,
                                   time.monotonic() + share, stop, out)
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
