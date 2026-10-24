import fcntl
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import store
from holophyte.admission import project_of
from holophyte.admission import state as admission_state
from holophyte.agents.review_workspace import scratch_git_environment
from holophyte.cli.host_modes import FACTORY_PATH
from holophyte.config.checks import check_config
from holophyte.config.config_tables import sweep_config
from holophyte.host.registry import home
from holophyte.loop.gates import sh
from holophyte.scout.answer import current_ask, fail_answer
from holophyte.scout.follow_up import current_pass, fail_pass
from holophyte.scout.scout_process import scout_dir
from store.enums import ScoutPhase
from store.scout_thread import (
    ASK,
    STEER,
    Message,
    ThreadRefused,
    take_message,
    waiting_messages,
)
from store.scouts import (
    ANSWERING,
    PLANNING,
    QUEUED,
    ScoutRefused,
    end_scout,
    move_scout,
    record_scout_event,
    scout,
    scouts,
)

LIVE = tuple(phase.value for phase in (
    ScoutPhase.PLANNING, ScoutPhase.SWARMING, ScoutPhase.SYNTHESIZING,
    ScoutPhase.ANSWERING))
SPAWN = subprocess.Popen
START_LOCK = "scout-start.lock"
START_FAILED = "scout_start_failed"
SANDBOX_UNAVAILABLE = "scout_sandbox_unavailable"
SYSTEMD_UNIT_VARS = ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID",
             "NOTIFY_SOCKET", "MANAGERPID")


def _project_scouts(conn, target, phases):
    project = project_of(conn, target)
    return [row for row in scouts(conn, phases) if row.project_id == project]


def _end_one_stale(conn, row, reason, now):
    if scout(conn, row.id).heartbeat_at != row.heartbeat_at:
        return None
    if row.phase == ANSWERING:
        fail_answer(conn, row.id, current_ask(conn, row.id), reason)
        return "answer failed, back to done"
    steered = current_pass(conn, row.id)
    if steered is not None:
        fail_pass(conn, row.id, steered, reason)
        return f"pass {steered['pass']} failed, back to done"
    end_scout(conn, row.id, "failed", reason, now=now)
    return "ended failed"


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
                ended = _end_one_stale(conn, row, reason, now)
        except ScoutRefused:
            continue
        if ended is not None:
            print(f"[holo2] {name}: scout {row.id} {ended}: {reason}",
                  file=out)


def _escape(match):
    return "".join(f"_{byte:02x}" for byte in match.group().encode())


def scout_unit(name, scout_id):
    return f"holophyte-scout-{re.sub(r'[^A-Za-z0-9.]', _escape, name)}-{scout_id}"


def _systemd_run():
    return os.environ.get("INVOCATION_ID") and shutil.which("systemd-run")


def _private_tmp_spawn():
    return bool(os.environ.get("INVOCATION_ID")) and not shutil.which(
        "systemd-run")


SANDBOXLESS = ("systemd-run is missing under a systemd unit, so the scout would"
               " inherit its PrivateTmp, where Codex read-only turns cannot"
               " start")


def _fail_sandboxless(conn, scout_id):
    with store.transaction(conn):
        record_scout_event(conn, scout_id, SANDBOX_UNAVAILABLE, SANDBOXLESS)
        end_scout(conn, scout_id, "failed", SANDBOXLESS)


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


def _start_failed(conn, scout_id, bad):
    with store.transaction(conn):
        record_scout_event(conn, scout_id, START_FAILED,
                           f"could not start: {bad}; queued for the next pass")
        move_scout(conn, scout_id, QUEUED)


