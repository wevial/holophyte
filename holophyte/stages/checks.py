"""A project's `[[pipeline.check]]` stages run on a candidate and recorded."""
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from holophyte.agents.review_workspace import review_base
from holophyte.config.config_tables import verify_config
from holophyte.config.pipeline_settings import (
    CHECK_MODES,
    check_secrets,
    current_checks,
)
from holophyte.loop.gates import _verify_command
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import REDACTED, outbound
from holophyte.review.blast_radius import matching
from holophyte.review.briefs import _changed_files
from holophyte.stages import record
from store import pipelines

OUTPUT_TAIL = 1000
FRAGMENT = 8
GROUP_GRACE_S = 2.0
UNLAUNCHED = re.compile(
    r"^(?:\S*/)?(?:ba|da|z)?sh: (?:line )?(?:\d+: )?.+: "
    r"(?:command not found|not found|Permission denied|cannot execute.*)$")
UNBOUND = ("this check's pinned commands hold a redacted value this process "
           "did not pin, so it cannot run them")
UNREAD = "this check's pinned mode is not one of advisory, off"


def run(project, conn, run_id, beat_s, wt, base_sha, sha, after, **detail):
    for check in configured(project, conn, run_id, after):
        reason = skip_reason(check, wt, base_sha, sha)
        if reason:
            record.skip(conn, run_id, check["id"], reason, sha)
            continue
        with record.Attempt(conn, run_id, check["id"], sha,
                            {**detail, "mode": check["mode"]},
                            wt=wt) as attempt, \
                heartbeat_while(conn, run_id, beat_s):
            outcome, found = _ran_bound(project, check, wt, sha)
            attempt.close(outcome, "next", sha, **found)


def _ran_bound(project, check, wt, sha):
    if check["mode"] not in CHECK_MODES:
        return "error", {"error": UNREAD}
    if check["commands"] is None:
        return "error", {"error": UNBOUND}
    return ran(project, check, wt, sha)


def configured(project, conn, run_id, after):
    secrets = check_secrets(project)
    pinned = None
    if conn is not None and run_id is not None:
        pinned = pipelines.run_pipelines(conn, [run_id]).get(run_id)
    if not pinned:
        return [{**check, "secrets": secrets}
                for check in current_checks(project) if check["after"] == after]
    stages = [stage for stage in pinned.get("stages", []) if "after" in stage]
    raw, held = record.pinned_raw_checks(conn, run_id) or ((), frozenset())
    if len(raw) == len(stages):
        return [{**check, "id": stage["id"], "secrets": secrets | held}
                for stage, check in zip(stages, raw) if check["after"] == after]
    return [{"id": stage["id"], "after": stage["after"], "mode": stage["mode"],
             "paths": stage.get("paths") or [], "secrets": secrets,
             "commands": None if stage.get("redacted") else stage["commands"]}
            for stage in stages if stage["after"] == after]


def skip_reason(check, wt, base_sha, sha):
    if check["mode"] == "off":
        return "mode off"
    if not check["paths"]:
        return None
    changed = _changed_files(wt, review_base(wt, base_sha, sha), sha)
    if any(matching(path, check["paths"]) for path in changed):
        return None
    return f"no changed file matches {', '.join(check['paths'])}"


def ran(project, check, wt, sha):
    held = tempfile.mkdtemp(prefix="holo-check-")
    tree = Path(held, "worktree")
    try:
        _git(wt, "worktree", "add", "--detach", "--quiet", str(tree), sha)
        outcome, found = _outcome(project, check, tree)
    except Exception as error:
        outcome, found = "error", {"error": str(error)}
    finally:
        left = _removed(wt, tree, held)
    if left is not None:
        found["cleanup_error"] = left
    return outcome, {key: scrubbed_output(value, check["secrets"])
                     if key in ("output", "error", "cleanup_error") else value
                     for key, value in found.items()}


def _removed(wt, tree, held):
    try:
        _git(wt, "worktree", "remove", "--force", "--force", str(tree))
    except Exception:
        pass
    try:
        if Path(held).exists():
            _opened(held)
            shutil.rmtree(held)
        _git(wt, "worktree", "prune")
    except Exception as error:
        return f"could not remove the check's worktree {held}: {error}"
    return None


def _opened(root):
    for top, dirs, _ in os.walk(root):
        for name in dirs:
            path = Path(top, name)
            if not path.is_symlink():
                os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IRWXU)


def _git(wt, *args):
    done = subprocess.run(["git", *args], cwd=wt, capture_output=True)
    if done.returncode != 0:
        raise OSError(f"git {args[0]} {args[1] if len(args) > 1 else ''}"
                      f" failed: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout.decode().strip()


def _outcome(project, check, wt):
    started = []
    try:
        status, out = _verify_command(project, " && ".join(check["commands"]),
                                      wt, verify_config(project).timeout_sec,
                                      on_start=lambda proc: _held(proc, started))
    except subprocess.TimeoutExpired as expired:
        return "fail", {"exit_status": None, "timed_out": True,
                        "output": _text(expired.output)}
    except Exception as error:
        return "error", {"error": str(error)}
    finally:
        for proc in started:
            _end_group(proc.pid)
    out = _text(out)
    if status == 0:
        return "pass", {"exit_status": 0, "output": out}
    last = out.splitlines()[-1] if out.strip() else ""
    unlaunched = status in (126, 127) and bool(UNLAUNCHED.match(last))
    return "error" if unlaunched else "fail", {
        "exit_status": status, "timed_out": False, "output": out}


def _held(proc, started):
    started.append(proc)
    if isinstance(proc.stdout, io.TextIOWrapper):
        proc.stdout.reconfigure(errors="replace")


def _end_group(group):
    deadline = time.monotonic() + GROUP_GRACE_S
    try:
        os.killpg(group, signal.SIGKILL)
        while time.monotonic() < deadline:
            os.killpg(group, 0)
            time.sleep(0.02)
    except (ProcessLookupError, PermissionError):
        pass


def _text(out):
    if isinstance(out, bytes):
        return out.decode(errors="replace")
    return out or ""


def scrubbed_output(text, secrets):
    text = outbound(text, secrets)
    for secret in sorted(secrets, key=len, reverse=True):
        for size in range(len(secret) - 1, FRAGMENT - 1, -1):
            for part in (secret[:size], secret[-size:]):
                text = text.replace(part, REDACTED)
    return text[-OUTPUT_TAIL:]
