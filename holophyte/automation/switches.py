"""The operator's stops: the host's kill switch and one automation's pause."""
import getpass
import json
import os
import sys
from time import time

import store.automations
from holophyte.automation.automation_config import BUILT_INS
from holophyte.automation.gate import SWITCH_FILE, read_switch
from holophyte.loop.runs import open_store
from holophyte.serve.serve_host import HOST_LEDGER, record_host_action


def save_switch(home, record):
    path = home / SWITCH_FILE
    temporary = path.with_name(f"{SWITCH_FILE}.{os.getpid()}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, indent=1))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as bad:
        temporary.unlink(missing_ok=True)
        raise SystemExit(f"[holo2] could not write {path} ({bad})") from None


def standing(switch):
    if switch.problem is not None:
        return switch.problem
    return (f"set by {switch.record.get('author')}: {switch.record.get('note')}"
            if switch.record else "no record")


def set_switch(home, off, note):
    word = "off" if off else "on"
    switch = read_switch(home)
    if switch.off == off and switch.problem is None:
        raise SystemExit(f"[holo2] automations are already {word},"
                         f" {standing(switch)}")
    now = int(time() * 1000)
    author = getpass.getuser()
    record = {"off": off, "note": note, "at": now, "author": author}
    home.mkdir(parents=True, exist_ok=True)
    try:
        record_host_action(home, {
            "at": now, "action": f"automations_{word}",
            "via": f"factory.py --automations-{word}", "pid": os.getpid(),
            "author": author, "note": note})
    except OSError as bad:
        raise SystemExit(f"[holo2] could not record in {home / HOST_LEDGER}"
                         f" ({bad}); automations left as they were") from None
    save_switch(home, record)
    print(f"[holo2] automations {word} on this host: {note}")


def built_in(name):
    if name not in BUILT_INS:
        print(f"[holo2] no automation named {name!r}; the built-ins are"
              f" {', '.join(BUILT_INS)}", file=sys.stderr)
        raise SystemExit(2)


def pause(target, name, note):
    built_in(name)
    now = int(time() * 1000)
    conn = open_store(target)
    try:
        if store.automations.pause_automation(
                conn, name, note, actor=store.automations.OPERATOR,
                now=now) is None:
            held = store.automations.automation_pause(conn, name)
            raise SystemExit(f"[holo2] automation {name} is already paused"
                             f" by {held.actor}: {held.note}")
    finally:
        conn.close()
    print(f"[holo2] automation {name} paused on {target.path}: {note}")


def resume(target, name, note):
    built_in(name)
    now = int(time() * 1000)
    conn = open_store(target)
    try:
        if store.automations.resume_automation(conn, name, note,
                                               now=now) is None:
            raise SystemExit(f"[holo2] automation {name} is not paused on"
                             f" {target.path}")
    finally:
        conn.close()
    print(f"[holo2] automation {name} resumed on {target.path}: {note}")
