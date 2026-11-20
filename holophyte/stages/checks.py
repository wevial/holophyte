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
from holophyte.loop.gates import run_verify, timed_out, verify_timed_out
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
    readable = (not stage.get("redacted") and stage["after"] in CHECK_AFTER
                and stage["mode"] in CHECK_MODES)
    return {**stage, "stage": stage["id"], "secrets": secrets,
            **({} if readable else {"commands": None, "paths": None,
                                    "mode": None})}


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
        ok, out = run_verify(" && ".join(check["commands"]), wt, None,
                             verify_config(project).timeout_sec,
                             project=project, rerun=False)
    except Exception as error:
        return "error", {"error": str(error)}
    if ok:
        return "pass", {"exit_status": 0, "output": str(out)}
    facts = getattr(out, "failure", None) or {}
    status = facts.get("exit_status")
    unlaunched = status in (126, 127) and bool(UNLAUNCHED.match(
        str(facts.get("last_output_line"))))
    return "error" if unlaunched else "fail", {
        "exit_status": status,
        "timed_out": timed_out(out) or verify_timed_out(out),
        "output": str(out)}


def scrubbed_output(text, secrets):
    text = outbound(text, secrets)
    for secret in sorted(secrets, key=len, reverse=True):
        for size in range(len(secret) - 1, FRAGMENT - 1, -1):
            for part in (secret[:size], secret[-size:]):
                text = text.replace(part, REDACTED)
    return text[-OUTPUT_TAIL:]


class Snapshot:
    """The index, tracked modes, the content, mode or link target of each path
    that differs from HEAD, and a copy of each ignored file or link."""

    def __init__(self, wt):
        self.wt = Path(wt)
        self.index = Path(wt, _git(wt, "rev-parse", "--git-path", "index"))
        self.staged = _state(self.index)
        self.files = {path: _state(self.wt / path) for path in _outside(wt)}
        self.modes = _modes(wt)
        self.ignored = _ignored(wt)
        self.saved = tempfile.TemporaryDirectory(prefix="holo-check-")
        try:
            self.kept = {path: _kept(self.wt / path, Path(self.saved.name, path))
                         for path in self.ignored}
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
        changed = sorted(path for path in _outside(self.wt) | set(self.files)
                         if _differs(self.wt / path, self.files.get(path)))
        changed += sorted(path for path, mode in self.modes.items()
                          if path not in changed and path not in self.files
                          and _mode(self.wt / path) not in (None, mode))
        added = sorted(_ignored(self.wt) - self.ignored)
        moved = sorted(path for path, kept in self.kept.items()
                       if kept is not None
                       and _signature(self.wt / path) != kept)
        for path, put in [(path, self._drop) for path in added] + [
                (path, self._put_back) for path in changed] + [
                (path, self._put_kept) for path in moved]:
            try:
                put(path)
                restored.add(path)
            except Exception as error:
                failed.append(f"{path}: {error}")

    def _drop(self, path):
        (self.wt / path).unlink(missing_ok=True)

    def _put_back(self, path):
        target, kept = self.wt / path, self.files.get(path)
        if kept is not None:
            _put(target, kept)
            return
        if not _tracked(self.wt, path):
            target.unlink(missing_ok=True)
            return
        _git(self.wt, "restore", "--source=HEAD", "--worktree", "--", path)
        if self.modes.get(path) is not None:
            os.chmod(target, self.modes[path])

    def _put_kept(self, path):
        kept, target = self.kept[path], self.wt / path
        if kept[0] == "link":
            _put(target, kept)
            return
        with Path(self.saved.name, path).open("rb") as copy:
            _replace(target, kept[-1],
                     lambda out: shutil.copyfileobj(copy, out))


def _git(wt, *args):
    return subprocess.run(["git", *args], cwd=wt, capture_output=True,
                          check=True).stdout.decode().strip()


def _outside(wt):
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--modified", "--deleted", "--others",
         "--exclude-standard"], cwd=wt, capture_output=True, check=True).stdout
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "HEAD"], cwd=wt,
        capture_output=True, check=True).stdout
    return {os.fsdecode(path) for path in (listed + b"\0" + staged).split(b"\0")
            if path}


def _ignored(wt):
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--ignored", "--exclude-standard"],
        cwd=wt, capture_output=True, check=True).stdout
    return {os.fsdecode(path) for path in listed.split(b"\0") if path}


def _modes(wt):
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=wt,
                            capture_output=True, check=True).stdout
    paths = {os.fsdecode(path) for path in listed.split(b"\0") if path}
    return {path: _mode(Path(wt, path)) for path in paths}


def _mode(path):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return stat.S_IMODE(info.st_mode) if stat.S_ISREG(info.st_mode) else None


def _tracked(wt, path):
    return subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"], cwd=wt,
                          capture_output=True).returncode == 0


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


def _differs(path, kept):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return kept is not None
    if kept is None:
        return True
    if stat.S_ISREG(info.st_mode) and kept[0] == "file" \
            and stat.S_IMODE(info.st_mode) != kept[2]:
        return True
    try:
        return _state(path) != kept
    except OSError:
        return True


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


def _kept(path, copy):
    signature = _signature(path)
    if signature is not None and signature[0] == "file":
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copy, follow_symlinks=False)
    return signature


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
