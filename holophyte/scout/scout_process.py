import os
import shutil
import signal
import stat
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import store
from holophyte.agents.review_workspace import review_worktrees, scratch_git_environment
from holophyte.config.config_tables import sweep_config
from holophyte.loop.gates import sh
from holophyte.loop.runs import open_store
from holophyte.redact import safe_print as print
from holophyte.scout.answer import Outcome, answer, settle, stop_answer
from holophyte.scout.lead import run_lead
from holophyte.scout.stop import stop_reason, stop_request
from holophyte.scout.swarm import ScoutStopped, Stopping
from store.scouts import (
    ANSWERING,
    PLANNING,
    ScoutRefused,
    beat_scout,
    claim_scout,
    end_scout,
    record_scout_event,
    scout,
)

BEATS_PER_STALE = 5
# A path under a file: no remote can ever resolve there.
NO_PUSH_URL = "/dev/null/scout-lead-refuses-push"
PRE_PUSH = "#!/bin/sh\necho 'a scout lead worktree does not push' >&2\nexit 1\n"


class ScoutEnded(Exception):
    pass


def scout_dir(project, scout_id):
    return project.holo_dir / "scouts" / str(scout_id)


def lead_path(project, scout_id):
    return scout_dir(project, scout_id) / "lead"


def refusal(scout_id, row):
    if row is None:
        return f"[holo2] scout {scout_id}: the store holds no such scout"
    if row.phase not in (PLANNING, ANSWERING):
        return (f"[holo2] scout {scout_id} is {row.phase}, not {PLANNING} or"
                f" {ANSWERING}; nothing to run")
    if row.pid is not None:
        return (f"[holo2] scout {scout_id} is {row.phase} and already records"
                f" pid {row.pid}; nothing to run")
    return None


def _beat(path, scout_id, interval_s, stop, ended):
    own = None
    try:
        while not stop.wait(interval_s):
            try:
                own = own or store.open(path)
                beat_scout(own, scout_id)
            except ScoutRefused as refused:
                ended.append(str(refused))
                return
            except Exception as bad:  # noqa: BLE001 - the next beat retries
                print(f"[holo2] scout {scout_id} heartbeat failed: {bad}",
                      flush=True)
    finally:
        if own is not None:
            own.close()


@contextmanager
def scout_heartbeat(conn, scout_id, interval_s):
    (path,) = [row[2] for row in conn.execute("PRAGMA database_list")
               if row[1] == "main"]
    stop, ended = threading.Event(), []
    thread = threading.Thread(target=_beat,
                              args=(path, scout_id, interval_s, stop, ended),
                              name=f"heartbeat-scout-{scout_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()
        if ended:
            raise ScoutEnded(ended[0])


def clear_lead(repo, path, env):
    if path.is_symlink():
        path.unlink()
    path = path.parent.resolve() / path.name
    if any(listed.resolve() == path
           for listed in review_worktrees(repo, env=env)):
        sh(["git", "worktree", "remove", "--force", "--force", str(path)],
           cwd=repo, env=env)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    sh(["git", "worktree", "prune"], cwd=repo, env=env)


def git_config(config, *args, env):
    found = subprocess.run(["git", "config", "--file", str(config), *args],
                           env=env, capture_output=True, text=True)
    return found.stdout.strip() if found.returncode == 0 else None


def enable_worktree_config(repo, env):
    common = Path(sh(["git", "rev-parse", "--path-format=absolute",
                      "--git-common-dir"], cwd=repo, env=env))
    shared, main_only = common / "config", common / "config.worktree"
    if git_config(shared, "--bool", "extensions.worktreeConfig",
                  env=env) == "true":
        return
    bare = git_config(shared, "--bool", "core.bare", env=env)
    worktree = git_config(shared, "core.worktree", env=env)
    for key, value in (("core.bare", "true" if bare == "true" else None),
                       ("core.worktree", worktree)):
        if value is not None:
            sh(["git", "config", "--file", str(main_only), key, value],
               env=env)
            sh(["git", "config", "--file", str(shared), "--unset", key],
               env=env)
    sh(["git", "config", "--file", str(shared), "extensions.worktreeConfig",
        "true"], env=env)


def refuse_push(repo, checkout, hooks, env):
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(PRE_PUSH)
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    enable_worktree_config(repo, env)
    for key, value in (("core.hooksPath", str(hooks)),
                       ("remote.origin.pushurl", NO_PUSH_URL)):
        sh(["git", "config", "--worktree", key, value], cwd=checkout, env=env)


def make_lead(project, scout_id, base_sha):
    repo, path = project.path, lead_path(project, scout_id)
    env = scratch_git_environment()
    clear_lead(repo, path, env)
    found = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"{base_sha}^{{commit}}"],
        cwd=repo, env=env, capture_output=True, text=True)
    if found.returncode != 0:
        raise RuntimeError(f"base {base_sha} names no commit in {repo}")
    path.parent.mkdir(parents=True, exist_ok=True)
    sh(["git", "worktree", "add", "--detach", "--quiet", str(path), base_sha],
       cwd=repo, env=env)
    refuse_push(repo, path, scout_dir(project, scout_id) / "hooks", env)
    return path


