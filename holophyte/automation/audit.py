"""`holo automation list`, `show ID` and `settle ID`: a firing's record."""
import getpass
import json
import sys
from time import time

import store.automations
import store.read
from holophyte.holo.grammar import AUTOMATION_READS
from holophyte.holo.render import age_since, clock, day, zone
from holophyte.loop.runs import open_store

DEFAULT_LIMIT = 20
LIST_MODE, SHOW_MODE = AUTOMATION_READS
LABEL = 10


def firing_json(firing):
    return {"id": firing.id, "automation": firing.automation,
            "revision": firing.revision, "subject": firing.subject,
            "cause": firing.cause, "firing_key": firing.firingKey,
            "mode": firing.mode, "state": firing.state,
            "reason": firing.reason, "detail": firing.detail,
            "created_ms": firing.createdAt, "settled_ms": firing.settledAt,
            "lease_until_ms": firing.leaseUntil,
            "worker_pid": firing.workerPid}


def hold_json(conn, hold):
    return {"id": hold.id, "sha": hold.sha, "checks": hold.checks,
            "firing": hold.firingId, "held_ms": hold.heldAt,
            "ended_ms": hold.endedAt, "end_reason": hold.endReason,
            "parked": [{"run": run.runId, "ticket": run.identifier}
                       for run in store.automations.held_runs(conn, hold.id)]}


def intervention_json(row):
    return {"id": row.id, "run": row.runId, "ticket": row.identifier,
            "action": row.action, "source": row.source, "actor": row.actor,
            "note": row.note, "at_ms": row.at}


def list_body(conn, limit, now):
    return {"firings": [firing_json(firing) for firing in
                        store.automations.firings(conn)[:limit]],
            "now_ms": now}


def show_body(conn, firing_id):
    firing = store.automations.firing(conn, firing_id)
    if firing is None:
        return None
    return {"firing": firing_json(firing),
            "holds": [hold_json(conn, hold) for hold in
                      store.automations.firing_holds(conn, firing)],
            "interventions": [
                intervention_json(row) for row in
                store.automations.firing_interventions(conn, firing.id)]}


def first_line(text):
    return text.splitlines()[0] if text else ""


def list_lines(body):
    now = body["now_ms"]
    return [f"{row['id']} {row['automation']} {row['state']} {row['mode']}"
            f" {age_since(row['created_ms'], now)} ago {row['subject']}"
            f" {row['cause']}: {first_line(row['reason'])}"
            for row in body["firings"]]


def when(ms, tz):
    return "-" if ms is None else f"{day(ms, tz)}, {clock(ms, tz)}"


def shown(value):
    return value if isinstance(value, str) else json.dumps(value)


def hold_lines(hold, tz):
    ended = ("open" if hold["ended_ms"] is None else
             f"ended {when(hold['ended_ms'], tz)}: {hold['end_reason']}")
    lines = [f"hold {hold['id']} at {hold['sha'][:12]}"
             f" ({', '.join(hold['checks'])}), held"
             f" {when(hold['held_ms'], tz)}, {ended}"]
    if hold["parked"]:
        lines.append("  parked " + ", ".join(
            f"{run['ticket']} (run {run['run']})" for run in hold["parked"]))
    return lines


def show_lines(body, tz):
    firing = body["firing"]
    fields = [("key", firing["firing_key"]),
              ("mode", firing["mode"]), ("state", firing["state"]),
              ("reason", firing["reason"]),
              ("created", when(firing["created_ms"], tz)),
              ("settled", when(firing["settled_ms"], tz))]
    lines = [f"firing {firing['id']} · {firing['automation']}"
             f" revision {firing['revision']}"]
    lines += [f"{name.ljust(LABEL)}{value}" for name, value in fields]
    lines += ["detail"] + [f"  {key}: {shown(value)}"
                           for key, value in firing["detail"].items()]
    for hold in body["holds"]:
        lines += hold_lines(hold, tz)
    lines.append(f"interventions ({len(body['interventions'])})")
    lines += [f"  {row['id']} {row['ticket'] or '-'} run {row['run']}"
              f" {row['action']} by {row['source']}, {when(row['at_ms'], tz)}"
              for row in body["interventions"]]
    return lines


def render(mode, body, zone_name):
    return (list_lines(body) if mode == LIST_MODE
            else show_lines(body, zone(zone_name)))


def missing(firing_id, target):
    return f"[holo2] no automation firing {firing_id} in {target.store_path}"


def read(target, mode, firing_id=None, limit=None, as_json=False,
         zone_name=None, out=None):
    out = sys.stdout if out is None else out
    body = {"firings": [], "now_ms": int(time() * 1000)}
    if target.store_path.exists():
        conn = store.read.open_readonly(target.store_path)
        try:
            body = (list_body(conn, limit or DEFAULT_LIMIT, body["now_ms"])
                    if mode == LIST_MODE else show_body(conn, firing_id))
        finally:
            conn.close()
    elif mode == SHOW_MODE:
        body = None
    if body is None:
        print(missing(firing_id, target), file=sys.stderr)
        return 1
    text = (json.dumps(body) if as_json
            else "\n".join(render(mode, body, zone_name)))
    if text:
        print(text, file=out)
    return 0


def settle(target, firing_id, state, note):
    conn = open_store(target)
    try:
        if not store.automations.settle_unknown(
                conn, firing_id, state, note, getpass.getuser(),
                now=int(time() * 1000)):
            firing = store.automations.firing(conn, firing_id)
            if firing is None:
                raise SystemExit(missing(firing_id, target))
            raise SystemExit(
                f"[holo2] automation firing {firing_id} is {firing.state};"
                " only an unknown firing is settled by hand")
    finally:
        conn.close()
    print(f"[holo2] automation firing {firing_id} settled {state}: {note}")
