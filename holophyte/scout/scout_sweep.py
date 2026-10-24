import socket
import subprocess
import sys

import store
from holophyte.admission import project_of
from holophyte.cli.host_modes import FACTORY_PATH
from holophyte.config.config_tables import sweep_config
from holophyte.loop.gates import sh
from holophyte.scout.scout_process import lead_path
from store.enums import ScoutPhase
from store.scouts import (
    PLANNING,
    QUEUED,
    ScoutRefused,
     end_scout,
    move_scout,
    scouts,
)

LIVE = tuple(phase.value for phase in (
    ScoutPhase.PLANNING, ScoutPhase.SWARMING, ScoutPhase.SYNTHESIZING,
    ScoutPhase.ANSWERING))
SPAWN = subprocess.Popen


def _swept_stores(entries):
    for entry in entries:
        if entry.error is None and entry.target.store_path.exists():
            yield entry


def _project_scouts(conn, target, phases):
    project = project_of(conn, target)
    return [row for row in scouts(conn, phases) if row.project_id == project]


def end_stale(name, target, conn, now, out):
    stale_ms = sweep_config(target).heartbeat_stale_ms
    for row in _project_scouts(conn, target, LIVE):
        beaten = row.heartbeat_at or row.started_at or row.created_at
        if now - beaten <= stale_ms:
            continue
        reason = (f"heartbeat stale: last beat {(now - beaten) // 1000}s ago,"
                  f" past [supervisor] heartbeat_stale_min")
        try:
            end_scout(conn, row.id, "failed", reason, now=now)
        except ScoutRefused:
            continue
        print(f"[holo2] {name}: scout {row.id} ended failed: {reason}",
              file=out)


def start_scout(target, scout_id):
    log = lead_path(target, scout_id).parent / "scout.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as handle:
        child = SPAWN(
            [sys.executable, "-u", str(FACTORY_PATH), "--scout",
             str(scout_id), str(target.path)],
            start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=handle, stderr=handle)
    return child.pid


def _start(name, target, row, out):
    conn = store.open(str(target.store_path))
    try:
        base = sh(["git", "rev-parse", "--verify", "main^{commit}"],
                  cwd=target.path)
        move_scout(conn, row.id, PLANNING, base_sha=base,
                   host=socket.gethostname())
        pid = start_scout(target, row.id)
    except ScoutRefused:
        return False
    finally:
        conn.close()
    print(f"[holo2] {name}: started scout {row.id} at {base[:12]} as pid"
          f" {pid}", file=out)
    return True


def sweep_scouts(entries, limit, now, out, key=str):
    live, queued = 0, []
    for entry in _swept_stores(entries):
        name = key(entry)
        try:
            conn = store.open(str(entry.target.store_path))
            try:
                end_stale(name, entry.target, conn, now, out)
                live += len(_project_scouts(conn, entry.target, LIVE))
                queued += [(name, entry.target, row) for row in
                           _project_scouts(conn, entry.target, (QUEUED,))]
            finally:
                conn.close()
        except (Exception, SystemExit) as bad:  # noqa: BLE001 - the project boundary
            print(f"[holo2] {name}: scout step failed: {bad}", file=out)
    queued.sort(key=lambda item: (item[2].created_at, item[2].id))
    for name, target, row in queued:
        if live >= limit:
            return
        try:
            live += _start(name, target, row, out)
        except (Exception, SystemExit) as bad:  # noqa: BLE001 - the project boundary
            print(f"[holo2] {name}: scout start failed: {bad}", file=out)
