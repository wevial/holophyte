"""A project's `[[pipeline.check]]` stages run on a candidate and recorded."""
import os
import re
import subprocess
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
    snapshot = Snapshot(wt)
    try:
        outcome, found = _outcome(project, check, wt)
    finally:
        restored = snapshot.restore()
    found = {key: scrubbed_output(value, check["secrets"])
             if key in ("output", "error") else value
             for key, value in found.items()}
    if restored:
        found["restored"] = [scrubbed_output(path, check["secrets"])
                             for path in restored]
    return outcome, found


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
    """The index, the content of every file that differs from HEAD, and which
    ignored files exist; an ignored file a check rewrites is not put back."""

    def __init__(self, wt):
        self.wt = Path(wt)
        self.index = Path(wt, _git(wt, "rev-parse", "--git-path", "index"))
        self.staged = self.index.read_bytes() if self.index.exists() else None
        self.files = {path: _read(self.wt / path) for path in _outside(wt)}
        self.ignored = _ignored(wt)

    def restore(self):
        changed = sorted(path for path in _outside(self.wt) | set(self.files)
                         if _read(self.wt / path) != self.files.get(path))
        added = sorted(_ignored(self.wt) - self.ignored)
        for path in added:
            (self.wt / path).unlink(missing_ok=True)
        for path in changed:
            target, kept = self.wt / path, self.files.get(path)
            if kept is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(kept)
            elif _tracked(self.wt, path):
                _git(self.wt, "restore", "--source=HEAD", "--worktree", "--",
                     path)
            else:
                target.unlink(missing_ok=True)
        if self.staged is not None:
            self.index.write_bytes(self.staged)
        return sorted(changed + added)


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


def _tracked(wt, path):
    return subprocess.run(["git", "cat-file", "-e", f"HEAD:{path}"], cwd=wt,
                          capture_output=True).returncode == 0


def _read(path):
    return path.read_bytes() if path.is_file() else None