def _end(conn, scout_id, phase, kind, summary, reason):
    with store.transaction(conn):
        record_scout_event(conn, scout_id, kind, summary)
        end_scout(conn, scout_id, phase, reason)


@contextmanager
def stop_on_sigterm(stopping):
    def on_term(_signum, _frame):
        if stopping.stop() and not stopping.starting:
            raise ScoutStopped()

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def end_stopped(conn, scout_id):
    if conn.in_transaction:
        conn.rollback()
    reason = stop_reason(conn, scout_id)
    try:
        _end(conn, scout_id, "stopped", "scout_stopped",
             f"scout {scout_id} stopped: {reason}", reason)
    except ScoutRefused as ended:
        print(f"[holo2] scout {scout_id} ended elsewhere: {ended}", flush=True)
        return 1
    print(f"[holo2] scout {scout_id} stopped: {reason}", flush=True)
    return 0


def answer_stopped(conn, scout_id):
    if conn.in_transaction:
        conn.rollback()
    reason, asked = stop_reason(conn, scout_id), stop_request(conn, scout_id)
    try:
        stop_answer(conn, scout_id, reason, asked and asked["author"],
                    asked and asked["note"])
    except ScoutRefused as ended:
        print(f"[holo2] scout {scout_id} ended elsewhere: {ended}", flush=True)
        return 1
    print(f"[holo2] scout {scout_id} answer stopped: {reason}", flush=True)
    return 0


def answered(conn, project, row, stopping):
    try:
        worktree = make_lead(project, row.id, row.base_sha)
        return answer(conn, project, row, worktree, stopping)
    except ScoutRefused:
        raise
    except Exception as bad:
        return Outcome(None, failure=str(bad).strip() or type(bad).__name__)


def answer_mode(conn, project, row, stopping):
    scout_id = row.id
    interval_s = sweep_config(project).heartbeat_stale_ms / 1000 / BEATS_PER_STALE
    try:
        beat_scout(conn, scout_id, os.getpid())
        owner = scout(conn, scout_id).pid
        if owner != os.getpid():
            raise ScoutRefused(scout_id, ANSWERING, f"pid {owner} answers it")
        with scout_heartbeat(conn, scout_id, interval_s):
            outcome = answered(conn, project, row, stopping)
        try:
            return settle(conn, scout_id, outcome)
        except ScoutRefused:
            raise
        except Exception as bad:
            if conn.in_transaction:
                conn.rollback()
            reason = str(bad).strip() or type(bad).__name__
            return settle(conn, scout_id, outcome._replace(failure=reason))
    except (ScoutEnded, ScoutRefused) as ended:
        print(f"[holo2] scout {scout_id} ended elsewhere: {ended}", flush=True)
        return 1


def scout_mode(project, scout_id):
    conn = open_store(project)
    try:
        row = scout(conn, scout_id)
        refused = refusal(scout_id, row)
        if refused is not None:
            print(refused, flush=True)
            return 1
        stopping = Stopping()
        answering = row.phase == ANSWERING
        with stop_on_sigterm(stopping):
            try:
                if answering:
                    return answer_mode(conn, project, row, stopping)
                return run_scout(conn, project, row, stopping)
            except ScoutStopped:
                if answering:
                    return answer_stopped(conn, scout_id)
                return end_stopped(conn, scout_id)
    finally:
        conn.close()


def run_scout(conn, project, row, stopping):
    scout_id = row.id
    interval_s = sweep_config(project).heartbeat_stale_ms / 1000 / BEATS_PER_STALE
    try:
        claim_scout(conn, scout_id, os.getpid())
    except ScoutRefused as lost:
        print(f"[holo2] scout {scout_id} was claimed elsewhere: {lost}",
              flush=True)
        return 1
    try:
        record_scout_event(conn, scout_id, "scout_started",
                           f"scout {scout_id} started as pid {os.getpid()}")
        try:
            with scout_heartbeat(conn, scout_id, interval_s):
                lead = make_lead(project, scout_id, row.base_sha)
                phase, reason = run_lead(conn, project, row, lead,
                                         row.budget_min * 60, stopping)
        except (ScoutEnded, ScoutRefused):
            raise
        except Exception as bad:  # noqa: BLE001 - recorded as the scout's end
            reason = str(bad).strip() or type(bad).__name__
            _end(conn, scout_id, "failed", "scout_failed",
                 f"scout {scout_id} failed: {reason}", reason)
            print(f"[holo2] scout {scout_id} failed: {reason}", flush=True)
            return 1
        _end(conn, scout_id, phase, f"scout_{phase}",
             f"scout {scout_id} {phase}: {reason}", reason)
    except (ScoutEnded, ScoutRefused) as ended:
        print(f"[holo2] scout {scout_id} ended elsewhere: {ended}",
              flush=True)
        return 1
    print(f"[holo2] scout {scout_id} {phase}: {reason}", flush=True)
    return 0 if phase == "done" else 1
