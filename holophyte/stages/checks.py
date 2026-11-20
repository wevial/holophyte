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
    pinned_checks,
)
from holophyte.loop.gates import _verify_command
from holophyte.loop.runs import heartbeat_while
from holophyte.redact import REDACTED, outbound, redact_document
from holophyte.review.blast_radius import matching
from holophyte.review.briefs import _changed_files
from holophyte.stages import record
from store import pipelines

OUTPUT_TAIL = 1000
PINNED_KEYS = ("id", "after", "mode", "commands", "paths")
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
    selectors = _pinned_selectors(project, stages)
    found = [unbound(stage, secrets, selectors[at])
             for at, stage in enumerate(stages)]
    return [check for check in found
            if check["after"] == after or check["after"] not in CHECK_AFTER]


def _pinned_selectors(project, stages):
    try:
        raw, shown, _ = pinned_checks(project)
    except (Exception, SystemExit):
        raw, shown = (), ()
    forms = [redact_document({key: display[key] for key in PINNED_KEYS})
             for display in shown]
    return [next((check for check, form in zip(raw, forms)
                  if form == {key: stage.get(key) for key in PINNED_KEYS}),
                 None) for stage in stages]


def unbound(stage, secrets, original=None):
    selectors = original or stage
    mode = selectors["mode"] if selectors["mode"] in CHECK_MODES else None
    paths = selectors.get("paths") or []
    globbed = original is not None or not (
        stage.get("redacted") and any(REDACTED in glob for glob in paths))
    runnable = (not stage.get("redacted") and selectors["after"] in CHECK_AFTER
                and mode is not None)
    return {**stage, "stage": stage["id"], "secrets": secrets,
            "after": selectors["after"], "mode": mode,
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
    """HEAD, the index, and each directory, link and file in the worktree,
    with a copy of every file's bytes."""

    def __init__(self, wt):
        self.wt = Path(wt)
        self.index = Path(wt, _git(wt, "rev-parse", "--git-path", "index"))
        self.branch = _branch(wt)
        self.head = _git(wt, "rev-parse", "--verify", "HEAD")
        self.staged = _state(self.index)
        self.found = _walk(self.wt)
        self.saved = tempfile.mkdtemp(prefix="holo-check-")
        try:
            for path, signature in self.found.items():
                if signature[0] == "file":
                    copy = Path(self.saved, path)
                    copy.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(self.wt / path, copy,
                                    follow_symlinks=False)
        except BaseException:
            shutil.rmtree(self.saved, ignore_errors=True)
            raise

    def restore(self):
        failed, restored = [], set()
        try:
            self._restore_head()
        except Exception as error:
            failed.append(f"HEAD: {error}")
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
        if failed:
            raise OSError("could not restore " + "; ".join(failed)
                          + f"; the saved copies are kept in {self.saved}")
        shutil.rmtree(self.saved, ignore_errors=True)
        return sorted(restored)

    def _restore_head(self):
        if self.branch is None:
            _git(self.wt, "update-ref", "--no-deref", "HEAD", self.head)
            return
        _git(self.wt, "update-ref", self.branch, self.head)
        _git(self.wt, "symbolic-ref", "HEAD", self.branch)

    def _restore_paths(self, failed, restored):
        now = _walk(self.wt)
        added = sorted((path for path in now if path not in self.found),
                       key=_depth, reverse=True)
        changed = sorted((path for path, kept in self.found.items()
                          if now.get(path) != kept), key=_depth)
        folders = [path for path in changed if self.found[path][0] == "dir"]
        steps = ([(path, self._open_folder) for path in folders]
                 + [(path, self._remove) for path in added]
                 + [(path, self._put_back) for path in changed
                    if path not in folders]
                 + [(path, self._close_folder) for path in reversed(folders)])
        for path, step in steps:
            try:
                step(path)
                restored.add(path)
            except Exception as error:
                failed.append(f"{path}: {error}")

    def _open_folder(self, path):
        target = self.wt / path
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            target.unlink()
        target.mkdir(exist_ok=True)
        os.chmod(target, self.found[path][1] | stat.S_IRWXU)

    def _close_folder(self, path):
        os.chmod(self.wt / path, self.found[path][1])

    def _remove(self, path):
        target = self.wt / path
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)

    def _put_back(self, path):
        target, kept = self.wt / path, self.found[path]
        if kept[0] == "link":
            _put(target, kept)
            return
        with Path(self.saved, path).open("rb") as copy:
            _replace(target, kept[-1],
                     lambda out: shutil.copyfileobj(copy, out))


def _git(wt, *args):
    return subprocess.run(["git", *args], cwd=wt, capture_output=True,
                          check=True).stdout.decode().strip()


def _branch(wt):
    named = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=wt,
                           capture_output=True)
    return named.stdout.decode().strip() if named.returncode == 0 else None


def _depth(path):
    return path.count("/")


def _walk(root):
    found = {}
    for top, dirs, files in os.walk(root):
        here = Path(top)
        links = [name for name in dirs if (here / name).is_symlink()]
        dirs[:] = [name for name in dirs
                   if name != ".git" and name not in links]
        for name in files + links + dirs:
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
    if stat.S_ISDIR(info.st_mode):
        return "dir", stat.S_IMODE(info.st_mode)
    if stat.S_ISREG(info.st_mode):
        return ("file", info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, stat.S_IMODE(info.st_mode))
    return None


def _put(target, state):
    if state[0] == "link":
        target.parent.mkdir(parents=True, exist_ok=True)
        _clear_directory(target)
        target.unlink(missing_ok=True)
        os.symlink(state[1], target)
        return
    _replace(target, state[2], lambda out: out.write(state[1]))


def _replace(target, mode, write):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    _clear_directory(target)
    handle, staging = tempfile.mkstemp(dir=target.parent, prefix=".holo-")
    try:
        with os.fdopen(handle, "wb") as out:
            write(out)
        os.chmod(staging, mode)
        os.replace(staging, target)
    except BaseException:
        Path(staging).unlink(missing_ok=True)
        raise


def _clear_directory(target):
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
