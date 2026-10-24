import os
import re
import shutil
import socket
import subprocess
import sys

import store
from holophyte.admission import project_of
from holophyte.admission import state as admission_state
from holophyte.agents.review_workspace import scratch_git_environment
from holophyte.cli.host_modes import FACTORY_PATH
from holophyte.config.config_tables import sweep_config
from holophyte.loop.gates import sh
from holophyte.scout.scout_process import scout_dir
from store.enums import ScoutPhase
from store.scouts import (
    PLANNING,
    QUEUED,
    ScoutRefused,
    end_scout,
    move_scout,
    scout,
    scouts,
)

LIVE = tuple(phase.value for phase in (
    ScoutPhase.PLANNING, ScoutPhase.SWARMING, ScoutPhase.SYNTHESIZING,
    ScoutPhase.ANSWERING))
SPAWN = subprocess.Popen
SYSTEMD_UNIT_VARS = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID",
             "NOTIFY_SOCKET", "MANAGERPID")


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
            with store.transaction(conn):
                if scout(conn, row.id).heartbeat_at != row.heartbeat_at:
                    continue
                end_scout(conn, row.id, "failed", reason, now=now)
        except ScoutRefused:
            continue
        print(f"[holo2] {name}: scout {row.id} ended failed: {reason}",
              file=out)


def _escape(match):
    return "".join(f"_{byte:02x}" for byte in match.group().encode())


def scout_unit(name, scout_id):
    return f"holophyte-scout-{re.sub(r'[^A-Za-z0-9.]', _escape, name)}-{scout_id}"


def _systemd_run():
    return os.environ.get("INVOCATION_ID") and shutil.which("systemd-run")


def start_scout(name, target, scout_id):
    log = scout_dir(target, scout_id) / "scout.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-u", str(FACTORY_PATH), "--scout", str(scout_id),
            str(target.path)]
    runner = _systemd_run()
    if runner:
        unit = scout_unit(name, scout_id)
        ran = subprocess.run(
            [runner, "--user", "--collect", "--quiet", "--unit", unit,
             f"--working-directory={os.getcwd()}",
             f"--property=StandardOutput=append:{log}",
             f"--property=StandardError=append:{log}",
             *(f"--setenv={key}" for key in os.environ if key not in SYSTEMD_UNIT_VARS),
             *argv],
            stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if ran.returncode != 0:
            raise OSError(f"systemd-run exited {ran.returncode}:"
                          f" {ran.stderr.strip()}")
        return f"unit {unit}"
    with open(log, "ab") as handle:
        child = SPAWN(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                      stdout=handle, stderr=handle)
    return f"pid {child.pid}"


def _start(name, target, row, out):
    conn = store.open(str(target.store_path))
    try:
        base = sh(["git", "rev-parse", "--verify", "main^{commit}"],
                  cwd=target.path, env=scratch_git_environment())
        move_scout(conn, row.id, PLANNING, base_sha=base,
                   host=socket.gethostname())
        try:
            started = start_scout(name, target, row.id)
        except OSError as bad:
            end_scout(conn, row.id, "failed", f"could not start: {bad}")
            raise
    except ScoutRefused:
        return False
    finally:
        conn.close()
    print(f"[holo2] {name}: started scout {row.id} at {base[:12]} as"
          f" {started}", file=out)
    return True


def _account(name, entry, conn, now, out, queued):
    admits = entry.error is None
    try:
        end_stale(name, entry.target, conn, now, out)
    except (Exception, SystemExit) as bad:  # noqa: BLE001 - its scouts still count
        print(f"[holo2] {name}: stale scout check failed: {bad}", file=out)
        admits = False
    if admits and admission_state(conn, entry.target)[0] == "enabled":
        queued += [(name, entry.target, row) for row in
                   _project_scouts(conn, entry.target, (QUEUED,))]
    return len(_project_scouts(conn, entry.target, LIVE))


def sweep_scouts(entries, limit, now, out, key=str):
    live, queued, unknown = 0, [], False
    for entry in entries:
        if not entry.target.store_path.exists():
            continue
        name = key(entry)
        try:
            conn = store.open(str(entry.target.store_path))
            try:
                live += _account(name, entry, conn, now, out, queued)
            finally:
                conn.close()
        except (Exception, SystemExit) as bad:  # noqa: BLE001 - the project boundary
            print(f"[holo2] {name}: scout step failed: {bad}", file=out)
            unknown = True
    if unknown and queued:
        print("[holo2] no scout starts this pass: a store's live scouts could"
              " not be counted", file=out)
        return
    queued.sort(key=lambda item: (item[2].created_at, item[2].id))
    for name, target, row in queued:
        if live >= limit:
            return
        try:
            live += _start(name, target, row, out)
        except (Exception, SystemExit) as bad:  # noqa: BLE001 - the project boundary
            print(f"[holo2] {name}: scout start failed: {bad}", file=out)