def _start(name, target, row, out):
    conn = store.open(str(target.store_path))
    try:
        base = sh(["git", "rev-parse", "--verify", "main^{commit}"],
                  cwd=target.path, env=scratch_git_environment())
        move_scout(conn, row.id, PLANNING, base_sha=base,
                   host=socket.gethostname())
        if _private_tmp_spawn():
            _fail_sandboxless(conn, row.id)
            print(f"[holo2] {name}: scout {row.id} ended failed: {SANDBOXLESS}",
                  file=out)
            return False
        try:
            started = start_scout(name, target, row.id)
        except OSError as bad:
            _start_failed(conn, row.id, bad)
            raise
    except ScoutRefused:
        return False
    finally:
        conn.close()
    print(f"[holo2] {name}: started scout {row.id} at {base[:12]} as"
          f" {started}", file=out)
    return True


def _config_refusal(target):
    try:
        check_config(target)
    except (SystemExit, ValueError) as bad:
        return str(bad) or type(bad).__name__
    return None


def _message_failed(conn, message, reason):
    if message.mode == ASK:
        fail_answer(conn, message.scout_id, message.id, reason)
    else:
        fail_pass(conn, message.scout_id,
                  current_pass(conn, message.scout_id), reason)


def _start_message(name, target, message, out):
    work = "answer" if message.mode == ASK else "pass"
    refused = _config_refusal(target)
    if refused is not None:
        print(f"[holo2] {name}: scout {message.scout_id}'s {message.mode}"
              f" {message.id} waits: {refused}", file=out)
        return False
    conn = store.open(str(target.store_path))
    try:
        take_message(conn, message.id, host=socket.gethostname())
        if _private_tmp_spawn():
            _message_failed(conn, message, SANDBOXLESS)
            print(f"[holo2] {name}: scout {message.scout_id} {work} failed:"
                  f" {SANDBOXLESS}", file=out)
            return False
        try:
            started = start_scout(name, target, message.scout_id)
        except OSError as bad:
            _message_failed(conn, message, f"could not start: {bad}")
            raise
    except ThreadRefused:
        return False
    finally:
        conn.close()
    doing = "answering" if message.mode == ASK else "on a pass steered by"
    print(f"[holo2] {name}: started scout {message.scout_id} {doing} message"
          f" {message.id} as {started}", file=out)
    return True


def _waiting(conn, target):
    project = project_of(conn, target)
    asks = [(message.at, message) for message in waiting_messages(conn)
            if message.mode in (ASK, STEER)
            and message.project_id == project]
    return [(row.created_at, row) for row in
            _project_scouts(conn, target, (QUEUED,))] + asks


def _account(name, entry, conn, now, out, queued):
    admits = entry.error is None
    try:
        if now is not None:
            end_stale(name, entry.target, conn, now, out)
    except (Exception, SystemExit) as bad:  # noqa: BLE001 - its scouts still count
        print(f"[holo2] {name}: stale scout check failed: {bad}", file=out)
        admits = False
    if admits and admission_state(conn, entry.target)[0] == "enabled":
        queued += [(at, name, entry.target, row)
                   for at, row in _waiting(conn, entry.target)]
    return len(_project_scouts(conn, entry.target, LIVE))


def sweep_scouts(entries, limit, now, out, key=str, home_dir=None):
    try:
        start_queued(entries, limit, out, key, now, home_dir)
    except OSError as bad:
        print(f"[holo2] no scout starts this pass: {bad}", file=out)


def start_queued(entries, limit, out, key=str, now=None, home_dir=None):
    lock = Path(home_dir or home()) / START_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        return _start_queued(entries, limit, out, key, now)


def _start_queued(entries, limit, out, key, now):
    live, queued, unknown, started = 0, [], False, []
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
        return live, started
    queued.sort(key=lambda item: (item[0], item[3].id))
    for _, name, target, row in queued:
        if live >= limit:
            break
        answers = isinstance(row, Message)
        try:
            if (_start_message if answers else _start)(name, target, row, out):
                live += 1
                started.append((target.path,
                                row.scout_id if answers else row.id))
        except (Exception, SystemExit) as bad:  # noqa: BLE001 - the project boundary
            print(f"[holo2] {name}: scout start failed: {bad}", file=out)
    return live, started
