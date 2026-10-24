import os
import shutil
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
from store.scouts import (
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
    if row.phase != PLANNING:
        return (f"[holo2] scout {scout_id} is {row.phase}, not {PLANNING};"
                " nothing to run")
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


def scout_mode(project, scout_id):
    conn = open_store(project)
    try:
        row = scout(conn, scout_id)
        refused = refusal(scout_id, row)
        if refused is not None:
            print(refused, flush=True)
            return 1
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
                    make_lead(project, scout_id, row.base_sha)
            except (ScoutEnded, ScoutRefused):
                raise
            except Exception as bad:  # noqa: BLE001 - recorded as the scout's end
                reason = str(bad).strip() or type(bad).__name__
                _end(conn, scout_id, "failed", "scout_failed",
                     f"scout {scout_id} failed: {reason}", reason)
                print(f"[holo2] scout {scout_id} failed: {reason}", flush=True)
                return 1
            _end(conn, scout_id, "done", "scout_done",
                 f"scout {scout_id} done", "the lead worktree is ready")
        except (ScoutEnded, ScoutRefused) as ended:
            print(f"[holo2] scout {scout_id} ended elsewhere: {ended}",
                  flush=True)
            return 1
        print(f"[holo2] scout {scout_id} done", flush=True)
        return 0
    finally:
        conn.close()
