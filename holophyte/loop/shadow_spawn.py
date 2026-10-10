import fcntl
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from time import time

import store
from holophyte.agents.harness import shadow_seat
from holophyte.loop.gates import read_merge_lock
from holophyte.loop.reexec import reexec_command
from holophyte.loop.runs import open_store
from holophyte.loop.shadow import ShadowBrief, run_shadow, shadow_branch, shadow_label
from holophyte.redact import known_secrets, outbound

SPAWN = subprocess.Popen
FACTORY = Path(__file__).resolve().parents[2] / "factory.py"


def shadows_dir(project):
    return project.holo_dir / "shadows"


def brief_path(project, run_id):
    return shadows_dir(project) / f"{run_id}.json"


def write_brief(project, run_id, brief):
    path = brief_path(project, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with open(fd, "w") as out:
        json.dump({"run_id": run_id, **asdict(brief)}, out)
    return path


def read_brief(path):
    fields = json.loads(Path(path).read_text())
    return fields.pop("run_id"), ShadowBrief(**fields)


def start_shadow(project, conn, run_id, brief):
    seat = shadow_seat(project)
    if seat is None or conn is None or run_id is None:
        return
    payload = {"pid": None, "branch": shadow_branch(brief.branch),
               "route": outbound(shadow_label(seat), known_secrets(project.config())),
               "error": None}
    path = brief_path(project, run_id)
    try:
        write_brief(project, run_id, brief)
        program, _ = reexec_command()
        log = os.open(path.with_suffix(".log"),
                      os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            child = SPAWN([program, str(FACTORY), "--shadow", str(path),
                           str(project.path)], stdin=subprocess.DEVNULL,
                          stdout=log, stderr=subprocess.STDOUT,
                          start_new_session=True)
        finally:
            os.close(log)
        payload["pid"] = child.pid
        summary = f"shadow {payload['route']} started as pid {child.pid}"
    except Exception as error:
        path.unlink(missing_ok=True)
        payload["error"] = str(error)
        summary = f"shadow {payload['route']} not started: {error}"
    store.record_event(conn, run_id, "shadow_started", summary, level="detail",
                       payload=json.dumps(payload))


def shadow_mode(target, path):
    try:
        run_id, brief = read_brief(path)
    finally:
        Path(path).unlink(missing_ok=True)
    conn = open_store(target)
    try:
        taken, busy = take_shadow_lock(target, run_id)
        if not taken:
            who = "another run" if busy is None else f"run {busy}"
            store.record_event(conn, run_id, "shadow_skipped",
                               f"shadow skipped: {who}'s shadow is running",
                               level="detail",
                               payload=json.dumps({"busy_run": busy}))
            return 0
        try:
            run_shadow(target, conn, run_id, brief)
        except Exception as error:
            store.record_event(conn, run_id, "shadow_result",
                               f"Shadow: error: {error}", level="detail",
                               payload=json.dumps({"outcome": "error",
                                                   "detail": str(error)}))
        return 0
    finally:
        conn.close()


def take_shadow_lock(target, run_id):
    # The flock is kept until this process exits; the file is never unlinked.
    path = target.holo_dir / "shadow.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        holder = read_merge_lock(path)
        return False, holder[0] if holder else None
    os.ftruncate(fd, 0)
    os.write(fd, f"{run_id} {time():.3f}\n".encode())
    return True, None
