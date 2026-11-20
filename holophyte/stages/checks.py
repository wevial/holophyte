"""A project's `[[pipeline.check]]` stages run on a candidate and recorded."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from holophyte.agents.review_workspace import review_base
from holophyte.config.config_tables import verify_config
from holophyte.config.pipeline_settings import (
    CHECK_AFTER,
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
UNLAUNCHED = re.compile(
    r"^(?:\S*/)?(?:ba|da|z)?sh: (?:line )?(?:\d+: )?.+: "
    r"(?:command not found|not found|Permission denied|cannot execute.*)$")
UNBOUND = ("this check's pinned text holds a redacted value this process did "
           "not pin, so it cannot run it")


def run(project, conn, run_id, beat_s, wt, base_sha, sha, after, **detail):
    for check in configured(project, conn, run_id, after):
        reason = skip_reason(check, wt, base_sha, sha)
        if reason:
            record.skip(conn, run_id, check["stage"],
                        scrubbed_output(reason, check["secrets"]), sha)
            continue
        with record.Attempt(conn, run_id, check["stage"], sha,
                            {**detail, "mode": check["mode"] or REDACTED},
                            wt=wt) as attempt, \
                heartbeat_while(conn, run_id, beat_s):
            outcome, found = (ran(project, check, wt)
                              if check["commands"] is not None
                              else ("error", {"error": UNBOUND}))
            attempt.close(outcome, "next", sha, **found)


def configured(project, conn, run_id, after):
    secrets = check_secrets(project)
    pinned = None
    if conn is not None and run_id is not None:
        pinned = pipelines.run_pipelines(conn, [run_id]).get(run_id)
    if not pinned:
        return [{**check, "stage": check["id"], "secrets": secrets}
                for check in current_checks(project) if check["after"] == after]
    stages = [stage for stage in pinned.get("stages", []) if "after" in stage]
    raw, held = record.pinned_raw_checks(conn, run_id) or ((), frozenset())
    if len(raw) == len(stages):
        return [{**check, "stage": stage["id"], "secrets": secrets | held}
                for stage, check in zip(stages, raw) if check["after"] == after]
    return [unbound(stage, secrets) for stage in stages
            if stage["after"] == after or stage["after"] not in CHECK_AFTER]


def unbound(stage, secrets):
    mode = stage["mode"] if stage["mode"] in CHECK_MODES else None
    paths = stage.get("paths") or []
    globbed = not (stage.get("redacted")
                   and any(REDACTED in glob for glob in paths))
    runnable = (not stage.get("redacted") and stage["after"] in CHECK_AFTER
                and mode is not None)
    return {**stage, "stage": stage["id"], "secrets": secrets, "mode": mode,
            "paths": paths if globbed else None,
            "commands": stage["commands"] if runnable else None}


def skip_reason(check, wt, base_sha, sha):
    if check["mode"] == "off":
        return "mode off"
    if not check["paths"]:
        return None
    changed = _changed_files(wt, review_base(wt, base_sha, sha), sha)
    if any(matching(path, check["paths"]) for path in changed):
        return None
    return f"no changed file matches {', '.join(check['paths'])}"


def ran(project, check, wt):
    try:
        snapshot = Snapshot(wt)
    except Exception as error:
        outcome, found = "error", {"error": str(error)}
    else:
        try:
            outcome, found = _outcome(project, check, wt)
        finally:
            restored, failed = _restored(snapshot)
        if failed is not None:
            outcome, found = "error", {**found, "check_outcome": outcome,
                                       "restore_error": failed}
        if restored:
            found["restored"] = restored
    return outcome, _scrubbed(found, check["secrets"])


def _scrubbed(found, secrets):
    found = {key: scrubbed_output(value, secrets)
             if key in ("output", "error", "restore_error") else value
             for key, value in found.items()}
    if "restored" in found:
        found["restored"] = [scrubbed_output(path, secrets)
                             for path in found["restored"]]
    return found


def _restored(snapshot):
    try:
        return snapshot.restore(), None
    except Exception as error:
        return [], str(error)


def _outcome(project, check, wt):
    try:
        status, out = _verify_command(project, " && ".join(check["commands"]),
                                      wt, verify_config(project).timeout_sec)
    except subprocess.TimeoutExpired as expired:
        return "fail", {"exit_status": None, "timed_out": True,
                        "output": _text(expired.output)}
    except Exception as error:
        return "error", {"error": str(error)}
    out = _text(out)
    if status == 0:
        return "pass", {"exit_status": 0, "output": out}
    last = out.splitlines()[-1] if out.strip() else ""
    unlaunched = status in (126, 127) and bool(UNLAUNCHED.match(last))
    return "error" if unlaunched else "fail", {
        "exit_status": status, "timed_out": False, "output": out}


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


class Snapshot:
    """The index, each file or link in the worktree, and a copy of each file
    that differs from HEAD or that Git was told not to look at."""

    def __init__(self, wt):
        self.wt = Path(wt)
        self.index = Path(wt, _git(wt, "rev-parse", "--git-path", "index"))
        self.staged = _state(self.index)
        self.clean = _clean(wt)
        self.found = _walk(self.wt)
        self.saved = tempfile.TemporaryDirectory(prefix="holo-check-")
        try:
            for path, signature in self.found.items():
                if signature[0] == "file" and path not in self.clean:
                    copy = Path(self.saved.name, path)
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.wt / path, copy,
                                    follow_symlinks=False)
        except BaseException:
            self.saved.cleanup()
            raise

    def restore(self):
        failed, restored = [], set()
        try:
            self._restore_paths(failed, restored)
        except Exception as error:
            failed.append(str(error))
        finally:
            try:
                if self.staged is not None:
                    _put(self.index, self.staged)
            except Exception as error:
                failed.append(f"index: {error}")
            self.saved.cleanup()
        if failed:
            raise OSError("could not restore " + "; ".join(failed))
        return sorted(restored)

    def _restore_paths(self, failed, restored):
        now = _walk(self.wt)
        added = sorted(path for path in now if path not in self.found)
        changed = sorted(path for path, kept in self.found.items()
                         if now.get(path) != kept)
        for path in added + changed:
            try:
                self._put_back(path)
                restored.add(path)
            except Exception as error:
                failed.append(f"{path}: {error}")

    def _put_back(self, path):
        target, kept = self.wt / path, self.found.get(path)
        if kept is None:
            target.unlink(missing_ok=True)
        elif kept[0] == "link":
            _put(target, kept)
        elif path in self.clean:
            content = subprocess.run(
                ["git", "cat-file", "--filters", f"HEAD:{path}"], cwd=self.wt,
                capture_output=True, check=True).stdout
            _replace(target, kept[-1], lambda out: out.write(content))
        else:
            with Path(self.saved.name, path).open("rb") as copy:
                _replace(target, kept[-1],
                         lambda out: shutil.copyfileobj(copy, out))


def _git(wt, *args):
    return subprocess.run(["git", *args], cwd=wt, capture_output=True,
                          check=True).stdout.decode().strip()


def _listed(wt, *args):
    listed = subprocess.run(["git", *args], cwd=wt, capture_output=True,
                            check=True).stdout
    return {os.fsdecode(path) for path in listed.split(b"\0") if path}


def _clean(wt):
    looked = {entry[2:] for entry in _listed(wt, "ls-files", "-z", "-v")
              if entry.startswith("H ")}
    return looked - _listed(
        wt, "ls-files", "-z", "--modified", "--deleted") - _listed(
        wt, "diff", "--cached", "--name-only", "-z", "HEAD")


def _walk(root):
    found = {}
    for top, dirs, files in os.walk(root):
        here = Path(top)
        links = [name for name in dirs if (here / name).is_symlink()]
        dirs[:] = [name for name in dirs
                   if name != ".git" and name not in links]
        for name in files + links:
            signature = _signature(here / name) if name != ".git" else None
            if signature is not None:
                found[(here / name).relative_to(root).as_posix()] = signature
    return found


def _state(path):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return "link", os.readlink(path)
    if stat.S_ISREG(info.st_mode):
        return "file", path.read_bytes(), stat.S_IMODE(info.st_mode)
    return None


def _signature(path):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        return "link", os.readlink(path)
    if stat.S_ISREG(info.st_mode):
        return ("file", info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, stat.S_IMODE(info.st_mode))
    return None


def _put(target, state):
    if state[0] == "link":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        os.symlink(state[1], target)
        return
    _replace(target, state[2], lambda out: out.write(state[1]))


def _replace(target, mode, write):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    handle, staging = tempfile.mkstemp(dir=target.parent, prefix=".holo-")
    try:
        with os.fdopen(handle, "wb") as out:
            write(out)
        os.chmod(staging, mode)
        os.replace(staging, target)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise
